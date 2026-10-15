"""What the server's sessions spend their time waiting on, sampled from ``pg_stat_activity``.

PostgreSQL records what each backend is waiting on at this instant
(``wait_event_type`` and ``wait_event``) and keeps no history of it. Without
an extension, the only way to learn where time goes is to look again and
again: each look is exact, and the profile built from many looks is a sample
of the interval, as good as the number of looks.

:class:`WaitSampler` takes those looks, one statement each, at a stated
interval from the client, and builds a profile from them: how many sessions
were active on average (*average active sessions*, AAS), and how that splits
across wait events and queries. It measures the interval it actually achieved
from the server's clock, rather than reporting the one it asked for, since a
round trip, a busy server or a slow client stretch it.

Where the ``pg_wait_sampling`` extension is installed, which samples in the
server every few milliseconds, its profile over the same window is read too,
and the result says which source each figure comes from. Azure's Query Store
records waits too (``query_store.pgms_wait_sampling_view``), over intervals of
its own; the last of them is read when it is there.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from collections import defaultdict
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from itertools import pairwise
from typing import Any
from typing import LiteralString
from typing import cast

from ..db import QUERY_MARKER
from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..db import get_postgres_version
from ..db.privileges import HIDDEN_QUERY_TEXT
from ..db.privileges import StatisticsAccess
from ..db.privileges import statistics_access
from ..errors import InvalidArgumentError
from ..severity import Confidence

#: The interval between two samples unless asked otherwise, in seconds. Each
#: sample is one short statement; at twice a second a ten-second sample takes
#: twenty looks, and costs the server next to nothing.
DEFAULT_SAMPLE_INTERVAL = 0.5

#: The shortest interval accepted. A round trip takes about a millisecond on
#: a local socket, so anything shorter could not be achieved, and a sample
#: that reported it would be reporting what it asked for.
MIN_SAMPLE_INTERVAL = 0.01

#: The most samples one run takes, so that a typo in the duration does not
#: keep a connection busy for a day.
MAX_SAMPLES = 100_000

#: pg_stat_activity.query_id was added in PostgreSQL 14.
QUERY_ID_MIN_VERSION = 14

#: How many wait events each query's entry lists.
WAITS_PER_QUERY = 3

#: How many queries the profile lists, the most active first.
MAX_QUERIES = 20

#: What a session that is active and reports no wait event is shown as.
#: PostgreSQL reports no wait while a backend runs on the CPU, and also while
#: it waits on something it does not instrument; which of the two cannot be
#: told from pg_stat_activity.
CPU = "CPU"

#: What a backend waits on in its main loop when it has nothing to do. The
#: documentation describes the type as "the server process is idle", so a
#: session in one is not counted as active.
IDLE_WAIT_TYPE = "Activity"


def _active_filter() -> str:
    """SQL for "this backend is doing something", read as ``a`` from pg_stat_activity.

    A client backend is active when its state says so. The server's own
    processes have no state; they are counted unless they sit in an
    ``Activity`` wait, which is their main loop idling. pg-debug's own
    statements, from this run or another, are left out by their marker. A
    backend whose state is withheld from the role is returned so that it can
    be counted as unknown.
    """
    marker = QUERY_MARKER.replace("_", r"\_")
    return f"""(
        a.query = '{HIDDEN_QUERY_TEXT}'
        OR (
            (a.state = 'active' OR (a.state IS NULL AND a.backend_type IS DISTINCT FROM 'client backend'))
            AND a.wait_event_type IS DISTINCT FROM '{IDLE_WAIT_TYPE}'
            AND a.query NOT LIKE '{marker}%'
        )
    )"""


def sample_sql(has_query_id: bool) -> LiteralString:
    """One sample: the server's time, and every active backend but pg-debug's own.

    The time comes from a subquery joined to the backends so that a sample
    with no active backend still returns its one row, with no pid.
    """
    query_id = "a.query_id" if has_query_id else "NULL::bigint AS query_id"
    return cast(
        LiteralString,
        f"""
        SELECT t.sampled_at, s.*
        FROM (SELECT clock_timestamp() AS sampled_at) t
        LEFT JOIN LATERAL (
            SELECT
                a.pid,
                a.datname,
                a.usename,
                a.backend_type,
                a.state,
                a.wait_event_type,
                a.wait_event,
                {query_id},
                a.query
            FROM pg_stat_activity a
            WHERE a.pid <> pg_backend_pid()
              AND {_active_filter()}
        ) s ON true
        """,
    )


#: What the profile is read with, once, before sampling: whether queries have
#: an id to group by, how much of their text pg_stat_activity keeps, and
#: which extensions that sample waits are installed, and where.
SETUP_SQL = """
    SELECT
        current_setting('compute_query_id', true) AS compute_query_id,
        current_setting('track_activities', true) AS track_activities,
        current_setting('track_activity_query_size', true) AS track_activity_query_size,
        (SELECT quote_ident(n.nspname) FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
          WHERE e.extname = 'pg_wait_sampling') AS pg_wait_sampling_schema,
        current_setting('pg_wait_sampling.profile_period', true) AS profile_period,
        current_setting('pg_wait_sampling.profile_queries', true) AS profile_queries,
        current_setting('pg_wait_sampling.sample_cpu', true) AS sample_cpu,
        to_regclass('query_store.pgms_wait_sampling_view') IS NOT NULL AS has_azure_wait_sampling
"""

#: The columns of Azure's view that :data:`AZURE_LAST_INTERVAL_SQL` reads.
#: Read from the catalog first, so that a view of another shape is reported
#: as such rather than failing.
AZURE_COLUMNS_SQL = """
    SELECT array_agg(a.attname::text ORDER BY a.attname) AS columns
    FROM pg_attribute a
    WHERE a.attrelid = to_regclass('query_store.pgms_wait_sampling_view')
      AND a.attnum > 0
      AND NOT a.attisdropped
"""

AZURE_REQUIRED_COLUMNS = frozenset({"event_type", "event", "calls", "start_time", "end_time"})

#: Azure Query Store's waits for its most recent interval, by event.
AZURE_LAST_INTERVAL_SQL = """
    WITH last AS (
        SELECT max(start_time) AS start_time FROM query_store.pgms_wait_sampling_view
    )
    SELECT
        w.event_type,
        w.event,
        sum(w.calls)::bigint AS samples,
        min(w.start_time) AS start_time,
        max(w.end_time) AS end_time
    FROM query_store.pgms_wait_sampling_view w
    JOIN last ON w.start_time = last.start_time
    GROUP BY w.event_type, w.event
    ORDER BY samples DESC
"""


def _wait_sampling_profile_sql(schema: str) -> LiteralString:
    """pg_wait_sampling's cumulative profile, by event and query, with the server's time."""
    return cast(
        LiteralString,
        f"""
        SELECT t.read_at, p.event_type, p.event, p.queryid, p.count
        FROM (SELECT clock_timestamp() AS read_at) t
        LEFT JOIN LATERAL (
            SELECT event_type, event, queryid, sum(count)::bigint AS count
            FROM {schema}.pg_wait_sampling_profile
            GROUP BY event_type, event, queryid
        ) p ON true
        """,
    )


# -- Normalizing query text --------------------------------------------------

_STRING_LITERAL = re.compile(r"(?:[EeBbXxNn])?'(?:[^']|'')*'")
_NUMBER = re.compile(r"(?<![\w$.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b")
_WHITESPACE = re.compile(r"\s+")
_IN_LIST = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")


def normalize_query(text: str | None) -> str:
    """A query's text with its constants replaced by ``?``, to group the ones that differ only in them.

    A string, a number, and a list of them in ``IN (...)`` become ``?``;
    whitespace runs become one space. Bind parameters (``$1``) are left as
    they are. It is a textual approximation of what ``query_id`` does from
    the parse tree, and it groups by what pg_stat_activity kept of the text,
    which is cut at ``track_activity_query_size`` bytes.
    """
    if not text:
        return ""
    normalized = _STRING_LITERAL.sub("?", text)
    normalized = _NUMBER.sub("?", normalized)
    normalized = _WHITESPACE.sub(" ", normalized).strip()
    return _IN_LIST.sub("(?)", normalized)


# -- The profile -------------------------------------------------------------


def wait_label(wait_event_type: str | None, wait_event: str | None) -> str:
    """``Lock:transactionid``, or :data:`CPU` for an active session that reports no wait."""
    if not wait_event_type and not wait_event:
        return CPU
    return f"{wait_event_type or '?'}:{wait_event or '?'}"


@dataclass(frozen=True)
class SampleTiming:
    """How many samples were taken, and the interval between them as measured on the server's clock.

    Attributes:
        samples: How many were taken.
        requested_seconds: How long the sampling was asked to last.
        requested_interval_seconds: The interval asked for.
        achieved_interval_seconds: The mean interval between consecutive
            samples, from the server's ``clock_timestamp()`` at each. None
            with fewer than two samples.
        shortest_gap_seconds: The shortest interval between two samples.
        longest_gap_seconds: The longest: a gap much longer than asked for is
            time the profile did not look at.
        span_seconds: From the first sample to the last.
        started_at: The server's time at the first sample.
    """

    samples: int
    requested_seconds: float
    requested_interval_seconds: float
    achieved_interval_seconds: float | None
    shortest_gap_seconds: float | None
    longest_gap_seconds: float | None
    span_seconds: float
    started_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["started_at"] = self.started_at.isoformat() if self.started_at else None
        return result


@dataclass
class WaitProfile:
    """Where active sessions spent the sampled interval, and how far that can be trusted.

    Each sample is exact: it is what pg_stat_activity showed at that instant.
    The profile is a sample of the interval: a wait shorter than the interval
    between samples is seen in proportion to how often it happens, not
    reliably, and one that happened only between two samples is not seen at
    all. So every attribution carries :attr:`Confidence.CANDIDATE`.

    Attributes:
        source: Where the main profile was read: ``pg_stat_activity``.
        timing: How many samples were taken and the interval achieved.
        active_sessions: Average active sessions (AAS) over the samples:
            session-samples divided by samples.
        active_sessions_max: The most sessions active in any one sample.
        active_client_sessions: The part of ``active_sessions`` that is
            client backends, the application's sessions; the rest is the
            server's own processes at work (autovacuum, the checkpointer).
        waits: One entry per wait event, the most sampled first:
            ``wait`` (``Type:Event``, or ``CPU``), ``wait_event_type``,
            ``wait_event``, ``samples`` (session-samples in it),
            ``active_sessions`` (its share of AAS), ``share`` of all
            session-samples, and ``confidence``.
        queries: The most active queries, each with ``query_id`` or the
            normalized ``query`` it was grouped by, ``samples``,
            ``active_sessions``, ``share``, ``waits`` (its most sampled wait
            events) and ``confidence``. A server process that runs no query
            (autovacuum, the checkpointer) is listed by its ``backend_type``.
        query_grouping: ``query_id`` when every query was grouped by the id
            the server computed, ``normalized_text`` when none was, ``mixed``
            when some were.
        query_grouping_reason: Why, when not every query had an id.
        withheld_per_sample: The most backends, in any one sample, whose
            state PostgreSQL withheld from the role, which leaves whether they
            were active unknown. They are not in any figure above.
        statistics_access: Why they were withheld, when some were.
        extension: What a wait-sampling extension recorded over the same
            window, or why it was not read; None when none is installed.
        notes: What the profile cannot tell, in sentences.
    """

    source: str
    timing: SampleTiming
    active_sessions: float
    active_sessions_max: int
    active_client_sessions: float = 0.0
    waits: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    query_grouping: str = "query_id"
    query_grouping_reason: str | None = None
    withheld_per_sample: int = 0
    statistics_access: StatisticsAccess | None = None
    extension: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "exactness": "each sample is exact; the profile is a sample of the interval",
            "timing": self.timing.to_dict(),
            "active_sessions": self.active_sessions,
            "active_sessions_max": self.active_sessions_max,
            "active_client_sessions": self.active_client_sessions,
            "waits": self.waits,
            "queries": self.queries,
            "query_grouping": self.query_grouping,
            "query_grouping_reason": self.query_grouping_reason,
            "withheld_per_sample": self.withheld_per_sample,
            "statistics_access": asdict(self.statistics_access) if self.statistics_access else None,
            "extension": self.extension,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class _Setup:
    has_query_id: bool
    compute_query_id: str | None
    track_activities: str | None
    query_size: str | None
    wait_sampling_schema: str | None
    profile_period_ms: float | None
    profile_queries: str | None
    sample_cpu: str | None
    has_azure: bool
    version: int


class WaitSampler:
    """Samples pg_stat_activity at an interval and builds a :class:`WaitProfile`."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def sample(self, seconds: float, interval: float = DEFAULT_SAMPLE_INTERVAL) -> WaitProfile:
        """Sample for ``seconds``, one look every ``interval`` seconds, and build the profile.

        Every look is a statement of its own, so nothing is kept on the
        server between them: behind a transaction pooler each may reach a
        different backend, and each leaves its own out.

        Raises:
            InvalidArgumentError: If either is not positive, the interval is
                shorter than :data:`MIN_SAMPLE_INTERVAL` or not shorter than
                the duration, or they would take more than :data:`MAX_SAMPLES`.
        """
        count = check_sampling(seconds, interval)
        setup = await self._setup()
        query = sample_sql(setup.has_query_id)

        extension_before = await self._read_wait_sampling(setup)

        samples: list[list[dict[str, Any]]] = []
        times: list[datetime] = []
        start = time.monotonic()
        for index in range(count):
            delay = start + index * interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            rows = await self.sql_driver.execute_query(query)
            cells = [row.cells for row in rows or []]
            if cells:
                times.append(cells[0]["sampled_at"])
            samples.append([row for row in cells if row.get("pid") is not None])

        extension_after = await self._read_wait_sampling(setup)

        profile = build_profile(samples, times, seconds, interval, setup.has_query_id)
        _explain_grouping(profile, setup)
        if profile.withheld_per_sample:
            profile.statistics_access = await statistics_access(self.sql_driver)
        profile.extension = await self._extension_profile(setup, extension_before, extension_after)
        if setup.track_activities == "off":
            profile.notes.insert(
                0,
                "track_activities is off, so pg_stat_activity does not report what sessions are doing, and every figure here is empty.",
            )
        return profile

    async def _setup(self) -> _Setup:
        version = await get_postgres_version(self.sql_driver)
        rows = await self.sql_driver.execute_query(SETUP_SQL)
        cells = rows[0].cells if rows else {}
        period = cells.get("profile_period")
        try:
            period_ms = float(period) if period not in (None, "") else None
        except ValueError:
            period_ms = None
        return _Setup(
            has_query_id=version >= QUERY_ID_MIN_VERSION,
            compute_query_id=cells.get("compute_query_id"),
            track_activities=cells.get("track_activities"),
            query_size=cells.get("track_activity_query_size"),
            wait_sampling_schema=cells.get("pg_wait_sampling_schema"),
            profile_period_ms=period_ms,
            profile_queries=cells.get("profile_queries"),
            sample_cpu=cells.get("sample_cpu"),
            has_azure=bool(cells.get("has_azure_wait_sampling")),
            version=version,
        )

    async def _read_wait_sampling(self, setup: _Setup) -> list[dict[str, Any]] | str | None:
        """pg_wait_sampling's profile as it stands, or why it could not be read, or None without it."""
        if setup.wait_sampling_schema is None:
            return None
        try:
            rows = await self.sql_driver.execute_query(_wait_sampling_profile_sql(setup.wait_sampling_schema))
        except Exception as error:
            return f"reading {setup.wait_sampling_schema}.pg_wait_sampling_profile failed: {error}"
        return [row.cells for row in rows or []]

    async def _extension_profile(
        self,
        setup: _Setup,
        before: list[dict[str, Any]] | str | None,
        after: list[dict[str, Any]] | str | None,
    ) -> dict[str, Any] | None:
        if setup.wait_sampling_schema is not None:
            return wait_sampling_delta(before, after, setup.profile_period_ms, setup.sample_cpu)
        if setup.has_azure:
            return await self._azure_last_interval()
        return None

    async def _azure_last_interval(self) -> dict[str, Any]:
        source = "query_store.pgms_wait_sampling_view"
        rows = await self.sql_driver.execute_query(AZURE_COLUMNS_SQL)
        columns = set((rows[0].cells.get("columns") if rows else None) or [])
        missing = sorted(AZURE_REQUIRED_COLUMNS - columns)
        if missing:
            return {
                "source": source,
                "read": False,
                "reason": f"the view has no {', '.join(missing)} column, so its shape is not the one pg-debug knows how to read",
            }
        try:
            rows = await self.sql_driver.execute_query(AZURE_LAST_INTERVAL_SQL)
        except Exception as error:
            return {"source": source, "read": False, "reason": f"reading it failed: {error}"}
        events = [row.cells for row in rows or []]
        if not events:
            return {"source": source, "read": False, "reason": "it holds no interval yet"}
        start = min(event["start_time"] for event in events)
        end = max(event["end_time"] for event in events)
        return {
            "source": source,
            "read": True,
            "interval_start": start.isoformat() if isinstance(start, datetime) else start,
            "interval_end": end.isoformat() if isinstance(end, datetime) else end,
            "waits": [
                {
                    "wait": wait_label(event.get("event_type"), event.get("event")),
                    "samples": int(event.get("samples") or 0),
                    "confidence": Confidence.CANDIDATE.value,
                }
                for event in events
            ],
            "note": (
                "Azure Query Store aggregates waits over intervals of its own (pg_qs.interval_length_minutes); this is its "
                "most recent one, not the window sampled here, and it covers the queries Query Store captures."
            ),
        }


def check_sampling(seconds: float, interval: float) -> int:
    """How many samples ``seconds`` at ``interval`` takes, or why it is refused.

    Samples are taken at 0, ``interval``, ``2 * interval``, ... up to but not
    including ``seconds`` from the first, so 10 seconds at 0.5 takes 20.

    Raises:
        InvalidArgumentError: As :meth:`WaitSampler.sample` says.
    """
    if not seconds > 0:
        raise InvalidArgumentError(f"The sampling duration must be greater than zero seconds, not {seconds}.")
    if not interval >= MIN_SAMPLE_INTERVAL:
        raise InvalidArgumentError(
            f"The sampling interval must be at least {MIN_SAMPLE_INTERVAL} seconds, not {interval}: a sample takes a round trip to the server."
        )
    if interval >= seconds:
        raise InvalidArgumentError(
            f"The sampling interval ({interval}s) is not shorter than the duration ({seconds}s), which would take one sample and measure no interval."
        )
    count = max(1, int(-(-seconds // interval)))
    # A float division that lands just above a whole number counts one too many.
    if (count - 1) * interval >= seconds:
        count -= 1
    if count > MAX_SAMPLES:
        raise InvalidArgumentError(f"{seconds}s at {interval}s is {count} samples; pg-debug takes at most {MAX_SAMPLES} in one run.")
    return count


def measure_timing(times: list[datetime], seconds: float, interval: float) -> SampleTiming:
    """The interval the samples achieved, from the server's time at each."""
    gaps = [(later - earlier).total_seconds() for earlier, later in pairwise(times)]
    span = (times[-1] - times[0]).total_seconds() if len(times) > 1 else 0.0
    return SampleTiming(
        samples=len(times),
        requested_seconds=seconds,
        requested_interval_seconds=interval,
        achieved_interval_seconds=round(span / len(gaps), 4) if gaps else None,
        shortest_gap_seconds=round(min(gaps), 4) if gaps else None,
        longest_gap_seconds=round(max(gaps), 4) if gaps else None,
        span_seconds=round(span, 4),
        started_at=times[0] if times else None,
    )


def _query_key(row: dict[str, Any]) -> tuple[str, Any]:
    """What a session-sample is grouped under: its query's id, its normalized text, or its process type."""
    if row.get("backend_type") not in (None, "client backend"):
        return ("backend_type", row.get("backend_type"))
    if row.get("query_id") is not None:
        return ("query_id", row["query_id"])
    return ("query", normalize_query(row.get("query")))


def build_profile(samples: list[list[dict[str, Any]]], times: list[datetime], seconds: float, interval: float, has_query_id: bool) -> WaitProfile:
    """Turn the rows of each sample into a :class:`WaitProfile`.

    Args:
        samples: The active backends of each sample, as :func:`sample_sql`
            returns them, without the row a sample with none returns.
        times: The server's time at each sample.
        seconds: The duration asked for.
        interval: The interval asked for.
        has_query_id: Whether the server's version reports ``query_id``.
    """
    taken = len(samples)
    timing = measure_timing(times, seconds, interval)

    waits: Counter[tuple[str | None, str | None]] = Counter()
    by_query: dict[tuple[str, Any], Counter[tuple[str | None, str | None]]] = defaultdict(Counter)
    shown: dict[tuple[str, Any], dict[str, Any]] = {}
    per_sample: list[int] = []
    withheld_per_sample = 0
    client = 0

    for rows in samples:
        visible = [row for row in rows if row.get("query") != HIDDEN_QUERY_TEXT]
        withheld_per_sample = max(withheld_per_sample, len(rows) - len(visible))
        per_sample.append(len(visible))
        client += sum(1 for row in visible if row.get("backend_type") == "client backend")
        for row in visible:
            wait = (row.get("wait_event_type"), row.get("wait_event"))
            waits[wait] += 1
            key = _query_key(row)
            by_query[key][wait] += 1
            shown.setdefault(key, row)

    total = sum(waits.values())

    def share(count: int) -> float:
        return round(count / total, 4) if total else 0.0

    def aas(count: int) -> float:
        return round(count / taken, 3) if taken else 0.0

    wait_entries = [
        {
            "wait": wait_label(event_type, event),
            "wait_event_type": event_type,
            "wait_event": event,
            "samples": count,
            "active_sessions": aas(count),
            "share": share(count),
            "confidence": Confidence.CANDIDATE.value,
        }
        for (event_type, event), count in waits.most_common()
    ]

    ranked = sorted(by_query.items(), key=lambda item: -sum(item[1].values()))
    query_entries: list[dict[str, Any]] = []
    for key, query_waits in ranked[:MAX_QUERIES]:
        count = sum(query_waits.values())
        row = shown[key]
        entry: dict[str, Any] = {
            "grouped_by": key[0],
            "query_id": row.get("query_id") if key[0] == "query_id" else None,
            "backend_type": row.get("backend_type"),
            "query": key[1] if key[0] == "query" else " ".join(str(row.get("query") or "").split()),
            "datname": row.get("datname"),
            "usename": row.get("usename"),
            "samples": count,
            "active_sessions": aas(count),
            "share": share(count),
            "waits": [
                {"wait": wait_label(event_type, event), "samples": n, "share_of_query": round(n / count, 4)}
                for (event_type, event), n in query_waits.most_common(WAITS_PER_QUERY)
            ],
            "confidence": Confidence.CANDIDATE.value,
        }
        query_entries.append(entry)

    client_keys = [key for key in by_query if key[0] != "backend_type"]
    with_id = sum(1 for key in client_keys if key[0] == "query_id")
    if not client_keys or with_id == len(client_keys):
        grouping = "query_id" if has_query_id else "normalized_text"
    elif with_id == 0:
        grouping = "normalized_text"
    else:
        grouping = "mixed"

    notes = [
        f"Each of the {taken} samples is exact; the profile is a sample of the interval. A wait shorter than the "
        f"{timing.achieved_interval_seconds if timing.achieved_interval_seconds is not None else interval}s between samples "
        "shows in proportion to how often it happens, and one that happened only between two samples is not seen.",
    ]
    if waits.get((None, None)):
        notes.append(
            f"{CPU} is an active session reporting no wait event: on the CPU, or waiting on something PostgreSQL does not "
            "instrument; pg_stat_activity does not tell the two apart."
        )
    if timing.longest_gap_seconds is not None and timing.longest_gap_seconds > 2 * interval:
        notes.append(
            f"The longest gap between two samples was {timing.longest_gap_seconds}s against {interval}s asked for; the "
            "profile did not look at the server during it."
        )

    return WaitProfile(
        source="pg_stat_activity",
        timing=timing,
        active_sessions=aas(total),
        active_sessions_max=max(per_sample, default=0),
        active_client_sessions=aas(client),
        waits=wait_entries,
        queries=query_entries,
        query_grouping=grouping,
        withheld_per_sample=withheld_per_sample,
        notes=notes,
    )


def _explain_grouping(profile: WaitProfile, setup: _Setup) -> None:
    """Say why queries were grouped by their text rather than their id, when they were."""
    if profile.query_grouping == "query_id":
        return
    if not setup.has_query_id:
        reason = f"PostgreSQL {setup.version} does not report query_id in pg_stat_activity; it was added in {QUERY_ID_MIN_VERSION}"
    elif profile.query_grouping == "mixed":
        # The server computes ids, so the ones without were not computed yet:
        # a relation is locked while the statement is parsed, before its id is.
        reason = (
            "the server computes query_id, and reported none for some statements: one still waiting for a lock it takes "
            "while it is parsed has no id yet"
        )
    elif setup.compute_query_id in ("off", None):
        reason = "compute_query_id is off, so the server computes no query_id"
    elif setup.compute_query_id == "auto":
        reason = "compute_query_id is auto and nothing loaded asks for a query_id (pg_stat_statements does), so the server computes none"
    else:
        reason = "the server reported no query_id for any statement sampled"
    size = f" pg_stat_activity keeps the first {setup.query_size} of each query's text." if setup.query_size else ""
    profile.query_grouping_reason = (
        f"Queries without a query_id are grouped by their text with constants replaced by ?: {reason}.{size} Two queries "
        "that differ only past that point are counted as one."
    )


def wait_sampling_delta(
    before: list[dict[str, Any]] | str | None,
    after: list[dict[str, Any]] | str | None,
    profile_period_ms: float | None,
    sample_cpu: str | None,
) -> dict[str, Any]:
    """What pg_wait_sampling counted between two reads of its profile.

    Its profile is cumulative: the samples it took, by event and query,
    since it was last reset. The difference between a read before the
    sampling and one after is what it saw in that window, every
    ``pg_wait_sampling.profile_period`` milliseconds. It samples every
    process, idle ones included, so a session waiting for its client to send
    a query (``Client:ClientRead``) or a server process idling in its main
    loop (``Activity``) is in its counts; the active-session figure leaves
    them out, as the pg_stat_activity profile does.
    """
    source = "pg_wait_sampling_profile"
    if isinstance(before, str) or isinstance(after, str):
        return {"source": source, "read": False, "reason": before if isinstance(before, str) else after}
    if not before or not after:
        return {"source": source, "read": False, "reason": "the profile could not be read before and after the sampling"}

    def counts(rows: list[dict[str, Any]]) -> Counter[tuple[Any, Any, Any]]:
        return Counter(
            {
                (row.get("event_type"), row.get("event"), row.get("queryid")): int(row.get("count") or 0)
                for row in rows
                if row.get("count") is not None
            }
        )

    first, second = counts(before), counts(after)
    if any(second.get(key, 0) < value for key, value in first.items()):
        return {
            "source": source,
            "read": False,
            "reason": "the profile was reset during the sampling (pg_wait_sampling_reset_profile()), so its counts cannot be differenced",
        }
    delta = Counter({key: second[key] - first.get(key, 0) for key in second if second[key] - first.get(key, 0) > 0})
    window = (after[0]["read_at"] - before[0]["read_at"]).total_seconds()

    by_wait: Counter[tuple[Any, Any]] = Counter()
    for (event_type, event, _queryid), count in delta.items():
        by_wait[(event_type, event)] += count

    def idle(event_type: Any, event: Any) -> bool:
        return event_type == IDLE_WAIT_TYPE or (event_type == "Client" and event == "ClientRead")

    expected = window * 1000 / profile_period_ms if profile_period_ms and window > 0 else None
    active = sum(count for (event_type, event), count in by_wait.items() if not idle(event_type, event))
    result: dict[str, Any] = {
        "source": source,
        "read": True,
        "window_seconds": round(window, 3),
        "profile_period_ms": profile_period_ms,
        "samples": sum(by_wait.values()),
        "active_sessions": round(active / expected, 3) if expected else None,
        "waits": [
            {
                "wait": wait_label(event_type, event),
                "samples": count,
                "active_sessions": round(count / expected, 3) if expected else None,
                "idle": idle(event_type, event),
                "confidence": Confidence.CANDIDATE.value,
            }
            for (event_type, event), count in by_wait.most_common()
        ],
        "note": (
            "Sampled inside the server every pg_wait_sampling.profile_period milliseconds, by every process and in every "
            "database; Client:ClientRead and Activity waits are idle sessions and processes and are left out of active_sessions."
        ),
    }
    if sample_cpu == "off":
        result["note"] += " pg_wait_sampling.sample_cpu is off, so time on the CPU is not in it."
    return result


__all__ = [
    "CPU",
    "DEFAULT_SAMPLE_INTERVAL",
    "MIN_SAMPLE_INTERVAL",
    "SampleTiming",
    "WaitProfile",
    "WaitSampler",
    "build_profile",
    "check_sampling",
    "normalize_query",
    "wait_label",
    "wait_sampling_delta",
]

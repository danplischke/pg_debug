import asyncio
import json
import logging
from collections import Counter
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from typing import Literal
from typing import LiteralString
from typing import cast

from .db import QUERY_MARKER
from .db import TRANSACTION_CONTROL as _TRANSACTION_CONTROL
from .db import SafeSqlDriver
from .db import SqlDriver
from .db import workload_only as _workload_only
from .db.statements import PG_STAT_STATEMENTS
from .db.statements import THIS_DATABASE
from .db.statements import StatementStatistics
from .db.statements import locate_pg_stat_statements
from .db.window import CountingWindow
from .db.window import statement_window
from .errors import ExtensionRequiredError
from .errors import InvalidArgumentError
from .severity import Confidence
from .tags import parse_sql_comment

logger = logging.getLogger(__name__)


install_pg_stat_statements_message = (
    "The pg_stat_statements extension is required to "
    "report slow queries, but it is not currently "
    "installed.\n\n"
    "You can install it by running: "
    "`CREATE EXTENSION pg_stat_statements;`\n\n"
    "**What does it do?** It records statistics (like "
    "execution time, number of calls, rows returned) for "
    "every query executed against the database.\n\n"
    "**Is it safe?** Installing 'pg_stat_statements' is "
    "generally safe and a standard practice for performance "
    "monitoring. It adds overhead by tracking statistics, "
    "but this is usually negligible unless under extreme load."
)


#: What pg_stat_statements still shows of a statement whose queryid and text it
#: withholds from the role: by these, a statement that did not run between two
#: snapshots is found in both.
_WITHHELD_IDENTITY = (
    "userid",
    "dbid",
    "toplevel",
    "calls",
    "total_exec_time",
    "rows",
    "shared_blks_hit",
    "shared_blks_read",
    "shared_blks_dirtied",
    "wal_bytes",
)


#: How far a statement's mean time during the sample has to be from its mean
#: before it, as a factor either way, to be called changed.
DEFAULT_CHANGE_RATIO = 3.0

#: How many times longer than the sample a statement's counters must have been
#: counting before it, for its mean before the sample to be a history the
#: sample can be compared with, and for one missing from it to be new.
MIN_WINDOW_FACTOR = 10.0

#: For a comparison with a saved snapshot: the period before the snapshot has
#: to be at least as long as the period since it. A snapshot is taken on
#: purpose, before a deploy or at the start of a day, and the two periods are
#: what is being compared; a sample of a few seconds is not, and needs the
#: longer record above.
SNAPSHOT_WINDOW_FACTOR = 1.0

#: What a snapshot file says it is, and the version of its layout.
SNAPSHOT_FORMAT = "pg-debug top-queries snapshot"
SNAPSHOT_VERSION = 1

#: The counters a snapshot keeps for each statement.
_SNAPSHOT_COLUMNS = (
    "queryid",
    "userid",
    "dbid",
    "toplevel",
    "stats_since",
    "query",
    "calls",
    "total_exec_time",
    "rows",
    "shared_blks_hit",
    "shared_blks_read",
    "shared_blks_dirtied",
    "wal_bytes",
)


@dataclass(frozen=True)
class ChangeRule:
    """When a sampled statement is called changed rather than merely large.

    A statement that ranks high in a sample may always have; what is worth
    knowing first is the one that is new, or whose mean time moved. Both are
    inferred: from comparing the sample with the statement's own record before
    it, which is only a history when it is much longer than the sample. They
    are candidates; :meth:`explain`, or ``diagnose`` on the statement, confirm
    one.

    Attributes:
        sample_seconds: How long the sample was.
        window: How long pg_stat_statements had counted at the second snapshot.
        ratio: How far the mean has to move, as a factor either way.
        min_window_factor: How many samples long the record before the sample
            has to be.
    """

    sample_seconds: float
    window: CountingWindow
    ratio: float = DEFAULT_CHANGE_RATIO
    min_window_factor: float = MIN_WINDOW_FACTOR

    @property
    def needed_seconds(self) -> float:
        return self.sample_seconds * self.min_window_factor

    def refusal(self) -> str | None:
        """Why no statement in the sample can be judged, or None when they can."""
        seconds = self.window.seconds
        if seconds is None:
            return (
                "Whether a statement is new or changed is not judged: pg_stat_statements before 1.9 does not record "
                "when it was last reset, so how long the record the sample is compared with is cannot be told."
            )
        before = seconds - self.sample_seconds
        if before < self.needed_seconds:
            return (
                f"Whether a statement is new or changed is not judged: pg_stat_statements had counted for {max(before, 0):,.0f}s "
                f"before the sample, and a record {self.min_window_factor:g} times the {self.sample_seconds:g}s sample "
                f"({self.needed_seconds:,.0f}s) is needed to compare it with."
            )
        return None

    def judge(self, row: dict[str, Any], baseline: dict[str, Any] | None, delta: dict[str, Any], reset: bool) -> dict[str, Any] | None:
        """Whether one sampled statement is new or changed, with the figures; None when neither.

        Args:
            row: The statement in the second snapshot.
            baseline: The statement in the first, None when it was not there.
            delta: Its counters over the sample.
            reset: Whether its counters went backwards, so that it has no
                record before the sample to compare with.
        """
        if reset:
            return {"kind": None, "refused": "its counters started again during the sample, so it has no record before it to compare with"}
        if baseline is None:
            note = "not in pg_stat_statements before the sample"
            if self.window.evicted:
                note += (
                    f"; pg_stat_statements has evicted statements {self.window.evicted:,} times since its reset, "
                    "so it may have run before and been evicted"
                )
            return {"kind": "new", "confidence": Confidence.CANDIDATE.value, "reason": note}

        since = row.get("stats_since")
        if since is not None:
            # 1.11 dates each entry: one evicted and recorded again, or reset
            # alone, has a record shorter than the extension's window.
            counted = (self.window.now - since).total_seconds() - self.sample_seconds
            if counted < self.needed_seconds:
                return {
                    "kind": None,
                    "refused": (
                        f"its record before the sample is {max(counted, 0):,.0f}s long, under the {self.needed_seconds:,.0f}s "
                        "needed to compare the sample with"
                    ),
                }

        calls = baseline.get("calls") or 0
        if calls <= 0 or not delta.get("calls"):
            return None
        before_mean = (baseline.get("total_exec_time") or 0) / calls
        now_mean = delta["mean_exec_time"]
        if before_mean <= 0 or now_mean <= 0:
            return None
        factor = now_mean / before_mean
        if 1 / self.ratio < factor < self.ratio:
            return None
        kind = "slower" if factor > 1 else "faster"
        return {
            "kind": kind,
            "confidence": Confidence.CANDIDATE.value,
            "ratio": factor,
            "sample_mean_exec_time": now_mean,
            "sample_calls": delta["calls"],
            "previous_mean_exec_time": before_mean,
            "previous_calls": calls,
            "reason": (
                f"{now_mean:,.2f}ms a call over {delta['calls']:,} call(s) in the sample, against {before_mean:,.2f}ms "
                f"over its {calls:,} call(s) before it"
            ),
        }


@dataclass(frozen=True)
class QuerySample:
    """Query activity measured over an interval, and what the measurement had to leave out.

    Attributes:
        queries: The ranking, as :meth:`TopQueriesCalc.sample` returns it.
            Each carries ``change``: None, or a dict whose ``kind`` is
            ``new``, ``slower`` or ``faster`` (a candidate, with the figures),
            or None with ``refused`` saying why that statement could not be
            judged.
        withheld: How many of other roles' statements ran during the interval
            and are not in ``queries``: pg_stat_statements withholds their
            queryid and text from a role without pg_read_all_stats, so their
            counters cannot be followed from one snapshot to the next. 0 for a
            role shown everything.
        statement_window: How long pg_stat_statements had counted at the
            second snapshot (:meth:`CountingWindow.to_dict`).
        changes_refused: Why no statement was judged new or changed: the
            record before the sample is unknown, or not much longer than the
            sample. None when they were judged.
    """

    queries: list[dict[str, Any]]
    withheld: int = 0
    statement_window: dict[str, Any] | None = None
    changes_refused: str | None = None
    since: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QuerySnapshot:
    """pg_stat_statements' counters at one moment, to compare a later moment with.

    Saved to a file with :meth:`save` and read back with :meth:`load`, so the
    two moments can be a deploy apart, or a day, with nothing running in
    between: ``top-queries --save`` before, ``top-queries --compare`` after.

    Attributes:
        taken_at: The server's time when it was read.
        database: The database it was read in; a snapshot compares only with
            the same database.
        server_version: The server's major version.
        statements_since: When pg_stat_statements last started counting
            (``pg_stat_statements_info.stats_reset``, PostgreSQL 14+), or
            None when it does not record it.
        server_started: When the server last started
            (``pg_postmaster_start_time()``).
        rows: Each statement the role is shown the identity of, with its
            counters. Other roles' statements whose queryid is withheld are
            not kept: they cannot be matched with anything later.
        withheld: How many such statements there were.
    """

    taken_at: datetime
    database: str
    server_version: int
    statements_since: datetime | None
    server_started: datetime | None
    rows: list[dict[str, Any]]
    withheld: int = 0

    def to_dict(self) -> dict[str, Any]:
        def when(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "format": SNAPSHOT_FORMAT,
            "version": SNAPSHOT_VERSION,
            "taken_at": when(self.taken_at),
            "database": self.database,
            "server_version": self.server_version,
            "statements_since": when(self.statements_since),
            "server_started": when(self.server_started),
            "withheld": self.withheld,
            "rows": [{key: _number(value) for key, value in {**row, "stats_since": when(row.get("stats_since"))}.items()} for row in self.rows],
        }

    def save(self, path: str | Path) -> None:
        """Write the snapshot to a JSON file."""
        Path(path).write_text(json.dumps(self.to_dict(), indent=1, default=str) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "QuerySnapshot":
        """Read a snapshot :meth:`save` wrote.

        Raises:
            InvalidArgumentError: If the file cannot be read or is not a snapshot.
        """
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as e:
            raise InvalidArgumentError(f"Could not read the snapshot {path}: {e.strerror or e}.") from e
        except ValueError as e:
            raise InvalidArgumentError(f"{path} is not a top-queries snapshot: it is not JSON ({e}).") from e
        if not isinstance(data, dict) or data.get("format") != SNAPSHOT_FORMAT:
            raise InvalidArgumentError(f"{path} is not a top-queries snapshot; top-queries --save FILE writes one.")
        if data.get("version") != SNAPSHOT_VERSION:
            raise InvalidArgumentError(f"{path} is a version {data.get('version')} snapshot; this pg-debug reads version {SNAPSHOT_VERSION}.")

        def when(value: Any) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        rows = [{**row, "stats_since": when(row.get("stats_since"))} for row in data.get("rows") or []]
        return cls(
            taken_at=when(data["taken_at"]) or datetime.now().astimezone(),
            database=data.get("database") or "",
            server_version=int(data.get("server_version") or 0),
            statements_since=when(data.get("statements_since")),
            server_started=when(data.get("server_started")),
            rows=rows,
            withheld=int(data.get("withheld") or 0),
        )


def _wal_bytes(stats: StatementStatistics) -> str:
    return "wal_bytes" if stats.has_wal else "0 AS wal_bytes"


def _wal_bytes_frac(stats: StatementStatistics) -> str:
    if stats.has_wal:
        return "wal_bytes / NULLIF(SUM(wal_bytes) OVER (), 0) AS total_wal_bytes_frac"
    return "0 AS total_wal_bytes_frac"


class TopQueriesCalc:
    """Tool for retrieving the slowest SQL queries."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def _require_pg_stat_statements(self) -> StatementStatistics:
        """Find pg_stat_statements, or raise ExtensionRequiredError when it is not installed."""
        stats = await locate_pg_stat_statements(self.sql_driver)
        if stats is None:
            logger.warning("Extension %s is not installed", PG_STAT_STATEMENTS)
            raise ExtensionRequiredError(PG_STAT_STATEMENTS, install_pg_stat_statements_message)
        return stats

    async def sample(
        self,
        interval_seconds: float,
        sort_by: Literal["resources", "mean", "total"] = "total",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Measure query activity over an interval instead of since the last reset.

        pg_stat_statements counters are cumulative, so the usual ranking is
        dominated by whatever has run most since statistics were last reset —
        often a migration or a bulk load rather than the live workload. This
        takes two snapshots and reports the difference, which is what the
        database is doing *now*.

        Args:
            interval_seconds: How long to wait between the two snapshots.
                Longer samples are steadier; a few seconds is enough on a busy
                server.
            sort_by: ``total`` (default) or ``mean`` execution time, or
                ``resources`` to rank by a blend of time, buffer access and WAL
                volume, as the cumulative report does.
            limit: Number of queries to return.

        Returns:
            One dict per query that ran during the interval, with the counters
            accumulated over it. ``mean_exec_time`` is recomputed from the
            interval, not carried over from the cumulative view. Other roles'
            statements whose queryid pg_stat_statements withholds from the
            role are left out; :meth:`measure` says how many ran.

        Raises:
            ExtensionRequiredError: If pg_stat_statements is not installed.
            InvalidArgumentError: If the interval is not positive.
        """
        return (await self.measure(interval_seconds, sort_by=sort_by, limit=limit)).queries

    async def measure(
        self,
        interval_seconds: float,
        sort_by: Literal["resources", "mean", "total"] = "total",
        limit: int = 10,
    ) -> QuerySample:
        """The ranking :meth:`sample` returns, and how many statements it had to leave out.

        A role without pg_read_all_stats, which pg_monitor includes, is shown
        other roles' statements with their counters but neither their text nor
        their queryid. Without a queryid a statement cannot be followed from
        one snapshot to the next, so those are not ranked: subtracting one's
        counters from another's would report activity no statement had. They
        are counted instead: a statement that did not run is in both snapshots
        with the same counters, so the ones in the second snapshot that the
        first has no exact match for are the ones that ran. The resource
        shares of the ranked statements are shares of the ranked statements.

        Takes the same arguments, and raises the same errors, as
        :meth:`sample`.
        """
        if interval_seconds <= 0:
            raise InvalidArgumentError("The sampling interval must be greater than zero seconds.")

        stats = await self._require_pg_stat_statements()

        before = await self._counter_snapshot(stats)
        await asyncio.sleep(interval_seconds)
        after = await self._counter_snapshot(stats)
        window = await self._statement_window(stats)

        rule = ChangeRule(sample_seconds=interval_seconds, window=window)
        refused = rule.refusal()
        deltas = self._diff_snapshots(self._by_statement(before), self._by_statement(after), rule=None if refused else rule)
        return QuerySample(
            queries=self._rank(deltas, sort_by=sort_by, limit=limit),
            withheld=self._withheld_that_ran(before, after),
            statement_window=window.to_dict(),
            changes_refused=refused,
        )

    async def snapshot(self) -> QuerySnapshot:
        """Read pg_stat_statements' counters now, to compare a later moment with.

        Raises:
            ExtensionRequiredError: If pg_stat_statements is not installed.
        """
        stats = await self._require_pg_stat_statements()
        rows = await self._counter_snapshot(stats)
        window = await self._statement_window(stats)
        about = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            "SELECT current_database() AS database, current_setting('server_version_num')::int / 10000 AS version, "
            "pg_postmaster_start_time() AS started",
        )
        cells = about[0].cells if about else {}
        kept = [{column: row.get(column) for column in _SNAPSHOT_COLUMNS} for row in rows if row.get("queryid") is not None]
        return QuerySnapshot(
            taken_at=window.now,
            database=cells.get("database") or "",
            server_version=int(cells.get("version") or 0),
            statements_since=window.since,
            server_started=cells.get("started"),
            rows=[row for row in kept if not self._is_own_bookkeeping(row.get("query"))],
            withheld=sum(1 for row in rows if row.get("queryid") is None),
        )

    async def compare(
        self,
        snapshot: QuerySnapshot,
        sort_by: Literal["resources", "mean", "total"] = "total",
        limit: int = 10,
    ) -> QuerySample:
        """What ran between a saved snapshot and now, ranked as a sample ranks it.

        The same subtraction :meth:`measure` makes between two snapshots, with
        the first read from a file: each statement's counters since the
        snapshot, the statements that are new since, and the ones whose mean
        time moved by a factor of three either way against their mean before
        it, as candidates. A comparison asks for a record before the snapshot
        at least as long as the time since it.

        What moved the counters in between is said rather than mistaken for
        work: a pg_stat_statements reset since the snapshot means every
        statement counted again from zero, so each is measured from the reset
        and none is judged against its past; a server restart may have lost
        what was recorded since its last clean shutdown.

        Raises:
            ExtensionRequiredError: If pg_stat_statements is not installed.
            InvalidArgumentError: If the snapshot was taken in another
                database, or after now.
        """
        later = await self.snapshot()
        if snapshot.database != later.database:
            raise InvalidArgumentError(
                f"The snapshot was taken in database {snapshot.database!r}, and this is {later.database!r}: "
                "pg_stat_statements' counters only compare within one database."
            )
        elapsed = (later.taken_at - snapshot.taken_at).total_seconds()
        if elapsed <= 0:
            raise InvalidArgumentError(f"The snapshot was taken at {snapshot.taken_at.isoformat()}, which is not before now.")

        notes: list[str] = []
        before_rows = snapshot.rows
        reset_at = later.statements_since
        reset_after = reset_at is not None and reset_at > snapshot.taken_at
        if reset_at is not None and reset_after:
            notes.append(
                f"pg_stat_statements was reset at {reset_at.isoformat()}, after the snapshot: every statement "
                "counted again from zero then, so the figures are since the reset, and none is compared with its past."
            )
            before_rows = []
        elif later.statements_since is None:
            notes.append(
                "pg_stat_statements before 1.9 does not record when it was reset: a reset since the snapshot shows only as "
                "counters that went backwards, and a statement that ran more after a reset than before it would be measured short."
            )
        if snapshot.server_started and later.server_started and later.server_started > snapshot.server_started:
            notes.append(
                f"The server restarted at {later.server_started.isoformat()}, after the snapshot. A clean shutdown keeps "
                "pg_stat_statements' counters; a crash, an immediate shutdown or a failover loses what was recorded since the last clean one."
            )
        if snapshot.server_version and later.server_version and snapshot.server_version != later.server_version:
            notes.append(f"The server was PostgreSQL {snapshot.server_version} at the snapshot and is {later.server_version} now.")

        window = CountingWindow(
            since=later.statements_since if not reset_after else None, now=later.taken_at, began_with="reset" if later.statements_since else None
        )
        rule = ChangeRule(sample_seconds=elapsed, window=window, min_window_factor=SNAPSHOT_WINDOW_FACTOR)
        refused = None if reset_after else rule.refusal()
        deltas = self._diff_snapshots(self._by_statement(before_rows), self._by_statement(later.rows), rule=None if refused or reset_after else rule)
        return QuerySample(
            queries=self._rank(deltas, sort_by=sort_by, limit=limit),
            withheld=0,
            statement_window=window.to_dict(),
            changes_refused=refused,
            since=snapshot.taken_at.isoformat(),
            notes=notes,
        )

    async def _statement_window(self, stats: StatementStatistics) -> CountingWindow:
        """How long pg_stat_statements has counted: the record a sample is compared with."""
        return await statement_window(self.sql_driver, stats)

    @staticmethod
    def _by_statement(rows: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
        """The rows the role is shown the identity of, keyed by it.

        A statement run inside a function and the same statement run at top
        level are two rows with one queryid, user and database, told apart by
        ``toplevel`` (None before pg_stat_statements 1.9, which has one row).
        """
        return {(row["queryid"], row["userid"], row["dbid"], row.get("toplevel")): row for row in rows if row.get("queryid") is not None}

    @staticmethod
    def _withheld_that_ran(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> int:
        """How many of the statements whose queryid is withheld ran between the two snapshots.

        One that did not run is in both with the same user, database and
        counters, so it cancels out; one that ran, or is new, is in the second
        with counters no row of the first has. The count can only fall short,
        and only by a statement whose counters after the interval equal, to the
        microsecond of execution time, another's before it.
        """

        def withheld(rows: list[dict[str, Any]]) -> Counter[tuple[Any, ...]]:
            return Counter(tuple(row.get(field) for field in _WITHHELD_IDENTITY) for row in rows if row.get("queryid") is None)

        ran = withheld(after) - withheld(before)
        calls = _WITHHELD_IDENTITY.index("calls")
        # An entry is made when a statement is parsed, before it has run.
        return sum(count for counters, count in ran.items() if (counters[calls] or 0) > 0)

    async def _counter_snapshot(self, stats: StatementStatistics) -> list[dict[str, Any]]:
        """Read the raw per-query counters, one row per pg_stat_statements entry of this database."""

        # The driver marks this — and everything else it runs — so the snapshot
        # query is filtered out of its own results by _is_own_bookkeeping.
        query = cast(
            LiteralString,
            f"""
            SELECT
                queryid,
                userid,
                dbid,
                {"toplevel" if stats.has_toplevel else "NULL AS toplevel"},
                {"stats_since" if stats.has_stats_since else "NULL::timestamptz AS stats_since"},
                query,
                calls,
                {stats.total_time} AS total_exec_time,
                rows,
                shared_blks_hit,
                shared_blks_read,
                shared_blks_dirtied,
                {_wal_bytes(stats)}
            FROM {stats.relation}
            WHERE {THIS_DATABASE}
            """,
        )
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query)
        return [dict(r.cells) for r in rows] if rows else []

    @staticmethod
    def _is_own_bookkeeping(query: Any) -> bool:
        """True for statements this library issues rather than the workload.

        Transaction control is filtered too: it is never the answer to "what is
        expensive", and in restricted mode the read-only transaction wrapper is
        our own doing.
        """
        text = (query or "").strip()
        if QUERY_MARKER in text:
            return True
        return text.upper().rstrip(";").split(" ", 1)[0] in _TRANSACTION_CONTROL

    @staticmethod
    def _diff_snapshots(
        before: dict[Any, dict[str, Any]],
        after: dict[Any, dict[str, Any]],
        rule: ChangeRule | None = None,
    ) -> list[dict[str, Any]]:
        """Subtract two snapshots, keeping only queries that ran in between.

        The snapshots are keyed by statement, as :meth:`_by_statement` keys
        them: a statement whose queryid is withheld is not one statement in
        either, has no delta, and is counted by :meth:`_withheld_that_ran`.

        Each delta carries ``change``, as :meth:`ChangeRule.judge` judges it
        when a rule is given, and None otherwise.
        """
        counters = ("calls", "total_exec_time", "rows", "shared_blks_hit", "shared_blks_read", "shared_blks_dirtied", "wal_bytes")
        deltas: list[dict[str, Any]] = []

        for key, row in after.items():
            # Reading the statistics registers queries of its own — the
            # snapshot itself, and the transaction control around it in
            # restricted mode. Reporting those back would be noise.
            if TopQueriesCalc._is_own_bookkeeping(row.get("query")):
                continue

            baseline = before.get(key)
            # A query missing from the first snapshot is new; one whose counters
            # went backwards means pg_stat_statements was reset or the entry was
            # evicted and reused. Both are measured from zero.
            reset = False
            if baseline is not None and (row["calls"] or 0) < (baseline["calls"] or 0):
                logger.debug("pg_stat_statements counters went backwards for %s; treating the entry as new", key)
                baseline = None
                reset = True

            delta = {"query": row["query"], "queryid": row["queryid"], "tags": parse_sql_comment(row.get("query"))}
            for counter in counters:
                current = row.get(counter) or 0
                previous = (baseline or {}).get(counter) or 0
                delta[counter] = current - previous

            if delta["calls"] <= 0:
                continue

            delta["mean_exec_time"] = delta["total_exec_time"] / delta["calls"] if delta["calls"] else 0.0
            delta["change"] = rule.judge(row, baseline, delta, reset) if rule is not None else None
            deltas.append(delta)

        return deltas

    @staticmethod
    def _rank(deltas: list[dict[str, Any]], sort_by: str, limit: int) -> list[dict[str, Any]]:
        """Order sampled queries by the requested criterion."""
        if sort_by == "resources":
            # Mirror the cumulative report: rank by the largest share of any one
            # resource, so a query that dominates I/O ranks with one that
            # dominates time.
            totals = {
                key: sum(d[key] for d in deltas) or 1
                for key in ("total_exec_time", "shared_blks_hit", "shared_blks_read", "shared_blks_dirtied", "wal_bytes")
            }
            for delta in deltas:
                delta["resource_fraction"] = max(delta[key] / totals[key] for key in totals)
            deltas.sort(key=lambda d: d["resource_fraction"], reverse=True)
        else:
            deltas.sort(key=lambda d: d["total_exec_time" if sort_by == "total" else "mean_exec_time"], reverse=True)

        return deltas[:limit]

    async def get_top_queries_by_time(self, limit: int = 10, sort_by: Literal["total", "mean"] = "mean") -> list[dict[str, Any]]:
        """Reports the slowest SQL queries based on execution time.

        Args:
            limit: Number of slow queries to return
            sort_by: Sort criteria - 'total' for total execution time or
                'mean' for mean execution time per call (default)

        Returns:
            One dict per query with its query text, call count, total and mean
            execution time, and row count.

        Raises:
            ExtensionRequiredError: If pg_stat_statements is not installed.
        """
        logger.debug("Getting top queries by time. limit=%s, sort_by=%s", limit, sort_by)
        cols = await self._require_pg_stat_statements()

        # Determine which column to sort by based on sort_by parameter
        order_by_column = cols.total_time if sort_by == "total" else cols.mean_time

        query = cast(
            LiteralString,
            f"""
                SELECT
                    query,
                    calls,
                    {cols.total_time},
                    {cols.mean_time},
                    rows
                FROM {cols.relation}
                WHERE {THIS_DATABASE} AND {_workload_only()}
                ORDER BY {order_by_column} DESC
                LIMIT {{}};
            """,
        )
        logger.debug("Executing query: %s", query)
        slow_query_rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            query,
            [limit],
        )
        slow_queries = [_tagged(row.cells) for row in slow_query_rows] if slow_query_rows else []
        logger.info("Found %s slow queries", len(slow_queries))
        return slow_queries

    async def get_top_resource_queries(self, frac_threshold: float = 0.05, limit: int | None = None) -> list[dict[str, Any]]:
        """Reports the most time consuming queries based on a resource blend.

        A query is reported when it accounts for more than ``frac_threshold`` of
        total execution time, buffer access, buffer reads, dirtied buffers or
        WAL bytes across the whole workload.

        Args:
            frac_threshold: Fraction threshold for filtering queries (default: 0.05)
            limit: How many to return, those with the largest share of any one
                resource first; None returns every query over the threshold.

        Returns:
            One dict per query with its execution, buffer and WAL statistics.

        Raises:
            ExtensionRequiredError: If pg_stat_statements is not installed.
        """
        logger.debug("Getting top resource queries with threshold %s", frac_threshold)
        cols = await self._require_pg_stat_statements()

        query = cast(
            LiteralString,
            f"""
                WITH resource_fractions AS (
                    SELECT
                        query,
                        calls,
                        rows,
                        {cols.total_time} AS total_exec_time,
                        {cols.mean_time} AS mean_exec_time,
                        {cols.stddev_time} AS stddev_exec_time,
                        shared_blks_hit,
                        shared_blks_read,
                        shared_blks_dirtied,
                        {_wal_bytes(cols)},
                        {cols.total_time} / NULLIF(SUM({cols.total_time}) OVER (), 0)
                            AS total_exec_time_frac,
                        (shared_blks_hit + shared_blks_read)
                            / NULLIF(SUM(shared_blks_hit + shared_blks_read) OVER (), 0)
                            AS shared_blks_accessed_frac,
                        shared_blks_read / NULLIF(SUM(shared_blks_read) OVER (), 0)
                            AS shared_blks_read_frac,
                        shared_blks_dirtied / NULLIF(SUM(shared_blks_dirtied) OVER (), 0)
                            AS shared_blks_dirtied_frac,
                        {_wal_bytes_frac(cols)}
                    FROM {cols.relation}
                    -- Before the window functions: our own queries, and
                    -- another database's, in the denominator would dilute
                    -- every fraction below.
                    WHERE {THIS_DATABASE} AND {_workload_only()}
                )
                SELECT
                    query,
                    calls,
                    rows,
                    total_exec_time,
                    mean_exec_time,
                    stddev_exec_time,
                    total_exec_time_frac,
                    shared_blks_accessed_frac,
                    shared_blks_read_frac,
                    shared_blks_dirtied_frac,
                    total_wal_bytes_frac,
                    shared_blks_hit,
                    shared_blks_read,
                    shared_blks_dirtied,
                    wal_bytes
                FROM resource_fractions
                WHERE
                    total_exec_time_frac > {frac_threshold}
                    OR shared_blks_accessed_frac > {frac_threshold}
                    OR shared_blks_read_frac > {frac_threshold}
                    OR shared_blks_dirtied_frac > {frac_threshold}
                    OR total_wal_bytes_frac > {frac_threshold}
                -- Ranked as a sample ranks them: by the largest share of
                -- any one resource, so a query that dominates I/O ranks with
                -- one that dominates time.
                ORDER BY GREATEST(
                    COALESCE(total_exec_time_frac, 0),
                    COALESCE(shared_blks_accessed_frac, 0),
                    COALESCE(shared_blks_read_frac, 0),
                    COALESCE(shared_blks_dirtied_frac, 0),
                    COALESCE(total_wal_bytes_frac, 0)
                ) DESC, total_exec_time DESC
                {"" if limit is None else f"LIMIT {int(limit)}"}
            """,
        )

        logger.debug("Executing query: %s", query)
        slow_query_rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            query,
        )
        resource_queries = [_tagged(row.cells) for row in slow_query_rows] if slow_query_rows else []
        logger.info("Found %s resource-intensive queries", len(resource_queries))
        return resource_queries


def _tagged(row: dict[str, Any]) -> dict[str, Any]:
    """The row with the sqlcommenter tags of its text.

    pg_stat_statements groups a statement's runs with their comments ignored,
    and keeps the text of the run it recorded first: the tags are that
    caller's, not every caller's.
    """
    return {**row, "tags": parse_sql_comment(row.get("query"))}


def _number(value: Any) -> Any:
    """A numeric counter as JSON holds it: wal_bytes is numeric, which psycopg reads as a Decimal."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value

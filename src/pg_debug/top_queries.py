import asyncio
import logging
from collections import Counter
from dataclasses import asdict
from dataclasses import dataclass
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

            delta = {"query": row["query"], "queryid": row["queryid"]}
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
        slow_queries = [row.cells for row in slow_query_rows] if slow_query_rows else []
        logger.info("Found %s slow queries", len(slow_queries))
        return slow_queries

    async def get_top_resource_queries(self, frac_threshold: float = 0.05) -> list[dict[str, Any]]:
        """Reports the most time consuming queries based on a resource blend.

        A query is reported when it accounts for more than ``frac_threshold`` of
        total execution time, buffer access, buffer reads, dirtied buffers or
        WAL bytes across the whole workload.

        Args:
            frac_threshold: Fraction threshold for filtering queries (default: 0.05)

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
                ORDER BY total_exec_time DESC
            """,
        )

        logger.debug("Executing query: %s", query)
        slow_query_rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            query,
        )
        resource_queries = [row.cells for row in slow_query_rows] if slow_query_rows else []
        logger.info("Found %s resource-intensive queries", len(resource_queries))
        return resource_queries

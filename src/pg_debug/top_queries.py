import asyncio
import logging
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
from .db.extensions import check_extension
from .db.extensions import get_postgres_version
from .errors import ExtensionRequiredError
from .errors import InvalidArgumentError

logger = logging.getLogger(__name__)

PG_STAT_STATEMENTS = "pg_stat_statements"


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


@dataclass
class PgStatStatementsColumns:
    """Column names for pg_stat_statements view, which vary by PostgreSQL version."""

    total_time: str
    mean_time: str
    stddev_time: str
    wal_bytes_select: str  # Full SELECT expression (handles missing column in PG12)
    wal_bytes_frac: str  # Full fraction expression


def _get_pg_stat_statements_columns(pg_version: int) -> PgStatStatementsColumns:
    """Get pg_stat_statements column names based on PostgreSQL version.

    PostgreSQL 13 introduced pg_stat_statements v2.0 with breaking changes:
    - Renamed timing columns: *_time → *_exec_time (total_time → total_exec_time, etc.)
    - Added wal_bytes column for write-ahead log tracking

    This function provides version-appropriate column names to ensure compatibility
    with both old (PG ≤ 12) and new (PG ≥ 13) versions.

    See: https://www.postgresql.org/docs/13/release-13.html#id-1.11.6.11.4

    Args:
        pg_version: PostgreSQL major version number

    Returns:
        PgStatStatementsColumns with version-appropriate column names
    """
    if pg_version >= 13:
        # PostgreSQL 13+ with pg_stat_statements v2.0
        return PgStatStatementsColumns(
            total_time="total_exec_time",
            mean_time="mean_exec_time",
            stddev_time="stddev_exec_time",
            wal_bytes_select="wal_bytes",
            wal_bytes_frac="wal_bytes / NULLIF(SUM(wal_bytes) OVER (), 0) AS total_wal_bytes_frac",
        )

    # PostgreSQL 12 and older with pg_stat_statements v1.x
    return PgStatStatementsColumns(
        total_time="total_time",
        mean_time="mean_time",
        stddev_time="stddev_time",
        wal_bytes_select="0 AS wal_bytes",  # Column doesn't exist in PG12
        wal_bytes_frac="0 AS total_wal_bytes_frac",
    )


class TopQueriesCalc:
    """Tool for retrieving the slowest SQL queries."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def _require_pg_stat_statements(self) -> None:
        """Raise ExtensionRequiredError if pg_stat_statements is not installed."""
        extension_status = await check_extension(
            self.sql_driver,
            PG_STAT_STATEMENTS,
            include_messages=False,
        )
        if not extension_status.is_installed:
            logger.warning(f"Extension {PG_STAT_STATEMENTS} is not installed")
            raise ExtensionRequiredError(PG_STAT_STATEMENTS, install_pg_stat_statements_message)

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
            interval, not carried over from the cumulative view.

        Raises:
            ExtensionRequiredError: If pg_stat_statements is not installed.
            InvalidArgumentError: If the interval is not positive.
        """
        if interval_seconds <= 0:
            raise InvalidArgumentError("The sampling interval must be greater than zero seconds.")

        await self._require_pg_stat_statements()

        before = await self._counter_snapshot()
        await asyncio.sleep(interval_seconds)
        after = await self._counter_snapshot()

        deltas = self._diff_snapshots(before, after)
        return self._rank(deltas, sort_by=sort_by, limit=limit)

    async def _counter_snapshot(self) -> dict[Any, dict[str, Any]]:
        """Read the raw per-query counters, keyed by query identity."""
        pg_version = await get_postgres_version(self.sql_driver)
        cols = _get_pg_stat_statements_columns(pg_version)

        # The driver marks this — and everything else it runs — so the snapshot
        # query is filtered out of its own results by _is_own_bookkeeping.
        query = cast(
            LiteralString,
            f"""
            SELECT
                queryid,
                userid,
                dbid,
                query,
                calls,
                {cols.total_time} AS total_exec_time,
                rows,
                shared_blks_hit,
                shared_blks_read,
                shared_blks_dirtied,
                {cols.wal_bytes_select}
            FROM pg_stat_statements
            """,
        )
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query)
        return {(r.cells["queryid"], r.cells["userid"], r.cells["dbid"]): dict(r.cells) for r in rows} if rows else {}

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
    def _diff_snapshots(before: dict[Any, dict[str, Any]], after: dict[Any, dict[str, Any]]) -> list[dict[str, Any]]:
        """Subtract two snapshots, keeping only queries that ran in between."""
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
            if baseline is not None and (row["calls"] or 0) < (baseline["calls"] or 0):
                logger.debug("pg_stat_statements counters went backwards for %s; treating the entry as new", key)
                baseline = None

            delta = {"query": row["query"], "queryid": row["queryid"]}
            for counter in counters:
                current = row.get(counter) or 0
                previous = (baseline or {}).get(counter) or 0
                delta[counter] = current - previous

            if delta["calls"] <= 0:
                continue

            delta["mean_exec_time"] = delta["total_exec_time"] / delta["calls"] if delta["calls"] else 0.0
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
        logger.debug(f"Getting top queries by time. limit={limit}, sort_by={sort_by}")
        await self._require_pg_stat_statements()

        # Get version-appropriate column names
        pg_version = await get_postgres_version(self.sql_driver)
        logger.debug(f"PostgreSQL version: {pg_version}")
        cols = _get_pg_stat_statements_columns(pg_version)

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
                FROM pg_stat_statements
                WHERE {_workload_only()}
                ORDER BY {order_by_column} DESC
                LIMIT {{}};
            """,
        )
        logger.debug(f"Executing query: {query}")
        slow_query_rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            query,
            [limit],
        )
        slow_queries = [row.cells for row in slow_query_rows] if slow_query_rows else []
        logger.info(f"Found {len(slow_queries)} slow queries")
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
        logger.debug(f"Getting top resource queries with threshold {frac_threshold}")
        await self._require_pg_stat_statements()

        # Get version-appropriate column names
        pg_version = await get_postgres_version(self.sql_driver)
        logger.debug(f"PostgreSQL version: {pg_version}")
        cols = _get_pg_stat_statements_columns(pg_version)

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
                        {cols.wal_bytes_select},
                        {cols.total_time} / NULLIF(SUM({cols.total_time}) OVER (), 0)
                            AS total_exec_time_frac,
                        (shared_blks_hit + shared_blks_read)
                            / NULLIF(SUM(shared_blks_hit + shared_blks_read) OVER (), 0)
                            AS shared_blks_accessed_frac,
                        shared_blks_read / NULLIF(SUM(shared_blks_read) OVER (), 0)
                            AS shared_blks_read_frac,
                        shared_blks_dirtied / NULLIF(SUM(shared_blks_dirtied) OVER (), 0)
                            AS shared_blks_dirtied_frac,
                        {cols.wal_bytes_frac}
                    FROM pg_stat_statements
                    -- Before the window functions: our own queries in the
                    -- denominator would dilute every fraction below.
                    WHERE {_workload_only()}
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

        logger.debug(f"Executing query: {query}")
        slow_query_rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            query,
        )
        resource_queries = [row.cells for row in slow_query_rows] if slow_query_rows else []
        logger.info(f"Found {len(resource_queries)} resource-intensive queries")
        return resource_queries

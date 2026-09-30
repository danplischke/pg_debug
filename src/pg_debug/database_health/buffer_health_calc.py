"""Cache hit rates, and when they are worth reading at all.

A hit rate is a ratio, and a ratio over a handful of block accesses describes
the last few minutes rather than the server. A database that has just started,
or one whose statistics were just reset, reports a terrible hit rate for as
long as it takes to read each page once — and "0.0% (below 95.0% threshold)"
against 1,000 cold reads is a false alarm that sends someone to resize
shared_buffers on a server with nothing wrong with it.

So the same floor the configuration review applies is applied here, and from
the same constant: two parts of one tool answering the same question from the
same counters must not reach different verdicts.
"""

from typing import Any

from ..configuration import MIN_BLOCK_READS
from ..db import SqlDriver
from .result import HealthCheckResult

INDEX_HIT_RATE = "Buffer health for indexes"
TABLE_HIT_RATE = "Buffer health for tables"

INDEX_HIT_RATE_SQL = """
    SELECT
        sum(idx_blks_hit) AS hits,
        sum(idx_blks_read) AS reads,
        sum(idx_blks_hit) / nullif(sum(idx_blks_hit + idx_blks_read), 0) AS rate
    FROM pg_statio_user_indexes
"""

TABLE_HIT_RATE_SQL = """
    SELECT
        sum(heap_blks_hit) AS hits,
        sum(heap_blks_read) AS reads,
        sum(heap_blks_hit) / nullif(sum(heap_blks_hit + heap_blks_read), 0) AS rate
    FROM pg_statio_user_tables
"""


class BufferHealthCalc:
    _cached_indexes: list[dict[str, Any]] | None = None

    def __init__(self, sql_driver: SqlDriver):
        self.sql_driver = sql_driver

    async def index_hit_rate(self, threshold: float = 0.95) -> HealthCheckResult:
        """Calculate the index cache hit rate.

        Returns:
            The hit rate as a percentage, compared against the threshold, or a
            statement that too little has been read to judge it.
        """
        result = await self.sql_driver.execute_query(INDEX_HIT_RATE_SQL)

        return self._hit_rate_result(INDEX_HIT_RATE, "Index", result, threshold)

    async def table_hit_rate(self, threshold: float = 0.95) -> HealthCheckResult:
        """Calculate the table cache hit rate.

        Returns:
            The hit rate as a percentage, compared against the threshold, or a
            statement that too little has been read to judge it.
        """
        result = await self.sql_driver.execute_query(TABLE_HIT_RATE_SQL)

        return self._hit_rate_result(TABLE_HIT_RATE, "Table", result, threshold)

    @staticmethod
    def _hit_rate_result(name: str, subject: str, rows: Any, threshold: float) -> HealthCheckResult:
        result_list = [dict(x.cells) for x in rows] if rows else []
        threshold_pct = threshold * 100

        if not result_list or result_list[0]["rate"] is None:
            return HealthCheckResult(
                name=name,
                healthy=True,
                summary=f"No {subject.lower()} cache statistics available.",
                metrics={"hit_rate_percent": None, "threshold_percent": threshold_pct},
            )

        row = result_list[0]
        hits = int(row["hits"] or 0)
        reads = int(row["reads"] or 0)
        hit_rate = float(row["rate"]) * 100
        metrics = {
            "hit_rate_percent": hit_rate,
            "threshold_percent": threshold_pct,
            "blocks_hit": hits,
            "blocks_read": reads,
            "minimum_blocks_to_judge": MIN_BLOCK_READS,
        }

        if hits + reads < MIN_BLOCK_READS:
            # Healthy, because the alternative is reporting a server as unwell
            # on the strength of a number that is not about the server.
            return HealthCheckResult(
                name=name,
                healthy=True,
                summary=(
                    f"{subject} cache hit rate: not enough read to judge — {hits + reads:,} blocks accessed, "
                    f"against the {MIN_BLOCK_READS:,} it takes for the ratio to mean anything."
                ),
                metrics=metrics,
            )

        healthy = hit_rate >= threshold_pct
        comparison = "above" if healthy else "below"

        return HealthCheckResult(
            name=name,
            healthy=healthy,
            summary=f"{subject} cache hit rate: {hit_rate:.1f}% ({comparison} {threshold_pct:.1f}% threshold) over {hits + reads:,} blocks",
            metrics=metrics,
        )

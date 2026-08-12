from typing import Any

from ..sql import SqlDriver
from .result import HealthCheckResult

INDEX_HIT_RATE = "Buffer health for indexes"
TABLE_HIT_RATE = "Buffer health for tables"


class BufferHealthCalc:
    _cached_indexes: list[dict[str, Any]] | None = None

    def __init__(self, sql_driver: SqlDriver):
        self.sql_driver = sql_driver

    async def index_hit_rate(self, threshold: float = 0.95) -> HealthCheckResult:
        """Calculate the index cache hit rate.

        Returns:
            The hit rate as a percentage, compared against the threshold.
        """
        result = await self.sql_driver.execute_query("""
            SELECT
                (sum(idx_blks_hit)) / nullif(sum(idx_blks_hit + idx_blks_read), 0) AS rate
            FROM
                pg_statio_user_indexes
        """)

        return self._hit_rate_result(INDEX_HIT_RATE, "Index", result, threshold)

    async def table_hit_rate(self, threshold: float = 0.95) -> HealthCheckResult:
        """Calculate the table cache hit rate.

        Returns:
            The hit rate as a percentage, compared against the threshold.
        """
        result = await self.sql_driver.execute_query("""
            SELECT
                sum(heap_blks_hit) / nullif(sum(heap_blks_hit + heap_blks_read), 0) AS rate
            FROM
                pg_statio_user_tables
        """)

        return self._hit_rate_result(TABLE_HIT_RATE, "Table", result, threshold)

    @staticmethod
    def _hit_rate_result(name: str, subject: str, rows: Any, threshold: float) -> HealthCheckResult:
        result_list = [dict(x.cells) for x in rows] if rows else []

        if not result_list or result_list[0]["rate"] is None:
            return HealthCheckResult(
                name=name,
                healthy=True,
                summary=f"No {subject.lower()} cache statistics available.",
                metrics={"hit_rate_percent": None, "threshold_percent": threshold * 100},
            )

        hit_rate = float(result_list[0]["rate"]) * 100
        threshold_pct = threshold * 100
        healthy = hit_rate >= threshold_pct
        comparison = "above" if healthy else "below"

        return HealthCheckResult(
            name=name,
            healthy=healthy,
            summary=f"{subject} cache hit rate: {hit_rate:.1f}% ({comparison} {threshold_pct:.1f}% threshold)",
            metrics={"hit_rate_percent": hit_rate, "threshold_percent": threshold_pct},
        )

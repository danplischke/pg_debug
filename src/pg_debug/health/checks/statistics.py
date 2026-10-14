"""Health check for planner statistics freshness."""

from typing import cast

import humanize

from ...db import SafeSqlDriver
from ...db import SqlDriver
from ...db.window import table_window
from ...statistics import DEFAULT_MIN_CHANGED_ROWS
from ...statistics import DEFAULT_STALE_FRACTION
from ...statistics import StatisticsInspector
from ..result import HealthCheckResult

STATISTICS_HEALTH = "Statistics health"


class StatisticsHealthCalc:
    """Flags tables the planner is reasoning about with outdated numbers."""

    def __init__(
        self,
        sql_driver: SqlDriver | SafeSqlDriver,
        stale_fraction: float = DEFAULT_STALE_FRACTION,
        min_changed_rows: int = DEFAULT_MIN_CHANGED_ROWS,
    ):
        self.sql_driver = sql_driver
        self.stale_fraction = stale_fraction
        self.min_changed_rows = min_changed_rows

    async def stale_statistics_check(self) -> HealthCheckResult:
        """Report tables that have never been analyzed or have drifted since.

        Stale statistics make the planner misjudge row counts, which is a more
        common cause of a bad plan than a missing index — and cheaper to fix.
        """
        thresholds = {
            "stale_fraction": self.stale_fraction,
            "min_changed_rows": self.min_changed_rows,
        }
        tables = await StatisticsInspector(self.sql_driver).stale_tables(
            stale_fraction=self.stale_fraction,
            min_changed_rows=self.min_changed_rows,
        )
        # Drift is read from the counters, which start again at a crash or a
        # reset; whether a table has statistics at all is not.
        window = await table_window(cast(SqlDriver, self.sql_driver))
        metrics = {**thresholds, **window.to_dict()}

        if not tables:
            return HealthCheckResult(
                name=STATISTICS_HEALTH,
                healthy=True,
                summary=f"All tables have current statistics, judging drift by the changes counted {window.describe()}.",
                metrics=metrics,
            )

        lines = [f"Tables with outdated statistics, judging drift by the changes counted {window.describe()}:"]
        for table in tables:
            qualified = f"{table['schema']}.{table['table']}"
            if table["never_analyzed"]:
                size = humanize.naturalsize(table.get("size_bytes") or 0, binary=True)
                lines.append(f"Table '{qualified}' has no planner statistics: it has never been analyzed with rows in it ({size})")
            else:
                fraction = (table["changed_fraction"] or 0) * 100
                lines.append(f"Table '{qualified}' has {table['changed_rows']:,} rows changed since its last ANALYZE ({fraction:.1f}% of the table)")

        return HealthCheckResult(
            name=STATISTICS_HEALTH,
            healthy=False,
            summary="\n".join(lines),
            findings=tables,
            metrics=metrics,
        )


__all__ = ["STATISTICS_HEALTH", "StatisticsHealthCalc"]

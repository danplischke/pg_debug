"""Table bloat estimated from the catalogs.

Dead tuples that VACUUM has reclaimed leave free space behind. That space is
reused by later inserts, so some of it is normal and healthy; a table only
needs attention when a large share of its heap is space no live row occupies.

The estimate compares the pages a table actually occupies (``pg_class.relpages``)
against the pages its live rows would need. That second number comes from
:mod:`pg_debug.tuple_layout`, which builds a row the way PostgreSQL does —
padding each column forward to its type's alignment boundary — and is shared
with the data-model review so the two cannot drift apart. Skipping the padding,
as most catalog bloat queries do, understates the row width and so reports
bloat on tables that have none: on a six-column table used in testing here it
accounted for 11 bytes a row, a phantom 7.5%.

It needs no extension and reads only planner statistics, so it is cheap enough
to run against a production database. What it cannot see is the *spread* of a
variable-width column: ``pg_stats`` records one average width, and a table whose
rows straddle a MAXALIGN boundary packs fewer rows per page than that average
suggests. Such a table reads a few percent bloated when it is not, which is part
of why a table has to cross a minimum *share* as well as a minimum size before
it is flagged. ``pgstattuple`` measures the real figure by scanning the table,
which is exact and far more expensive.
"""

from typing import cast

from typing_extensions import LiteralString

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..tuple_layout import fillfactor_expr
from ..tuple_layout import row_layout_ctes
from ..tuple_layout import rows_per_page_expr
from .result import HealthCheckResult

TABLE_BLOAT = "Table bloat"

#: Ignore anything wasting less than this, matching the index bloat check.
DEFAULT_MIN_BLOAT_BYTES = 100 * 1024 * 1024

#: And ignore tables where the waste is a small share of the whole, so a large
#: healthy table is not flagged for its ordinary free space.
DEFAULT_MIN_BLOAT_PERCENT = 20.0

#: A table with no pages holds nothing to be bloated by.
_RELATION_FILTER = "c.relkind IN ('r', 'm') AND c.reltuples > 0 AND c.relpages > 0"

TABLE_BLOAT_SQL = f"""
WITH RECURSIVE {row_layout_ctes(_RELATION_FILTER)},
estimated AS (
    SELECT
        n.nspname AS schema,
        c.relname AS table,
        c.reltuples::numeric AS live_rows,
        c.relpages AS actual_pages,
        current_setting('block_size')::numeric AS block_size,
        s.row_bytes,
        {fillfactor_expr()} AS fillfactor
    FROM row_shapes s
    JOIN pg_class c ON c.oid = s.table_oid
    JOIN pg_namespace n ON n.oid = c.relnamespace
),
packed AS (
    SELECT
        e.*,
        {rows_per_page_expr("e.row_bytes", "e.fillfactor", "e.block_size")} AS rows_per_page
    FROM estimated e
),
sized AS (
    SELECT p.*, ceil(p.live_rows / p.rows_per_page) AS expected_pages
    FROM packed p
)
SELECT
    schema,
    "table",
    (actual_pages - expected_pages) * block_size AS bloat_bytes,
    actual_pages * block_size AS table_bytes,
    round((actual_pages - expected_pages) * 100.0 / actual_pages, 1) AS bloat_percent,
    live_rows,
    actual_pages,
    expected_pages,
    row_bytes,
    rows_per_page,
    fillfactor
FROM sized
WHERE actual_pages > expected_pages
  AND (actual_pages - expected_pages) * block_size >= {{}}
  AND (actual_pages - expected_pages) * 100.0 / actual_pages >= {{}}
ORDER BY (actual_pages - expected_pages) DESC, schema, "table"
"""


class TableBloatCalc:
    """Estimates how much of each table's heap holds no live rows."""

    def __init__(self, sql_driver: SqlDriver):
        self.sql_driver = sql_driver

    async def table_bloat_check(
        self,
        min_bloat_bytes: int = DEFAULT_MIN_BLOAT_BYTES,
        min_bloat_percent: float = DEFAULT_MIN_BLOAT_PERCENT,
    ) -> HealthCheckResult:
        """Find tables whose heap is substantially larger than their live rows need.

        A table has to cross *both* thresholds to be reported: a big table with a
        little free space is fine, and so is a small table that is mostly free
        space.

        Args:
            min_bloat_bytes: Minimum wasted bytes before a table is reported.
            min_bloat_percent: Minimum share of the table that must be wasted.

        Returns:
            The bloated tables found, one finding each.
        """
        metrics = {
            "min_bloat_bytes": min_bloat_bytes,
            "min_bloat_percent": min_bloat_percent,
        }

        rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            # The only things interpolated into the statement are the page
            # layout constants above; the thresholds go through {} as literals.
            cast(LiteralString, TABLE_BLOAT_SQL),
            [min_bloat_bytes, min_bloat_percent],
        )

        if not rows:
            return HealthCheckResult(
                name=TABLE_BLOAT,
                healthy=True,
                summary="No bloated tables found.",
                metrics=metrics,
            )

        findings = [dict(row.cells) for row in rows]

        lines = ["Bloated tables found:"]
        for finding in findings:
            bloat_mb = float(finding["bloat_bytes"]) / (1024 * 1024)
            total_mb = float(finding["table_bytes"]) / (1024 * 1024)
            lines.append(
                f"Table '{finding['schema']}.{finding['table']}' has {bloat_mb:.1f}MB bloat "
                f"out of {total_mb:.1f}MB total size ({float(finding['bloat_percent']):.1f}%)"
            )

        return HealthCheckResult(
            name=TABLE_BLOAT,
            healthy=False,
            summary="\n".join(lines),
            findings=findings,
            metrics=metrics,
        )

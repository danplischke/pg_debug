"""Table bloat estimated from the catalogs.

Dead tuples that VACUUM has reclaimed leave free space behind. That space is
reused by later inserts, so some of it is normal and healthy; a table only
needs attention when a large share of its heap is space no live row occupies.

The estimate compares the pages a table actually occupies (``pg_class.relpages``)
against the pages its live rows would need. That second number is built the way
PostgreSQL builds a tuple: walk the columns in ``attnum`` order, pad each one
forward to its type's alignment boundary, then round the whole tuple up to
MAXALIGN and fit as many as will go in a page. Skipping the padding — as most
catalog bloat queries do — understates the row width and so reports bloat on
tables that have none. On a six-column table used in testing here it accounted
for 11 bytes a row, a phantom 7.5%.

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

from ..sql import SafeSqlDriver
from ..sql import SqlDriver
from .result import HealthCheckResult

TABLE_BLOAT = "Table bloat"

#: Ignore anything wasting less than this, matching the index bloat check.
DEFAULT_MIN_BLOAT_BYTES = 100 * 1024 * 1024

#: And ignore tables where the waste is a small share of the whole, so a large
#: healthy table is not flagged for its ordinary free space.
DEFAULT_MIN_BLOAT_PERCENT = 20.0

# PostgreSQL layout constants, all documented in "Database Page Layout".
_PAGE_HEADER_BYTES = 24
_ITEM_POINTER_BYTES = 4
_TUPLE_HEADER_BYTES = 23
_MAXALIGN = 8

#: A varlena value shorter than this is stored with a one-byte header and no
#: alignment padding at all, whatever its type's declared alignment says.
_SHORT_VARLENA_LIMIT = 128

TABLE_BLOAT_SQL = f"""
WITH RECURSIVE columns AS (
    SELECT
        c.oid AS table_oid,
        row_number() OVER (PARTITION BY c.oid ORDER BY a.attnum) AS ord,
        CASE
            -- A short varlena carries a one-byte header and is stored
            -- unaligned, so its declared alignment does not apply.
            WHEN t.typlen = -1 AND coalesce(s.avg_width, 0) < {_SHORT_VARLENA_LIMIT} THEN 1
            WHEN t.typalign = 'd' THEN 8
            WHEN t.typalign = 'i' THEN 4
            WHEN t.typalign = 's' THEN 2
            ELSE 1
        END AS align_to,
        -- A NULL occupies no data bytes, only a bit in the null bitmap.
        (1 - coalesce(s.null_frac, 0)) * coalesce(s.avg_width, 0) AS width,
        coalesce(s.null_frac, 0) > 0 AS nullable,
        s.attname IS NOT NULL AS analyzed
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_type t ON t.oid = a.atttypid
    LEFT JOIN pg_stats s
        ON s.schemaname = n.nspname AND s.tablename = c.relname AND s.attname = a.attname
    WHERE a.attnum > 0
      AND NOT a.attisdropped
      AND c.relkind IN ('r', 'm')
      AND c.reltuples > 0
      AND c.relpages > 0
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
),
-- Lay the columns out one after another, padding each forward to its
-- alignment boundary. The offset of a column depends on every column before
-- it, which is why this walks them in order rather than summing the widths.
layout AS (
    SELECT table_oid, ord, ceil(width)::numeric AS data_end
    FROM columns
    WHERE ord = 1
  UNION ALL
    SELECT
        c.table_oid,
        c.ord,
        ceil(ceil(l.data_end / c.align_to) * c.align_to + c.width)::numeric
    FROM layout l
    JOIN columns c ON c.table_oid = l.table_oid AND c.ord = l.ord + 1
),
shapes AS (
    SELECT
        l.table_oid,
        max(l.data_end) AS data_bytes,
        max(h.column_count) AS column_count,
        bool_or(h.has_nulls) AS has_nulls
    FROM layout l
    JOIN (
        SELECT table_oid, count(*) AS column_count, bool_or(nullable) AS has_nulls
        FROM columns
        GROUP BY table_oid
        -- A table with an unanalyzed column has no usable width for it, and a
        -- guess there would be reported as bloat.
        HAVING bool_and(analyzed)
    ) h ON h.table_oid = l.table_oid
    GROUP BY l.table_oid
),
estimated AS (
    SELECT
        n.nspname AS schema,
        c.relname AS table,
        c.reltuples::numeric AS live_rows,
        c.relpages AS actual_pages,
        current_setting('block_size')::numeric AS block_size,
        {_ITEM_POINTER_BYTES} + {_MAXALIGN} * ceil(
            (
                -- The data area starts after a MAXALIGNed tuple header.
                {_MAXALIGN} * ceil(
                    ({_TUPLE_HEADER_BYTES}
                     + CASE WHEN s.has_nulls THEN ceil(s.column_count / 8.0) ELSE 0 END
                    ) / {_MAXALIGN}.0
                )
                + s.data_bytes
            ) / {_MAXALIGN}.0
        ) AS row_bytes,
        coalesce(
            (SELECT split_part(opt, '=', 2)::numeric FROM unnest(c.reloptions) opt WHERE opt LIKE 'fillfactor=%'),
            100
        ) AS fillfactor
    FROM shapes s
    JOIN pg_class c ON c.oid = s.table_oid
    JOIN pg_namespace n ON n.oid = c.relnamespace
),
packed AS (
    SELECT
        e.*,
        -- Whole rows only: a row that does not fit starts the next page.
        greatest(floor((e.block_size - {_PAGE_HEADER_BYTES}) * e.fillfactor / 100 / e.row_bytes), 1) AS rows_per_page
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

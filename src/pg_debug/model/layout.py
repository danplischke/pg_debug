"""The column-order rule: bytes lost to alignment padding.

PostgreSQL stores columns in the order they were declared and starts each one
at an offset its type can be read from — an ``int8`` at a multiple of 8, an
``int4`` at a multiple of 4. Declaring a ``boolean`` between two ``bigint``s
therefore costs seven bytes of nothing, on every row, for the life of the
table. Ordering the same columns widest-alignment-first removes the holes
entirely and changes no behaviour whatsoever.

Both row widths come from :mod:`pg_debug.queries.tuple_layout`, the same code the table
bloat check measures with, so a table this rule calls optimal is a table the
bloat check will not accuse of padding.
"""

from __future__ import annotations

from typing import Any

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..queries.tuple_layout import DECLARED_ORDER
from ..queries.tuple_layout import PACKED_ORDER
from ..queries.tuple_layout import fillfactor_expr
from ..queries.tuple_layout import row_layout_ctes
from ..queries.tuple_layout import rows_per_page_expr
from .catalog import scoped
from .catalog import user_schemas
from .result import Confidence
from .result import ModelFinding
from .result import ReviewOptions
from .result import RuleId
from .result import Severity

#: Padding below this is not worth a table rewrite, whatever the row count says.
MIN_PADDING_BYTES = 1024 * 1024

#: Where the saving stops being an argument and starts being a decision.
HIGH_PADDING_BYTES = 100 * 1024 * 1024
MEDIUM_PADDING_BYTES = 10 * 1024 * 1024

_RELATION_FILTER = "c.relkind IN ('r', 'm') AND c.reltuples > 0"

COLUMN_ORDER_PADDING_SQL = f"""
WITH RECURSIVE {row_layout_ctes(_RELATION_FILTER, (DECLARED_ORDER, PACKED_ORDER))},
packed_order AS (
    SELECT
        o.table_oid,
        array_agg(o.column_name ORDER BY o.ord) AS columns,
        string_agg(quote_ident(o.column_name) || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY o.ord) AS column_definitions,
        bool_and(o.fixed_width) AS all_fixed_width
    FROM ordered o
    JOIN pg_attribute a ON a.attrelid = o.table_oid AND a.attnum = o.attnum
    WHERE o.variant = 2
    GROUP BY o.table_oid
),
compared AS (
    SELECT
        declared.table_oid,
        declared.row_bytes AS current_row_bytes,
        packed.row_bytes AS packed_row_bytes,
        c.reltuples::numeric AS live_rows,
        current_setting('block_size')::numeric AS block_size,
        {fillfactor_expr()} AS fillfactor
    FROM row_shapes declared
    JOIN row_shapes packed ON packed.table_oid = declared.table_oid AND packed.variant = 2
    JOIN pg_class c ON c.oid = declared.table_oid
    WHERE declared.variant = 1
),
-- The saving is in pages, not in bytes a row. A few bytes off the row can be
-- worth no pages at all, or a good deal more than the row arithmetic suggests,
-- depending on where the row width falls against the page.
paged AS (
    SELECT
        cmp.*,
        ceil(cmp.live_rows / {rows_per_page_expr("cmp.current_row_bytes", "cmp.fillfactor", "cmp.block_size")}) AS current_pages,
        ceil(cmp.live_rows / {rows_per_page_expr("cmp.packed_row_bytes", "cmp.fillfactor", "cmp.block_size")}) AS packed_pages
    FROM compared cmp
)
SELECT
    n.nspname AS schema,
    c.relname AS table,
    format('%I.%I', n.nspname, c.relname) AS table_ident,
    o.columns,
    o.column_definitions,
    o.all_fixed_width,
    p.current_row_bytes::bigint AS current_row_bytes,
    p.packed_row_bytes::bigint AS packed_row_bytes,
    (p.current_row_bytes - p.packed_row_bytes)::bigint AS saved_bytes_per_row,
    p.live_rows::bigint AS row_count,
    p.current_pages::bigint AS current_pages,
    p.packed_pages::bigint AS packed_pages,
    ((p.current_pages - p.packed_pages) * p.block_size)::bigint AS impact_bytes,
    pg_relation_size(c.oid) AS table_bytes
FROM paged p
JOIN pg_class c ON c.oid = p.table_oid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN packed_order o ON o.table_oid = p.table_oid
WHERE p.current_pages > p.packed_pages
  AND (p.current_pages - p.packed_pages) * p.block_size >= {{}}
  AND p.live_rows >= {{}}
  AND {user_schemas()}
"""


async def column_order_padding(sql_driver: SqlDriver, options: ReviewOptions) -> list[ModelFinding]:
    """Find tables whose columns would pack into fewer bytes in another order."""
    sql, params = scoped(COLUMN_ORDER_PADDING_SQL, options, "n.nspname")
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql, [MIN_PADDING_BYTES, options.min_rows, *params])
    return [_padding_finding(dict(row.cells)) for row in rows or []]


def _padding_finding(row: dict[str, Any]) -> ModelFinding:
    impact_bytes = int(row["impact_bytes"] or 0)
    per_row = int(row["saved_bytes_per_row"] or 0)
    row_count = int(row["row_count"] or 0)
    all_fixed_width = bool(row["all_fixed_width"])

    if impact_bytes >= HIGH_PADDING_BYTES:
        severity = Severity.HIGH
    elif impact_bytes >= MEDIUM_PADDING_BYTES:
        severity = Severity.MEDIUM
    else:
        severity = Severity.LOW

    detail = (
        f"Each row occupies {int(row['current_row_bytes'])} bytes and would occupy "
        f"{int(row['packed_row_bytes'])} with the columns in the order below — {per_row} bytes a row of "
        f"alignment padding. Across {row_count:,} rows that is {int(row['current_pages']):,} pages "
        f"instead of {int(row['packed_pages']):,}, a saving of {impact_bytes:,} bytes. Reordering changes "
        f"no behaviour: the same columns, the same types, the same constraints."
    )
    if all_fixed_width:
        confidence = Confidence.PROVEN
        detail += " Every column here is fixed-width, so this saving is exact."
    else:
        confidence = Confidence.CANDIDATE
        detail += (
            " Some columns are variable-width, and pg_stats records only their average width, so the saving is an estimate rather than a measurement."
        )

    return ModelFinding(
        rule=RuleId.COLUMN_ORDER_PADDING,
        confidence=confidence,
        severity=severity,
        schema=row["schema"],
        table=row["table"],
        columns=list(row["columns"]),
        summary=(f"Table {row['schema']}.{row['table']} wastes {per_row} bytes a row on alignment padding."),
        detail=detail,
        remediation=[
            # A column cannot be moved in place, so the fix is a rewrite. The
            # column list is the useful part; reproducing constraints, defaults
            # and indexes correctly is not something to generate blind.
            f"-- PostgreSQL cannot reorder columns in place. Recreate {row['table_ident']} with its",
            "-- columns in this order, then copy the rows over. Constraints, defaults, indexes and",
            "-- triggers are not reproduced here and must be carried across as well.",
            f"CREATE TABLE {row['table_ident']}_packed ({row['column_definitions']});",
        ],
        evidence={
            "current_row_bytes": int(row["current_row_bytes"]),
            "packed_row_bytes": int(row["packed_row_bytes"]),
            "saved_bytes_per_row": per_row,
            "current_pages": int(row["current_pages"]),
            "packed_pages": int(row["packed_pages"]),
            "row_count": row_count,
            "table_bytes": int(row["table_bytes"] or 0),
            "all_fixed_width": all_fixed_width,
        },
        verify=None if all_fixed_width else (f"SELECT pg_column_size(t.*) FROM {row['table_ident']} t LIMIT 100;"),
        impact_bytes=impact_bytes,
        rank=float(impact_bytes),
    )


__all__ = [
    "HIGH_PADDING_BYTES",
    "MEDIUM_PADDING_BYTES",
    "MIN_PADDING_BYTES",
    "column_order_padding",
]

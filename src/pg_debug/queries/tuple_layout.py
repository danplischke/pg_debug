"""How PostgreSQL lays a row out on a page, as reusable SQL.

A row is not the sum of its column widths. Every column starts at an offset
aligned to its type's requirement — an ``int8`` at a multiple of 8, an ``int4``
at a multiple of 4 — so a narrow column followed by a wide one leaves a hole,
and the hole is real bytes on real pages. The offset of a column therefore
depends on every column before it, which is why this walks them in order with a
recursive CTE rather than summing widths.

Two things read this and they must not disagree. The table bloat check compares
the pages a table's live rows need against the pages it occupies; ignoring the
padding understates the row and invents bloat on a healthy table — 11 bytes a
row, a phantom 7.5%, on a six-column table used in testing here. The data-model
review lays the same row out twice, once in the order the columns were declared
and once widest-alignment first, and reports the difference as a saving. Both
get their numbers from :func:`row_layout_ctes`.

What this cannot see is the *spread* of a variable-width column. ``pg_stats``
records one average width per column, so a table whose rows straddle a MAXALIGN
boundary packs fewer rows per page than that average implies. Anything derived
from a table with variable-width columns is an estimate, and callers say so.
"""

from __future__ import annotations

from typing import Sequence

# PostgreSQL layout constants, all documented in "Database Page Layout".
MAXALIGN = 8
PAGE_HEADER_BYTES = 24
ITEM_POINTER_BYTES = 4
TUPLE_HEADER_BYTES = 23

#: A varlena value shorter than this is stored with a one-byte header and no
#: alignment padding at all, whatever its type's declared alignment says.
SHORT_VARLENA_LIMIT = 128

#: The order the columns are actually in — what the table costs today.
DECLARED_ORDER = "attnum"

#: Widest alignment first, and variable-width columns after the fixed-width
#: ones of the same alignment. Descending alignment is what removes the padding:
#: a fixed-width column's width is a multiple of its own alignment, so as long
#: as the requirement never rises again, no column ever needs to be pushed
#: forward to meet it.
PACKED_ORDER = "align_to DESC, fixed_width DESC, width DESC, attnum"


def fillfactor_expr(reloptions: str = "c.reloptions") -> str:
    """The table's fillfactor, or 100 where it has not been set."""
    return f"coalesce((SELECT split_part(opt, '=', 2)::numeric FROM unnest({reloptions}) opt WHERE opt LIKE 'fillfactor=%'), 100)"


def rows_per_page_expr(row_bytes: str, fillfactor: str, block_size: str = "current_setting('block_size')::numeric") -> str:
    """How many rows of that width fit in a page.

    Whole rows only — a row that does not fit starts the next page — which is
    why a saving of a few bytes a row can be worth no pages at all, or rather
    more than the arithmetic on row widths alone suggests.
    """
    return f"greatest(floor(({block_size} - {PAGE_HEADER_BYTES}) * {fillfactor} / 100 / {row_bytes}), 1)"


def row_layout_ctes(relation_filter: str, orderings: Sequence[str] = (DECLARED_ORDER,)) -> str:
    """Build the CTEs that lay each table's row out byte by byte.

    The result is the body of a ``WITH RECURSIVE`` list — the caller supplies
    the keyword and whatever it needs after it::

        SQL = f"WITH RECURSIVE {row_layout_ctes(...)}, my_cte AS (...) SELECT ..."

    Args:
        relation_filter: SQL predicate narrowing which relations to lay out,
            over ``pg_class c`` and ``pg_namespace n``. It must not contain a
            bind placeholder: these CTEs come first in the statement, so a
            placeholder here would consume the caller's first parameter.
        orderings: One ORDER BY expression per layout to compute, over the
            columns of ``column_facts``. Each is numbered from 1 in the
            ``variant`` column, so ``(DECLARED_ORDER, PACKED_ORDER)`` yields
            variant 1 for the table as it stands and variant 2 for the packed
            alternative.

    Returns:
        Four CTEs — ``column_facts``, ``ordered``, ``layout`` and
        ``row_shapes`` — with no trailing comma. ``row_shapes`` has one row per
        (``table_oid``, ``variant``) carrying the total ``row_bytes``, and
        ``ordered`` is left available so a caller can read the column order
        back out.
    """
    if not orderings:
        raise ValueError("At least one ordering is required")

    variants = "\n  UNION ALL\n".join(
        f"""    SELECT f.*, {number} AS variant, row_number() OVER (PARTITION BY f.table_oid ORDER BY {ordering}) AS ord
    FROM column_facts f"""
        for number, ordering in enumerate(orderings, start=1)
    )

    return f"""column_facts AS (
    SELECT
        c.oid AS table_oid,
        a.attnum,
        a.attname AS column_name,
        CASE
            -- A short varlena carries a one-byte header and is stored
            -- unaligned, so its declared alignment does not apply.
            WHEN t.typlen = -1 AND coalesce(s.avg_width, 0) < {SHORT_VARLENA_LIMIT} THEN 1
            WHEN t.typalign = 'd' THEN 8
            WHEN t.typalign = 'i' THEN 4
            WHEN t.typalign = 's' THEN 2
            ELSE 1
        END AS align_to,
        -- A NULL occupies no data bytes, only a bit in the null bitmap.
        (1 - coalesce(s.null_frac, 0)) * coalesce(s.avg_width, 0) AS width,
        t.typlen > 0 AS fixed_width,
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
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND {relation_filter}
),
ordered AS (
{variants}
),
-- Lay the columns out one after another, padding each forward to its alignment
-- boundary. A column's offset depends on every column before it, so this walks
-- them in order rather than summing the widths.
layout AS (
    SELECT table_oid, variant, ord, ceil(width)::numeric AS data_end
    FROM ordered
    WHERE ord = 1
  UNION ALL
    SELECT
        o.table_oid,
        o.variant,
        o.ord,
        ceil(ceil(l.data_end / o.align_to) * o.align_to + o.width)::numeric
    FROM layout l
    JOIN ordered o ON o.table_oid = l.table_oid AND o.variant = l.variant AND o.ord = l.ord + 1
),
row_shapes AS (
    SELECT
        d.table_oid,
        d.variant,
        {ITEM_POINTER_BYTES} + {MAXALIGN} * ceil(
            (
                -- The data area starts after a MAXALIGNed tuple header.
                {MAXALIGN} * ceil(
                    ({TUPLE_HEADER_BYTES}
                     + CASE WHEN h.has_nulls THEN ceil(h.column_count / 8.0) ELSE 0 END
                    ) / {MAXALIGN}.0
                )
                + d.data_bytes
            ) / {MAXALIGN}.0
        ) AS row_bytes
    FROM (
        SELECT table_oid, variant, max(data_end) AS data_bytes
        FROM layout
        GROUP BY table_oid, variant
    ) d
    JOIN (
        SELECT table_oid, count(*) AS column_count, bool_or(nullable) AS has_nulls
        FROM column_facts
        GROUP BY table_oid
        -- A table with an unanalyzed column has no usable width for it, and a
        -- guess there would surface as bloat or as a phantom saving.
        HAVING bool_and(analyzed)
    ) h ON h.table_oid = d.table_oid
)"""


__all__ = [
    "DECLARED_ORDER",
    "ITEM_POINTER_BYTES",
    "MAXALIGN",
    "PACKED_ORDER",
    "PAGE_HEADER_BYTES",
    "SHORT_VARLENA_LIMIT",
    "TUPLE_HEADER_BYTES",
    "fillfactor_expr",
    "row_layout_ctes",
    "rows_per_page_expr",
]

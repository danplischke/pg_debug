"""Rules about the indexes a model has, judged against what the columns hold.

A btree keeps an entry for every row, NULL or not. On a column that is mostly
NULL, most of the index describes rows no query asking for a value will ever
want. How much of the column is NULL comes from the planner's sampled
statistics, and whether any query wants the NULLs comes from the workload,
which the catalog does not record, so what this finds is a candidate.
"""

from __future__ import annotations

from typing import Any

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..severity import Confidence
from ..severity import Severity
from .catalog import scoped
from .catalog import user_schemas
from .result import ModelFinding
from .result import ReviewOptions
from .result import RuleId

#: An index whose leading column is NULL in at least this share of the sampled
#: rows is reported. Below half, a partial index saves less than the index it
#: replaces costs to rebuild.
MOSTLY_NULL_FRACTION = 0.5

# A plain, valid, non-unique btree over a nullable leading column. A unique
# index is left out: whether NULLs are distinct in it (NULLS NOT DISTINCT,
# PostgreSQL 15) changes what a partial copy would enforce. So is an index
# behind a constraint, which cannot be made partial, and an expression index,
# whose leading key is not the column the statistics describe (indkey 0).
MOSTLY_NULL_INDEX_SQL = f"""
SELECT
    n.nspname AS schema,
    c.relname AS table,
    a.attname AS column,
    ic.relname AS index,
    format('%I.%I', n.nspname, c.relname) AS table_ident,
    format('%I.%I', n.nspname, ic.relname) AS index_ident,
    quote_ident(ic.relname) AS index_name_ident,
    quote_ident(left(ic.relname || '_not_null', 63)) AS partial_name_ident,
    format('%I.%I', n.nspname, left(ic.relname || '_not_null', 63)) AS partial_ident,
    quote_ident(a.attname) AS column_ident,
    pg_get_indexdef(i.indexrelid) AS index_definition,
    s.null_frac,
    c.reltuples::bigint AS row_count,
    pg_relation_size(i.indexrelid) AS index_bytes,
    psi.idx_scan,
    greatest(ps.last_analyze, ps.last_autoanalyze) AS last_analyzed
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_am am ON am.oid = ic.relam
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = i.indkey[0]
JOIN pg_stats s ON s.schemaname = n.nspname AND s.tablename = c.relname AND s.attname = a.attname AND NOT s.inherited
LEFT JOIN pg_stat_user_indexes psi ON psi.indexrelid = i.indexrelid
LEFT JOIN pg_stat_user_tables ps ON ps.relid = c.oid
WHERE am.amname = 'btree'
  AND i.indisvalid
  AND NOT i.indisunique
  AND i.indpred IS NULL
  AND i.indkey[0] <> 0
  AND NOT a.attnotnull
  AND c.relkind IN ('r', 'm')
  AND NOT EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = i.indexrelid)
  AND {user_schemas()}
  AND c.reltuples >= {{}}
  AND s.null_frac >= {{}}
"""


async def mostly_null_indexes(sql_driver: SqlDriver, options: ReviewOptions) -> list[ModelFinding]:
    """Find btree indexes whose leading column is mostly NULL.

    A partial index ``WHERE column IS NOT NULL`` holds only the rows that have
    a value, and the planner uses it for any condition on the column that
    cannot be true of a NULL (``=``, ``<``, ``IN``, ``LIKE``), because such a
    condition implies ``IS NOT NULL``. What it cannot serve is a query that
    looks for the NULLs, or one that reads the column in index order with no
    condition on it. Whether the workload has those is not in the catalog.
    """
    sql, params = scoped(MOSTLY_NULL_INDEX_SQL, options, "n.nspname")
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql, [options.min_rows, MOSTLY_NULL_FRACTION, *params])
    return [_mostly_null_index_finding(dict(row.cells)) for row in rows or []]


def _partial_definition(row: dict[str, Any]) -> str | None:
    """The index's own definition, renamed, built concurrently, and restricted to the rows with a value."""
    definition = str(row["index_definition"] or "")
    prefix = f"CREATE INDEX {row['index_name_ident']} ON "
    if not definition.startswith(prefix):
        return None
    return f"CREATE INDEX CONCURRENTLY {row['partial_name_ident']} ON {definition[len(prefix) :]} WHERE {row['column_ident']} IS NOT NULL;"


def _mostly_null_index_finding(row: dict[str, Any]) -> ModelFinding:
    null_frac = float(row["null_frac"] or 0.0)
    index_bytes = int(row["index_bytes"] or 0)
    row_count = int(row["row_count"] or 0)
    impact_bytes = int(index_bytes * null_frac)
    last_analyzed = row["last_analyzed"]
    table_ident = row["table_ident"]
    column_ident = row["column_ident"]

    partial = _partial_definition(row)
    if partial is None:
        remediation = [f"-- Recreate {row['index_ident']} with WHERE {column_ident} IS NOT NULL, then drop the original."]
    else:
        remediation = [
            partial,
            f"DROP INDEX CONCURRENTLY {row['index_ident']};",
            f"ALTER INDEX {row['partial_ident']} RENAME TO {row['index_name_ident']};",
        ]

    return ModelFinding(
        rule=RuleId.MOSTLY_NULL_INDEX,
        confidence=Confidence.CANDIDATE,
        severity=Severity.LOW,
        schema=row["schema"],
        table=row["table"],
        columns=[row["column"]],
        summary=(
            f"Index {row['index']} on {row['schema']}.{row['table']}.{row['column']} is {null_frac:.0%} NULL entries, by the sampled statistics."
        ),
        detail=(
            f"The planner's statistics, last collected {last_analyzed or 'at an unrecorded time'}, put the NULL "
            f"fraction of {row['column']} at {null_frac:.0%} of {row_count:,} rows, and a btree keeps an entry for every "
            f"one of them. A partial index WHERE {row['column']} IS NOT NULL would be about {impact_bytes:,} bytes smaller "
            f"and serve every condition that cannot be true of a NULL (=, <, IN, LIKE), since each implies IS NOT NULL. "
            f"It is a candidate because what it cannot serve, a query for {row['column']} IS NULL or one reading the "
            f"column in index order with no condition on it, is in the workload rather than the catalog: look for "
            f"those first. The verify query gives the exact NULL fraction the sample estimated."
        ),
        remediation=remediation,
        evidence={
            "index": row["index"],
            "null_frac": null_frac,
            "row_count": row_count,
            "index_bytes": index_bytes,
            "index_scans": None if row["idx_scan"] is None else int(row["idx_scan"]),
            "last_analyzed": str(last_analyzed) if last_analyzed else None,
        },
        verify=f"SELECT avg(({column_ident} IS NULL)::int) AS null_fraction, count(*) AS row_count FROM {table_ident};",
        impact_bytes=impact_bytes,
        rank=float(impact_bytes),
    )


__all__ = ["MOSTLY_NULL_FRACTION", "MOSTLY_NULL_INDEX_SQL", "mostly_null_indexes"]

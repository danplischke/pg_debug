"""Rules about keys: the parts of a model that say how rows relate.

Everything here is read straight from ``pg_constraint``, ``pg_index`` and
``pg_attribute``, so nothing in this module is a guess. What varies is how much
each finding matters, and that comes from the sizes and the write counts the
statistics collector already keeps.
"""

from __future__ import annotations

from typing import Any
from typing import Dict
from typing import List

from ..sql import SafeSqlDriver
from ..sql import SqlDriver
from .catalog import scoped
from .catalog import user_schemas
from .result import Confidence
from .result import ModelFinding
from .result import ReviewOptions
from .result import RuleId
from .result import Severity

#: Below this a sequential scan of the child costs a few milliseconds and holds
#: its lock for no time worth measuring. The finding is still true, just not
#: urgent.
SIGNIFICANT_TABLE_BYTES = 8 * 1024 * 1024

# An index serves a foreign key's referential-integrity lookup when its first
# k key columns are exactly the k constrained columns, in any order among
# themselves — a btree on (b, a) answers an equality lookup on {a, b} perfectly
# well. It must also be valid, unrestricted and over plain columns: a partial
# index does not cover the rows the check has to look at, an expression index
# does not answer a lookup by column value, and INCLUDE columns are payload
# rather than key, which is what indnkeyatts guards against.
_INDEX_SERVES_THE_KEY = """
    SELECT 1
    FROM pg_index i
    JOIN pg_class ic ON ic.oid = i.indexrelid
    JOIN pg_am am ON am.oid = ic.relam
    WHERE i.indrelid = con.conrelid
      AND i.indisvalid
      AND i.indpred IS NULL
      AND i.indexprs IS NULL
      AND am.amname IN ('btree', 'hash')
      AND i.indnkeyatts >= array_length(con.conkey, 1)
      AND (i.indkey::int2[])[0:array_length(con.conkey, 1) - 1] <@ con.conkey::int2[]
      AND (i.indkey::int2[])[0:array_length(con.conkey, 1) - 1] @> con.conkey::int2[]
"""

UNINDEXED_FOREIGN_KEY_SQL = f"""
SELECT
    cn.nspname AS schema,
    c.relname AS table,
    con.conname AS constraint_name,
    cols.columns,
    cols.column_list,
    format('%I.%I', cn.nspname, c.relname) AS table_ident,
    pn.nspname AS referenced_schema,
    p.relname AS referenced_table,
    c.reltuples::bigint AS row_count,
    pg_relation_size(c.oid) AS table_bytes,
    coalesce(ps.n_tup_del, 0) AS referenced_deletes,
    coalesce(ps.n_tup_upd, 0) AS referenced_updates
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace cn ON cn.oid = c.relnamespace
JOIN pg_class p ON p.oid = con.confrelid
JOIN pg_namespace pn ON pn.oid = p.relnamespace
LEFT JOIN pg_stat_user_tables ps ON ps.relid = con.confrelid
CROSS JOIN LATERAL (
    SELECT
        array_agg(a.attname ORDER BY k.ord) AS columns,
        string_agg(quote_ident(a.attname), ', ' ORDER BY k.ord) AS column_list
    FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
    JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
) cols
WHERE con.contype = 'f'
  AND {user_schemas("cn")}
  AND NOT EXISTS ({_INDEX_SERVES_THE_KEY})
"""

FOREIGN_KEY_TYPE_MISMATCH_SQL = f"""
SELECT
    cn.nspname AS schema,
    c.relname AS table,
    con.conname AS constraint_name,
    ca.attname AS column,
    format_type(ca.atttypid, ca.atttypmod) AS column_type,
    pn.nspname AS referenced_schema,
    p.relname AS referenced_table,
    pa.attname AS referenced_column,
    format_type(pa.atttypid, pa.atttypmod) AS referenced_type,
    format('%I.%I', cn.nspname, c.relname) AS table_ident,
    quote_ident(ca.attname) AS column_ident,
    c.reltuples::bigint AS row_count,
    pg_relation_size(c.oid) AS table_bytes
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace cn ON cn.oid = c.relnamespace
JOIN pg_class p ON p.oid = con.confrelid
JOIN pg_namespace pn ON pn.oid = p.relnamespace
JOIN unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
JOIN unnest(con.confkey) WITH ORDINALITY AS f(attnum, ord) ON f.ord = k.ord
JOIN pg_attribute ca ON ca.attrelid = con.conrelid AND ca.attnum = k.attnum
JOIN pg_attribute pa ON pa.attrelid = con.confrelid AND pa.attnum = f.attnum
WHERE con.contype = 'f'
  AND {user_schemas("cn")}
  AND (
      ca.atttypid <> pa.atttypid
      OR ca.atttypmod <> pa.atttypmod
      -- Only when both sides actually have a collation to disagree about.
      OR (ca.attcollation <> pa.attcollation AND ca.attcollation <> 0 AND pa.attcollation <> 0)
  )
"""

MISSING_PRIMARY_KEY_SQL = f"""
SELECT
    n.nspname AS schema,
    c.relname AS table,
    format('%I.%I', n.nspname, c.relname) AS table_ident,
    c.reltuples::bigint AS row_count,
    pg_relation_size(c.oid) AS table_bytes,
    c.relreplident,
    (
        SELECT quote_ident(ic.relname)
        FROM pg_index i
        JOIN pg_class ic ON ic.oid = i.indexrelid
        WHERE i.indrelid = c.oid
          AND i.indisunique
          AND i.indisvalid
          AND i.indpred IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM pg_attribute a
              WHERE a.attrelid = c.oid
                AND a.attnum = ANY((i.indkey::int2[])[0:i.indnkeyatts - 1])
                AND NOT a.attnotnull
          )
        ORDER BY i.indnkeyatts, ic.relname
        LIMIT 1
    ) AS unique_index
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND NOT c.relispartition
  AND {user_schemas()}
  AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary)
"""


async def unindexed_foreign_keys(sql_driver: SqlDriver, options: ReviewOptions) -> List[ModelFinding]:
    """Find foreign keys with no index behind them on the child side.

    PostgreSQL indexes the parent side of a foreign key for you — it has to,
    the key must be unique — and indexes nothing on the child. So every DELETE
    from the parent, and every UPDATE that changes the key, scans the whole
    child table to prove no row still references the row being removed. It does
    that while holding the parent row's lock.
    """
    sql, params = scoped(UNINDEXED_FOREIGN_KEY_SQL, options, "cn.nspname")
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql, params)
    return [_unindexed_foreign_key_finding(dict(row.cells)) for row in rows or []]


def _unindexed_foreign_key_finding(row: Dict[str, Any]) -> ModelFinding:
    table_bytes = int(row["table_bytes"] or 0)
    deletes = int(row["referenced_deletes"] or 0)
    big = table_bytes >= SIGNIFICANT_TABLE_BYTES

    if big and deletes > 0:
        severity = Severity.HIGH
    elif big:
        severity = Severity.MEDIUM
    else:
        severity = Severity.LOW

    columns = list(row["columns"])
    referenced = f"{row['referenced_schema']}.{row['referenced_table']}"
    detail = (
        f"Every DELETE from {referenced}, and every UPDATE there that changes the referenced key, "
        f"scans all {int(row['row_count'] or 0):,} rows of {row['schema']}.{row['table']} to check this "
        f"constraint, holding a lock on the parent row while it does. "
    )
    if deletes:
        detail += f"{deletes:,} deletes have been recorded against {referenced} since the statistics were last reset."
    else:
        detail += f"No deletes have been recorded against {referenced} yet, so the cost is latent rather than current."

    return ModelFinding(
        rule=RuleId.UNINDEXED_FOREIGN_KEY,
        confidence=Confidence.PROVEN,
        severity=severity,
        schema=row["schema"],
        table=row["table"],
        columns=columns,
        summary=f"Foreign key {row['constraint_name']} on ({', '.join(columns)}) has no index behind it.",
        detail=detail,
        remediation=[
            # CONCURRENTLY keeps writes running; it cannot be used inside a
            # transaction block, which is worth knowing before it is pasted
            # into a migration.
            f"CREATE INDEX CONCURRENTLY ON {row['table_ident']} ({row['column_list']});",
        ],
        evidence={
            "constraint": row["constraint_name"],
            "referenced_table": referenced,
            "row_count": int(row["row_count"] or 0),
            "table_bytes": table_bytes,
            "referenced_deletes": deletes,
            "referenced_updates": int(row["referenced_updates"] or 0),
        },
        rank=float(table_bytes),
    )


async def foreign_key_type_mismatches(sql_driver: SqlDriver, options: ReviewOptions) -> List[ModelFinding]:
    """Find foreign keys whose two sides are not the same type.

    PostgreSQL will let you reference a ``bigint`` from an ``integer`` as long
    as a cast exists. The constraint then works, but the two columns no longer
    compare directly: the model claims two things are the same identifier while
    storing them differently, and the child's index may not be usable for the
    check.
    """
    sql, params = scoped(FOREIGN_KEY_TYPE_MISMATCH_SQL, options, "cn.nspname")
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql, params)
    return [_type_mismatch_finding(dict(row.cells)) for row in rows or []]


def _type_mismatch_finding(row: Dict[str, Any]) -> ModelFinding:
    table_bytes = int(row["table_bytes"] or 0)
    severity = Severity.HIGH if table_bytes >= SIGNIFICANT_TABLE_BYTES else Severity.MEDIUM
    referenced = f"{row['referenced_schema']}.{row['referenced_table']}.{row['referenced_column']}"

    return ModelFinding(
        rule=RuleId.FOREIGN_KEY_TYPE_MISMATCH,
        confidence=Confidence.PROVEN,
        severity=severity,
        schema=row["schema"],
        table=row["table"],
        columns=[row["column"]],
        summary=(f"Foreign key {row['constraint_name']} joins {row['column_type']} to {row['referenced_type']}."),
        detail=(
            f"{row['schema']}.{row['table']}.{row['column']} is {row['column_type']} while {referenced} "
            f"is {row['referenced_type']}. The constraint holds, because a cast exists, but the two "
            f"columns no longer hold the same values in the same way — and a query joining them has to "
            f"convert one side before it can use an index on the other."
        ),
        remediation=[
            # This rewrites the table under an ACCESS EXCLUSIVE lock, which is
            # the whole reason to know about it before the table gets bigger.
            f"ALTER TABLE {row['table_ident']} ALTER COLUMN {row['column_ident']} TYPE {row['referenced_type']};",
        ],
        evidence={
            "constraint": row["constraint_name"],
            "column_type": row["column_type"],
            "referenced_column": referenced,
            "referenced_type": row["referenced_type"],
            "row_count": int(row["row_count"] or 0),
            "table_bytes": table_bytes,
        },
        rank=float(table_bytes),
    )


async def missing_primary_keys(sql_driver: SqlDriver, options: ReviewOptions) -> List[ModelFinding]:
    """Find tables with no primary key.

    Without one there is no agreed way to name a row: no ON CONFLICT, no
    reliable deduplication, and — unless the table has been given a replica
    identity by hand — logical replication rejects every UPDATE and DELETE
    against it outright.
    """
    sql, params = scoped(MISSING_PRIMARY_KEY_SQL, options, "n.nspname")
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql, params)
    return [_missing_primary_key_finding(dict(row.cells)) for row in rows or []]


def _missing_primary_key_finding(row: Dict[str, Any]) -> ModelFinding:
    unique_index = row["unique_index"]
    # 'f' means REPLICA IDENTITY FULL: no key, but every column is the key, so
    # replication can still identify a row.
    replica_identity_set = row["relreplident"] == "f"

    if unique_index or replica_identity_set:
        severity = Severity.MEDIUM
        detail = (
            "The table has no primary key. It does have "
            + ("a unique index over NOT NULL columns" if unique_index else "REPLICA IDENTITY FULL")
            + ", so a row can still be identified, but nothing in the schema says which columns are the key."
        )
        remediation = (
            [f"ALTER TABLE {row['table_ident']} ADD PRIMARY KEY USING INDEX {unique_index};"]
            if unique_index
            else [f"-- Add a key to {row['table_ident']}, then: ALTER TABLE {row['table_ident']} REPLICA IDENTITY DEFAULT;"]
        )
    else:
        severity = Severity.HIGH
        detail = (
            "The table has no primary key, no unique index over NOT NULL columns, and no replica "
            "identity. There is no way to address a single row of it: logical replication rejects "
            "every UPDATE and DELETE against the table, and duplicates cannot be prevented or found."
        )
        remediation = [
            f"ALTER TABLE {row['table_ident']} ADD COLUMN id bigint GENERATED ALWAYS AS IDENTITY;",
            f"ALTER TABLE {row['table_ident']} ADD PRIMARY KEY (id);",
        ]

    return ModelFinding(
        rule=RuleId.MISSING_PRIMARY_KEY,
        confidence=Confidence.PROVEN,
        severity=severity,
        schema=row["schema"],
        table=row["table"],
        columns=[],
        summary=f"Table {row['schema']}.{row['table']} has no primary key.",
        detail=detail,
        remediation=remediation,
        evidence={
            "row_count": int(row["row_count"] or 0),
            "table_bytes": int(row["table_bytes"] or 0),
            "unique_index": unique_index,
            "replica_identity": row["relreplident"],
        },
        rank=float(row["table_bytes"] or 0),
    )


__all__ = [
    "SIGNIFICANT_TABLE_BYTES",
    "foreign_key_type_mismatches",
    "missing_primary_keys",
    "unindexed_foreign_keys",
]

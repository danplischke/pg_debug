"""Rules about keys: the parts of a model that say how rows relate.

Everything here is read straight from ``pg_constraint``, ``pg_index`` and
``pg_attribute``, so nothing in this module is a guess. What varies is how much
each finding matters, and that comes from the sizes and the write counts the
statistics collector already keeps.
"""

from __future__ import annotations

from typing import Any

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..queries.sequences import type_max
from ..severity import Confidence
from ..severity import Severity
from .catalog import scoped
from .catalog import user_schemas
from .result import ModelFinding
from .result import ReviewOptions
from .result import RuleId

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

#: A primary key that has used this share of the values its type or its
#: sequence allows is reported. Widening it rewrites the table and every table
#: that references it, which takes longer the longer it is left.
PRIMARY_KEY_OVERFLOW_SHARE = 0.5

#: From this share on it is high severity: the rewrite now has to be planned
#: against a deadline the insert rate sets.
PRIMARY_KEY_OVERFLOW_HIGH_SHARE = 0.8

# The integer columns whose foreign keys reference the key, and so have to be
# widened with it.
_REFERENCING_INTEGER_COLUMNS = """
        FROM pg_constraint fk
        JOIN pg_class fc ON fc.oid = fk.conrelid
        JOIN pg_namespace fn ON fn.oid = fc.relnamespace
        JOIN pg_attribute fa ON fa.attrelid = fk.conrelid AND fa.attnum = fk.conkey[1]
        WHERE fk.contype = 'f'
          AND fk.confrelid = c.oid
          AND array_length(fk.confkey, 1) = 1
          AND fk.confkey[1] = a.attnum
          AND format_type(fa.atttypid, NULL) IN ('smallint', 'integer')
"""

# A single-column primary key fed by a sequence: a serial column owns its
# sequence through an 'a' (auto) dependency, an identity column through an 'i'
# (internal) one, which is why this reads pg_depend rather than parsing a
# nextval() default, which an identity column does not have. The key runs out
# at whichever limit is lower: the column's type, or the sequence's own
# maximum. A column widened to bigint whose serial sequence is still AS
# integer stops at 2^31 - 1 all the same. pg_sequences.last_value is NULL when
# the sequence has never been called, and when the connecting role may not
# read it; neither leaves a number to judge.
PRIMARY_KEY_OVERFLOW_SQL = f"""
SELECT
    n.nspname AS schema,
    c.relname AS table,
    a.attname AS column,
    format('%I.%I', n.nspname, c.relname) AS table_ident,
    quote_ident(a.attname) AS column_ident,
    format_type(a.atttypid, NULL) AS column_type,
    format('%I.%I', sn.nspname, s.relname) AS sequence_ident,
    d.deptype = 'i' AS is_identity,
    format_type(seq.seqtypid, NULL) AS sequence_type,
    seq.seqmax AS sequence_max,
    ps.last_value,
    c.reltuples::bigint AS row_count,
    pg_relation_size(c.oid) AS table_bytes,
    (
        SELECT array_agg(format('%I.%I.%I', fn.nspname, fc.relname, fa.attname) ORDER BY fn.nspname, fc.relname, fa.attname)
        {_REFERENCING_INTEGER_COLUMNS}
    ) AS referencing_columns,
    (
        SELECT array_agg(format('ALTER TABLE %I.%I ALTER COLUMN %I TYPE bigint;', fn.nspname, fc.relname, fa.attname)
                         ORDER BY fn.nspname, fc.relname, fa.attname)
        {_REFERENCING_INTEGER_COLUMNS}
    ) AS referencing_alters
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = i.indkey[0]
JOIN pg_depend d
  ON d.refobjid = c.oid
 AND d.refobjsubid = a.attnum
 AND d.deptype IN ('a', 'i')
 AND d.classid = 'pg_catalog.pg_class'::regclass
 AND d.refclassid = 'pg_catalog.pg_class'::regclass
JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
JOIN pg_namespace sn ON sn.oid = s.relnamespace
JOIN pg_sequence seq ON seq.seqrelid = s.oid
LEFT JOIN pg_sequences ps ON ps.schemaname = sn.nspname AND ps.sequencename = s.relname
WHERE i.indisprimary
  AND i.indnkeyatts = 1
  AND seq.seqincrement > 0
  AND {user_schemas()}
  AND (format_type(a.atttypid, NULL) IN ('smallint', 'integer') OR format_type(seq.seqtypid, NULL) IN ('smallint', 'integer'))
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


async def unindexed_foreign_keys(sql_driver: SqlDriver, options: ReviewOptions) -> list[ModelFinding]:
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


def _unindexed_foreign_key_finding(row: dict[str, Any]) -> ModelFinding:
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


async def foreign_key_type_mismatches(sql_driver: SqlDriver, options: ReviewOptions) -> list[ModelFinding]:
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


def _type_mismatch_finding(row: dict[str, Any]) -> ModelFinding:
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


async def primary_key_overflow(sql_driver: SqlDriver, options: ReviewOptions) -> list[ModelFinding]:
    """Find integer primary keys whose sequence has used much of what it can.

    A ``serial`` or ``integer`` identity key stops accepting inserts at
    2,147,483,647, and the fix, widening it to ``bigint``, rewrites the table
    and every table whose foreign key references it, under an ACCESS
    EXCLUSIVE lock. The sequence's last value is read, not estimated, so this
    is proven; a sequence the connecting role may not read is passed over, not
    guessed at.
    """
    sql, params = scoped(PRIMARY_KEY_OVERFLOW_SQL, options, "n.nspname")
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql, params)
    findings = [_primary_key_overflow_finding(dict(row.cells)) for row in rows or []]
    return [finding for finding in findings if finding is not None]


def _primary_key_overflow_finding(row: dict[str, Any]) -> ModelFinding | None:
    last_value = row["last_value"]
    if last_value is None:
        # Never called, or not readable by this role: either way there is no
        # number to judge, and an unread sequence is not an empty one.
        return None

    column_limit = type_max(row["column_type"])
    sequence_limit = int(row["sequence_max"])
    limit = min(column_limit, sequence_limit)
    share = int(last_value) / limit if limit else 0.0
    if share < PRIMARY_KEY_OVERFLOW_SHARE:
        return None

    severity = Severity.HIGH if share >= PRIMARY_KEY_OVERFLOW_HIGH_SHARE else Severity.MEDIUM
    table_ident = row["table_ident"]
    column_ident = row["column_ident"]
    sequence_ident = row["sequence_ident"]
    referencing = list(row["referencing_columns"] or [])
    limited_by = "column" if column_limit <= sequence_limit else "sequence"

    remediation: list[str] = []
    if row["column_type"] != "bigint":
        remediation.append(f"ALTER TABLE {table_ident} ALTER COLUMN {column_ident} TYPE bigint;")
    if not row["is_identity"] and row["sequence_type"] != "bigint":
        # An identity column's sequence follows its column's type; a serial
        # column's sequence was created AS integer and stays that way.
        remediation.append(f"ALTER SEQUENCE {sequence_ident} AS bigint;")
    remediation.extend(row["referencing_alters"] or [])

    if limited_by == "column":
        what_limits = f"its type, {row['column_type']}, holds at most {column_limit:,}"
    else:
        what_limits = f"its sequence {sequence_ident} is {row['sequence_type']} and stops at {sequence_limit:,}, whatever the column holds"

    row_count = int(row["row_count"] or 0)
    # reltuples is -1 on a table never vacuumed or analyzed (PostgreSQL 14 on).
    rows = f"{row_count:,} rows" if row_count >= 0 else "row count not yet estimated"
    detail = (
        f"The key is fed by {sequence_ident}, whose last value is {int(last_value):,}, and {what_limits}: "
        f"{share:.0%} of the values are used. Once they are all used, every insert fails. Widening to bigint rewrites "
        f"{row['schema']}.{row['table']} ({rows}) under an ACCESS EXCLUSIVE lock"
    )
    if referencing:
        detail += f", and the {len(referencing)} integer column(s) referencing it ({', '.join(referencing)}) have to be widened with it"
    detail += ". The statements below do it in place; on a large table, a new bigint column filled in batches and swapped in avoids the long lock."

    return ModelFinding(
        rule=RuleId.PRIMARY_KEY_OVERFLOW,
        confidence=Confidence.PROVEN,
        severity=severity,
        schema=row["schema"],
        table=row["table"],
        columns=[row["column"]],
        summary=f"Primary key {row['schema']}.{row['table']}.{row['column']} has used {share:.0%} of the values it can hold.",
        detail=detail,
        remediation=remediation,
        evidence={
            "column_type": row["column_type"],
            "sequence": sequence_ident,
            "sequence_type": row["sequence_type"],
            "last_value": int(last_value),
            "limit": limit,
            "limited_by": limited_by,
            "share_used": round(share, 4),
            "identity": bool(row["is_identity"]),
            "referencing_columns": referencing,
            "row_count": int(row["row_count"] or 0),
            "table_bytes": int(row["table_bytes"] or 0),
        },
        rank=share,
    )


async def missing_primary_keys(sql_driver: SqlDriver, options: ReviewOptions) -> list[ModelFinding]:
    """Find tables with no primary key.

    Without one there is no agreed way to name a row: no ON CONFLICT, no
    reliable deduplication, and — unless the table has been given a replica
    identity by hand — logical replication rejects every UPDATE and DELETE
    against it outright.
    """
    sql, params = scoped(MISSING_PRIMARY_KEY_SQL, options, "n.nspname")
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql, params)
    return [_missing_primary_key_finding(dict(row.cells)) for row in rows or []]


def _missing_primary_key_finding(row: dict[str, Any]) -> ModelFinding:
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
    "PRIMARY_KEY_OVERFLOW_HIGH_SHARE",
    "PRIMARY_KEY_OVERFLOW_SHARE",
    "SIGNIFICANT_TABLE_BYTES",
    "foreign_key_type_mismatches",
    "missing_primary_keys",
    "primary_key_overflow",
    "unindexed_foreign_keys",
]

"""Rules about individual columns: their types, and what their values reveal.

The first two rules read the catalog and are facts. The last two read sampled
planner statistics and are not: a column with no NULLs *in the sample* may
still have one, and a column with three distinct values in the sampled pages
may have thirty in the rest. Those are reported as candidates and carry the
query that settles the question, because the fix for both of them is DDL and
DDL applied on a guess is how a deployment fails.
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

#: More distinct values than this and the column is data, not a category.
MAX_DISTINCT_VALUES = 32

#: A category encoded as an integer costs four bytes. A text column narrower
#: than that has nothing to gain from becoming one.
_LOOKUP_KEY_BYTES = 4

# json keeps the text it was given; jsonb keeps the parsed value. Only the text
# can hold duplicate keys, so a duplicate is what would make the conversion
# change a value rather than just its spelling. Checked at the top level, where
# json_object_keys reads; CASE, because a scalar or array has no keys to count.
_DUPLICATE_KEYS = (
    "CASE WHEN json_typeof({column}) = 'object' "
    "THEN (SELECT count(*) FROM json_object_keys({column})) <> (SELECT count(*) FROM jsonb_object_keys({column}::jsonb)) "
    "ELSE false END"
)

# The statistics-driven rules need pg_stats in the FROM list rather than in an
# EXISTS, so they can report the numbers they judged on. `inherited` rows
# describe a partitioned parent's whole tree and would double-count.
_STATS_JOIN = "JOIN pg_stats s ON s.schemaname = n.nspname AND s.tablename = c.relname AND s.attname = a.attname AND NOT s.inherited"

_ANALYZE_JOIN = "LEFT JOIN pg_stat_user_tables ps ON ps.relid = c.oid"


def _column_source(*extra_joins: str) -> str:
    """Every column rule looks at the same columns. Only the WHERE differs."""
    joins = "".join(f"{join}\n" for join in extra_joins)
    return f"""
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type t ON t.oid = a.atttypid
{joins}WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p', 'm')
  AND {user_schemas()}
"""


_COLUMN_IDENTITY = """
    n.nspname AS schema,
    c.relname AS table,
    a.attname AS column,
    format('%I.%I', n.nspname, c.relname) AS table_ident,
    quote_ident(a.attname) AS column_ident,
    format_type(a.atttypid, a.atttypmod) AS column_type,
    c.reltuples::bigint AS row_count,
    pg_relation_size(c.oid) AS table_bytes
"""

NAIVE_TIMESTAMP_SQL = f"""
SELECT {_COLUMN_IDENTITY}
{_column_source()}
  AND t.typname = 'timestamp'
"""

JSON_COLUMN_SQL = f"""
SELECT {_COLUMN_IDENTITY}
{_column_source()}
  AND t.typname = 'json'
"""

BLANK_PADDED_CHAR_SQL = f"""
SELECT {_COLUMN_IDENTITY},
    a.atttypmod - 4 AS declared_length
{_column_source()}
  AND t.typname = 'bpchar'
"""

# Most columns in most databases hold no NULLs, so "nullable and never null" on
# its own describes half a schema and helps with none of it. It says something
# only where the nullability is itself a claim about the model: a foreign key
# that is optional in the schema but mandatory in every row, or a unique column
# whose uniqueness NULLs quietly escape.
_IN_FOREIGN_KEY = """
    EXISTS (
        SELECT 1 FROM pg_constraint con
        WHERE con.conrelid = c.oid AND con.contype = 'f' AND a.attnum = ANY(con.conkey)
    )
"""

_IN_UNIQUE_INDEX = """
    EXISTS (
        SELECT 1 FROM pg_index i
        WHERE i.indrelid = c.oid AND i.indisunique AND i.indisvalid
          AND a.attnum = ANY((i.indkey::int2[])[0:i.indnkeyatts - 1])
    )
"""

NULLABLE_NEVER_NULL_SQL = f"""
SELECT {_COLUMN_IDENTITY},
    s.null_frac,
    left(c.relname || '_' || a.attname || '_not_null', 63) AS constraint_name,
    greatest(ps.last_analyze, ps.last_autoanalyze) AS last_analyzed,
    ({_IN_FOREIGN_KEY}) AS in_foreign_key,
    ({_IN_UNIQUE_INDEX}) AS in_unique_index
{_column_source(_STATS_JOIN, _ANALYZE_JOIN)}
  AND NOT a.attnotnull
  AND s.null_frac = 0
  AND c.reltuples >= {{}}
  AND (({_IN_FOREIGN_KEY}) OR ({_IN_UNIQUE_INDEX}))
"""

# n_distinct is a count when positive and a negative multiple of the row count
# when the planner expresses it as a proportion. Reading the sign wrongly is
# how a column with every value distinct gets mistaken for a category.
_DISTINCT_VALUES = "CASE WHEN s.n_distinct > 0 THEN s.n_distinct ELSE -s.n_distinct * c.reltuples END"

LOW_CARDINALITY_TEXT_SQL = f"""
SELECT {_COLUMN_IDENTITY},
    s.n_distinct,
    s.avg_width,
    ({_DISTINCT_VALUES})::bigint AS distinct_values,
    s.most_common_vals::text::text[] AS common_values,
    -- The enum belongs in the same schema as the table it describes.
    format('%I.%I', n.nspname, left(c.relname || '_' || a.attname, 63)) AS enum_type_ident
{_column_source(_STATS_JOIN)}
  AND t.typname IN ('text', 'varchar', 'bpchar')
  AND c.reltuples >= {{}}
  AND s.avg_width > {_LOOKUP_KEY_BYTES}
  AND ({_DISTINCT_VALUES}) <= {{}}
"""


async def naive_timestamps(sql_driver: SqlDriver, options: ReviewOptions) -> list[ModelFinding]:
    """Find ``timestamp without time zone`` columns.

    The type does not record an instant. It records the reading on a clock,
    with no note of which clock, so the same value means different moments to a
    server in Berlin and a server in Denver — and the difference only shows up
    once someone changes ``TimeZone`` or the deployment crosses a region.
    ``timestamptz`` stores the instant and renders it in whatever zone the
    session asks for, which is almost always what was wanted.
    """
    sql, params = scoped(NAIVE_TIMESTAMP_SQL, options, "n.nspname")
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql, params)
    return [_naive_timestamp_finding(dict(row.cells)) for row in rows or []]


def _naive_timestamp_finding(row: dict[str, Any]) -> ModelFinding:
    return ModelFinding(
        rule=RuleId.NAIVE_TIMESTAMP,
        confidence=Confidence.PROVEN,
        severity=Severity.MEDIUM,
        schema=row["schema"],
        table=row["table"],
        columns=[row["column"]],
        summary=f"{row['schema']}.{row['table']}.{row['column']} is timestamp without time zone.",
        detail=(
            "The value records a clock reading rather than an instant, so what it means depends on the "
            "session's TimeZone setting at the moment it was written and again when it is read. "
            "Converting is a rewrite, and the conversion below assumes the existing values were written "
            "in UTC — if they were written in local time, name that zone instead."
        ),
        remediation=[
            f"ALTER TABLE {row['table_ident']} ALTER COLUMN {row['column_ident']} TYPE timestamptz USING {row['column_ident']} AT TIME ZONE 'UTC';",
        ],
        evidence={
            "column_type": row["column_type"],
            "row_count": int(row["row_count"] or 0),
            "table_bytes": int(row["table_bytes"] or 0),
        },
        rank=float(row["table_bytes"] or 0),
    )


async def json_columns(sql_driver: SqlDriver, options: ReviewOptions) -> list[ModelFinding]:
    """Find ``json`` columns, where ``jsonb`` is almost always what was meant.

    ``json`` stores the text exactly as it arrived and parses it again every
    time an operator reads it; it has no equality, no containment and no GIN
    index. ``jsonb`` stores the parsed value. What ``jsonb`` gives up is the
    text itself: whitespace, the order of keys and duplicate keys (the last
    one wins). The catalog cannot say whether anything relies on those, so
    this is a candidate, and the verify query counts the rows whose value the
    conversion would change rather than respell.
    """
    sql, params = scoped(JSON_COLUMN_SQL, options, "n.nspname")
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql, params)
    return [_json_column_finding(dict(row.cells)) for row in rows or []]


def _json_column_finding(row: dict[str, Any]) -> ModelFinding:
    table_ident = row["table_ident"]
    column_ident = row["column_ident"]
    return ModelFinding(
        rule=RuleId.JSON_COLUMN,
        confidence=Confidence.CANDIDATE,
        severity=Severity.LOW,
        schema=row["schema"],
        table=row["table"],
        columns=[row["column"]],
        summary=f"{row['schema']}.{row['table']}.{row['column']} is json; jsonb would store the parsed value.",
        detail=(
            "json keeps the text exactly as written and parses it again on every read, and it has no equality "
            "operator, no containment (@>) and no GIN index. jsonb stores the parsed value and has all three. "
            "It is a candidate rather than a fact because the catalog cannot say whether anything reads the "
            "text back expecting its original key order, whitespace or duplicate keys, which jsonb does not "
            "keep. The verify query counts the rows with a duplicate top-level key, the one case where the "
            "conversion changes a value rather than its spelling. The conversion rewrites the table under an "
            "ACCESS EXCLUSIVE lock."
        ),
        remediation=[
            f"ALTER TABLE {table_ident} ALTER COLUMN {column_ident} TYPE jsonb USING {column_ident}::jsonb;",
        ],
        evidence={
            "column_type": row["column_type"],
            "row_count": int(row["row_count"] or 0),
            "table_bytes": int(row["table_bytes"] or 0),
        },
        verify=f"SELECT count(*) FROM {table_ident} WHERE {_DUPLICATE_KEYS.format(column=column_ident)};",
        rank=float(row["table_bytes"] or 0),
    )


async def blank_padded_chars(sql_driver: SqlDriver, options: ReviewOptions) -> list[ModelFinding]:
    """Find ``character(n)`` columns.

    ``char(n)`` pads every value out to n with spaces and then, depending on the
    operation, sometimes strips them again. So ``'ab'`` stored in a ``char(10)``
    compares equal to ``'ab        '``, its length is 10, and concatenating it
    carries the padding along. It costs space and surprises people; ``text`` and
    ``varchar(n)`` do neither.
    """
    sql, params = scoped(BLANK_PADDED_CHAR_SQL, options, "n.nspname")
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql, params)
    return [_blank_padded_finding(dict(row.cells)) for row in rows or []]


def _blank_padded_finding(row: dict[str, Any]) -> ModelFinding:
    declared_length = row["declared_length"]
    return ModelFinding(
        rule=RuleId.BLANK_PADDED_CHAR,
        confidence=Confidence.PROVEN,
        severity=Severity.LOW,
        schema=row["schema"],
        table=row["table"],
        columns=[row["column"]],
        summary=f"{row['schema']}.{row['table']}.{row['column']} is {row['column_type']}, which pads with spaces.",
        detail=(
            f"Every value is stored padded out to {declared_length} characters and compares as though the "
            f"padding were not there, so length() and concatenation disagree with equality about what the "
            f"value is. varchar({declared_length}) keeps the limit without the padding; text drops both."
        ),
        remediation=[
            f"ALTER TABLE {row['table_ident']} ALTER COLUMN {row['column_ident']} TYPE varchar({declared_length});",
        ],
        evidence={
            "column_type": row["column_type"],
            "declared_length": declared_length,
            "row_count": int(row["row_count"] or 0),
            "table_bytes": int(row["table_bytes"] or 0),
        },
        rank=float(row["table_bytes"] or 0),
    )


async def nullable_never_null(sql_driver: SqlDriver, options: ReviewOptions) -> list[ModelFinding]:
    """Find key columns that are nullable but hold no NULLs.

    Deliberately narrow. Most columns in most databases hold no NULLs, so
    reporting all of them would bury the schema in findings that say nothing.
    Restricted to foreign keys and unique columns it says something specific:
    either a relationship the schema calls optional is mandatory in every row,
    or a uniqueness constraint has a gap NULLs can pass through.

    Inferred from a sample, so it is a candidate — the verify query settles it
    in one scan.
    """
    sql, params = scoped(NULLABLE_NEVER_NULL_SQL, options, "n.nspname")
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql, [options.min_rows, *params])
    return [_never_null_finding(dict(row.cells)) for row in rows or []]


def _never_null_finding(row: dict[str, Any]) -> ModelFinding:
    table_ident = row["table_ident"]
    column_ident = row["column_ident"]
    constraint = row["constraint_name"]
    last_analyzed = row["last_analyzed"]
    in_foreign_key = bool(row["in_foreign_key"])

    if in_foreign_key:
        what_it_means = (
            "This column is a foreign key, so the schema says the relationship is optional while every "
            "row says it is required. Anything reading the table still has to handle a row without a "
            "parent, and nothing stops one being written."
        )
    else:
        what_it_means = (
            "This column is part of a unique index, and uniqueness does not apply between NULLs — two "
            "rows may both be NULL there however unique the index looks. Declaring it NOT NULL closes "
            "that gap."
        )

    return ModelFinding(
        rule=RuleId.NULLABLE_NEVER_NULL,
        confidence=Confidence.CANDIDATE,
        severity=Severity.LOW,
        schema=row["schema"],
        table=row["table"],
        columns=[row["column"]],
        summary=f"{row['schema']}.{row['table']}.{row['column']} is nullable but the sample holds no NULLs.",
        detail=(
            f"The planner's statistics, last collected {last_analyzed or 'at an unrecorded time'}, put the "
            f"NULL fraction of this column at zero across {int(row['row_count'] or 0):,} rows. "
            f"{what_it_means} The steps below add the constraint without taking a long exclusive lock: the "
            f"CHECK is validated while the table stays readable and writable, and SET NOT NULL then trusts "
            f"it instead of scanning again."
        ),
        remediation=[
            f"ALTER TABLE {table_ident} ADD CONSTRAINT {constraint} CHECK ({column_ident} IS NOT NULL) NOT VALID;",
            f"ALTER TABLE {table_ident} VALIDATE CONSTRAINT {constraint};",
            f"ALTER TABLE {table_ident} ALTER COLUMN {column_ident} SET NOT NULL;",
            f"ALTER TABLE {table_ident} DROP CONSTRAINT {constraint};",
        ],
        evidence={
            "column_type": row["column_type"],
            "null_frac": float(row["null_frac"] or 0.0),
            "row_count": int(row["row_count"] or 0),
            "last_analyzed": str(last_analyzed) if last_analyzed else None,
            "in_foreign_key": in_foreign_key,
            "in_unique_index": bool(row["in_unique_index"]),
        },
        verify=f"SELECT count(*) FROM {table_ident} WHERE {column_ident} IS NULL;",
        rank=float(row["table_bytes"] or 0),
    )


async def low_cardinality_text(sql_driver: SqlDriver, options: ReviewOptions) -> list[ModelFinding]:
    """Find text columns holding a handful of distinct values.

    A status, a country code, a type discriminator: stored as text it costs its
    own length on every row and nothing in the schema says which values are
    legal. This is inferred from a sample and the sample can miss values, which
    is exactly what would make the conversion below fail — hence the verify
    query.
    """
    sql, params = scoped(LOW_CARDINALITY_TEXT_SQL, options, "n.nspname")
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql, [options.min_rows, MAX_DISTINCT_VALUES, *params])
    return [_low_cardinality_finding(dict(row.cells)) for row in rows or []]


def _low_cardinality_finding(row: dict[str, Any]) -> ModelFinding:
    distinct_values = int(row["distinct_values"] or 0)
    avg_width = int(row["avg_width"] or 0)
    row_count = int(row["row_count"] or 0)
    common_values = list(row["common_values"] or [])
    table_ident = row["table_ident"]
    column_ident = row["column_ident"]
    impact_bytes = max(avg_width - _LOOKUP_KEY_BYTES, 0) * row_count

    remediation = [
        f"-- {distinct_values} distinct values over {row_count:,} rows. Either reference a lookup table,",
        "-- which lets the set of values carry its own attributes, or use an enum:",
    ]
    if common_values:
        enum_type = row["enum_type_ident"]
        values = ", ".join(f"'{value}'" for value in sorted(common_values))
        remediation.append(f"CREATE TYPE {enum_type} AS ENUM ({values});")
        remediation.append(f"ALTER TABLE {table_ident} ALTER COLUMN {column_ident} TYPE {enum_type} USING {column_ident}::{enum_type};")

    return ModelFinding(
        rule=RuleId.LOW_CARDINALITY_TEXT,
        confidence=Confidence.CANDIDATE,
        severity=Severity.LOW,
        schema=row["schema"],
        table=row["table"],
        columns=[row["column"]],
        summary=(f"{row['schema']}.{row['table']}.{row['column']} is {row['column_type']} with about {distinct_values} distinct values."),
        detail=(
            f"The column averages {avg_width} bytes a row and holds roughly {distinct_values} different "
            f"values, so it is a category written out in full {row_count:,} times — about "
            f"{impact_bytes:,} bytes more than an integer key would cost, and with nothing in the schema "
            f"restricting what may be stored. The values listed are the ones the sample saw; run the verify "
            f"query before converting, because a value the sample missed will make the conversion fail."
        ),
        remediation=remediation,
        evidence={
            "column_type": row["column_type"],
            "distinct_values": distinct_values,
            "avg_width": avg_width,
            "row_count": row_count,
            "sampled_values": sorted(common_values),
        },
        verify=f"SELECT {column_ident}, count(*) FROM {table_ident} GROUP BY 1 ORDER BY 2 DESC;",
        impact_bytes=impact_bytes,
        rank=float(impact_bytes),
    )


__all__ = [
    "MAX_DISTINCT_VALUES",
    "blank_padded_chars",
    "json_columns",
    "low_cardinality_text",
    "naive_timestamps",
    "nullable_never_null",
]

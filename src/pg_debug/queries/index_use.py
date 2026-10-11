"""The SQL behind the index-use analysis.

Two catalog reads, one per question. The first describes every index on the
tables a query touches, in the terms the analysis compares against the query:
each column as PostgreSQL itself renders it (``lower(email)`` for an
expression, ``email`` for a plain column), the operator class on each key
column, the partial-index predicate, and whether the index is usable at all.
The second is the collation the database compares text under, which decides
whether ``LIKE 'prefix%'`` can walk a btree.

Both take their tables two ways, because a query names its tables two ways: a
qualified name is matched exactly, and a bare name is matched the way the
server would resolve it, through ``search_path``.
"""

from __future__ import annotations

#: Every index on the given tables. ``$1`` is the qualified names, ``$2`` the
#: bare ones. ``columns`` holds each index column as pg_get_indexdef renders
#: it, key columns first and INCLUDE columns after ``key_columns``.
INDEXES_FOR_TABLES_SQL = """
SELECT
    n.nspname AS schema,
    c.relname AS table,
    ic.relname AS index,
    am.amname AS method,
    i.indisvalid AS valid,
    i.indisready AS ready,
    i.indisunique AS "unique",
    i.indnkeyatts AS key_columns,
    (SELECT array_agg(pg_get_indexdef(i.indexrelid, k.n, true) ORDER BY k.n)
       FROM generate_series(1, i.indnatts) AS k(n)) AS columns,
    (SELECT array_agg(oc.opcname ORDER BY k.n)
       FROM generate_series(1, i.indnkeyatts) AS k(n)
       JOIN pg_opclass oc ON oc.oid = i.indclass[k.n - 1]) AS opclasses,
    pg_get_expr(i.indpred, i.indrelid) AS predicate,
    pg_get_indexdef(i.indexrelid) AS definition,
    pg_relation_size(i.indexrelid) AS size_bytes,
    c.reltuples::float8 AS table_rows,
    c.relpages AS table_pages
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_am am ON am.oid = ic.relam
WHERE (n.nspname || '.' || c.relname) = ANY({}::text[])
   OR (c.relname = ANY({}::text[]) AND pg_table_is_visible(c.oid))
ORDER BY n.nspname, c.relname, ic.relname
"""

#: The columns of the given tables, so a bare column in a query can be told
#: which table it belongs to, and a column told apart from a type name.
COLUMNS_FOR_TABLES_SQL = """
SELECT n.nspname AS schema, c.relname AS table, a.attname AS column, format_type(a.atttypid, a.atttypmod) AS type
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p', 'm', 'v')
  AND ((n.nspname || '.' || c.relname) = ANY({}::text[])
       OR (c.relname = ANY({}::text[]) AND pg_table_is_visible(c.oid)))
ORDER BY n.nspname, c.relname, a.attnum
"""

#: The collation text is compared under unless a column says otherwise.
DATABASE_COLLATION_SQL = "SELECT datcollate AS collation FROM pg_database WHERE datname = current_database()"

#: Take sequential scans off the table for the pinned session, and put them
#: back. What the planner does when it cannot read the table whole is the one
#: experiment that separates "cannot use the index" from "chose not to".
DISABLE_SEQSCAN_SQL = "SET enable_seqscan = off"
RESET_SEQSCAN_SQL = "RESET enable_seqscan"


def predicate_as_statement(table: str, predicate: str) -> str:
    """A partial index's WHERE clause as a statement the parser will take.

    Nothing runs it; the predicate reader works on parsed statements, and the
    catalog hands the predicate back as a bare expression.
    """
    return f"SELECT 1 FROM {table} WHERE {predicate}"


__all__ = [
    "COLUMNS_FOR_TABLES_SQL",
    "DATABASE_COLLATION_SQL",
    "DISABLE_SEQSCAN_SQL",
    "INDEXES_FOR_TABLES_SQL",
    "RESET_SEQSCAN_SQL",
    "predicate_as_statement",
]

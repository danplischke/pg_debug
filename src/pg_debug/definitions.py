"""Fingerprints of the definitions findings are about, for a baseline to compare.

A baseline accepts a finding about an object. If the object is later defined
differently — the duplicate index rebuilt over other columns, the foreign key
pointed at another table, the column's type widened — the finding that names
it is no longer the one that was accepted, even though it names the same
object. A fingerprint is a hash of what the catalog says defines the object,
read as PostgreSQL itself prints it, so that the baseline can tell.

What is fingerprinted is what the finding is about:

- an index: ``pg_get_indexdef``; a duplicate index, with the index covering it;
- a constraint: ``pg_get_constraintdef``;
- columns: each one's name, type, ``NOT NULL``, collation and default;
- a table, for a data model finding about the whole table: its columns.

Health findings about a table's state (its bloat, its dead rows, its scans)
and configuration findings carry none. What they report is a measurement, not
a definition, and a table's columns changing does not make its bloat another
finding.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from .db import SafeSqlDriver
from .db import SqlDriver

#: Characters of the SHA-256 kept. Enough that two definitions never collide
#: in a baseline, short enough to read in a diff.
FINGERPRINT_LENGTH = 16

#: The health check whose findings' ``name`` is a constraint's.
_CONSTRAINT_CHECK = "Constraint health"

#: Each object's definition, one row per target. Every lookup is by name,
#: so an object that has gone or been renamed reads as NULL and gets no
#: fingerprint.
DEFINITIONS_SQL = """
WITH given AS (
    SELECT
        (t ->> 'i')::int AS i,
        t ->> 'kind' AS kind,
        t ->> 'schema' AS schema,
        t ->> 'table' AS "table",
        t ->> 'name' AS name,
        ARRAY(SELECT jsonb_array_elements_text(coalesce(t -> 'columns', '[]'::jsonb))) AS columns
    FROM jsonb_array_elements({}::jsonb) AS t
),
target AS (
    SELECT given.*, c.oid AS relid
    FROM given
    LEFT JOIN pg_namespace n ON n.nspname = given.schema
    LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = given."table"
)
SELECT
    target.i,
    CASE target.kind
        WHEN 'index' THEN (
            SELECT pg_get_indexdef(ic.oid)
            FROM pg_class ic
            JOIN pg_namespace n ON n.oid = ic.relnamespace
            WHERE n.nspname = target.schema AND ic.relname = target.name AND ic.relkind IN ('i', 'I')
        )
        WHEN 'constraint' THEN (
            SELECT pg_get_constraintdef(con.oid)
            FROM pg_constraint con
            WHERE con.conrelid = target.relid AND con.conname = target.name
        )
        WHEN 'columns' THEN (
            SELECT string_agg(
                format('%I %s%s%s%s',
                       a.attname,
                       format_type(a.atttypid, a.atttypmod),
                       CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END,
                       coalesce(' COLLATE ' || (SELECT quote_ident(co.collname) FROM pg_collation co WHERE co.oid = a.attcollation), ''),
                       coalesce(' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid), '')),
                ', ' ORDER BY col.ord)
            FROM unnest(target.columns) WITH ORDINALITY AS col(name, ord)
            JOIN pg_attribute a ON a.attrelid = target.relid AND a.attname = col.name AND NOT a.attisdropped
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            -- A column that has gone leaves nothing to compare.
            HAVING count(*) = cardinality(target.columns)
        )
        WHEN 'table' THEN (
            SELECT string_agg(
                format('%I %s%s', a.attname, format_type(a.atttypid, a.atttypmod), CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END),
                ', ' ORDER BY a.attnum)
            FROM pg_attribute a
            WHERE a.attrelid = target.relid AND a.attnum > 0 AND NOT a.attisdropped
        )
    END AS definition
FROM target
ORDER BY target.i
"""


def targets(command: str, key: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The objects whose definitions make up a finding's fingerprint; empty for none."""
    schema, table = key.get("schema"), key.get("table")
    if not schema:
        return []
    if command == "health":
        if key.get("index"):
            found = [{"kind": "index", "schema": schema, "name": key["index"]}]
            if key.get("covered_by"):
                found.append({"kind": "index", "schema": schema, "name": key["covered_by"]})
            return found
        if key.get("check") == _CONSTRAINT_CHECK and table and key.get("name"):
            return [{"kind": "constraint", "schema": schema, "table": table, "name": key["name"]}]
        if table and key.get("column"):
            return [{"kind": "columns", "schema": schema, "table": table, "columns": [key["column"]]}]
        return []
    if command == "model" and table:
        columns = list(key.get("columns") or [])
        if columns:
            return [{"kind": "columns", "schema": schema, "table": table, "columns": columns}]
        return [{"kind": "table", "schema": schema, "table": table}]
    return []


def fingerprint(text: str) -> str:
    """The short hash a baseline records for a definition."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


async def definition_fingerprints(sql_driver: SqlDriver | SafeSqlDriver, command: str, keys: Sequence[Mapping[str, Any]]) -> list[str | None]:
    """A fingerprint for each key, or None where nothing defines what it names.

    One statement reads every definition, whatever the number of keys.

    Args:
        sql_driver: The driver to read the catalog through.
        command: ``health``, ``model`` or ``config``: which findings the keys
            are of.
        keys: The findings' keys, as :mod:`pg_debug.baseline` makes them.

    Returns:
        One entry per key, in the same order. None for a finding that is not
        about a definition, and for one whose object could not be found.
    """
    plans = [targets(command, key) for key in keys]
    rows_in: list[dict[str, Any]] = []
    for plan in plans:
        for target in plan:
            rows_in.append({"i": len(rows_in), **target})
    if not rows_in:
        return [None] * len(keys)

    rows = await SafeSqlDriver.execute_param_query(sql_driver, DEFINITIONS_SQL, [json.dumps(rows_in)])
    definitions = {int(row.cells["i"]): row.cells.get("definition") for row in rows or []}

    result: list[str | None] = []
    position = 0
    for plan in plans:
        texts = [definitions.get(position + offset) for offset in range(len(plan))]
        position += len(plan)
        result.append(fingerprint("\n".join(texts)) if plan and all(texts) else None)  # type: ignore[arg-type]
    return result


__all__ = ["DEFINITIONS_SQL", "FINGERPRINT_LENGTH", "definition_fingerprints", "fingerprint", "targets"]

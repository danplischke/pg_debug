"""Collation versions that no longer match the library providing them.

A B-tree index on text is ordered by its collation, and the collation's
rules come from the C library or ICU. When an upgrade changes them, an index
built under the old rules no longer agrees with the new ones: a lookup can
miss a row that is there and a unique index can admit a duplicate, without
any query failing. PostgreSQL records the version of the library each
collation was created with and can ask the library for its version now; when
the two differ, the rules may have changed under the indexes.

Recorded since PostgreSQL 10 for ICU collations, 13 for libc ones, and 15 for
each database's default collation (``pg_database.datcollversion``). A
collation whose version was never recorded is left out, since it proves
nothing either way. A mismatch proves the library changed, not that an index
is corrupt; ``amcheck``'s ``bt_index_check`` can show that one is.
"""

from __future__ import annotations

from typing import Any

from ...db import SafeSqlDriver
from ...db import SqlDriver
from ...db import get_postgres_version
from ..result import HealthCheckResult

COLLATION_HEALTH = "Collation versions"

#: ``pg_database.datcollversion`` and ``pg_database_collation_actual_version``.
DATABASE_VERSION_MIN_VERSION = 15

#: The oid of the ``default`` collation: an index column that uses the
#: database's default collation records this one.
DEFAULT_COLLATION_OID = 100

#: How many dependent indexes a finding lists by name.
MAX_INDEXES = 100

DATABASES_SQL = """
SELECT
    datname AS database,
    quote_ident(datname) AS identifier,
    datname = current_database() AS is_current,
    datcollversion AS recorded_version,
    pg_database_collation_actual_version(oid) AS actual_version
FROM pg_database
WHERE datcollversion IS NOT NULL
ORDER BY datname
"""

# Only collations an index in this database uses: they are the ones whose
# change can corrupt something, and asking the library about every locale
# the server knows is slow.
COLLATIONS_SQL = """
WITH used AS (
    SELECT DISTINCT unnest(i.indcollation::oid[]) AS oid FROM pg_index i
)
SELECT
    n.nspname AS schema,
    c.collname AS collation,
    format('%I.%I', n.nspname, c.collname) AS identifier,
    CASE c.collprovider WHEN 'i' THEN 'icu' WHEN 'c' THEN 'libc' WHEN 'b' THEN 'builtin' ELSE c.collprovider::text END AS provider,
    c.collversion AS recorded_version,
    pg_collation_actual_version(c.oid) AS actual_version,
    c.oid::bigint AS collation_oid
FROM pg_collation c
JOIN used u ON u.oid = c.oid
JOIN pg_namespace n ON n.oid = c.collnamespace
WHERE c.collversion IS NOT NULL
ORDER BY 1, 2
"""

INDEXES_SQL = """
SELECT
    format('%I.%I', n.nspname, ic.relname) AS index,
    format('%I.%I', n.nspname, tc.relname) AS table
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_class tc ON tc.oid = i.indrelid
JOIN pg_namespace n ON n.oid = ic.relnamespace
WHERE {} = ANY (i.indcollation::oid[])
ORDER BY 1
"""


class CollationHealthCalc:
    """Reports collations whose recorded version differs from the library's, and the indexes that depend on them."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def collation_version_check(self) -> HealthCheckResult:
        """Find databases and collations whose rules may have changed under their indexes.

        A ``database`` finding is a database whose default collation's
        recorded version differs from the library's; for the database
        connected to, it lists the indexes that use the default collation.
        A ``collation`` finding is a collation used by an index in this
        database whose recorded version differs; it lists those indexes.
        Each carries the statements that rebuild the indexes and record the
        new version.
        """
        version = await get_postgres_version(self.sql_driver)
        findings: list[dict[str, Any]] = []
        databases: list[dict[str, Any]] = []
        if version >= DATABASE_VERSION_MIN_VERSION:
            databases = [dict(row.cells) for row in await SafeSqlDriver.execute_param_query(self.sql_driver, DATABASES_SQL) or []]
        collations = [dict(row.cells) for row in await SafeSqlDriver.execute_param_query(self.sql_driver, COLLATIONS_SQL) or []]

        for database in databases:
            if not _drifted(database):
                continue
            finding: dict[str, Any] = {
                "kind": "database",
                "database": database["database"],
                "identifier": database["identifier"],
                "recorded_version": database["recorded_version"],
                "actual_version": database["actual_version"],
            }
            if database["is_current"]:
                await self._attach_indexes(finding, DEFAULT_COLLATION_OID)
            else:
                finding["indexes"] = None
            finding["fix"] = _database_fix(finding)
            findings.append(finding)

        for collation in collations:
            if not _drifted(collation):
                continue
            finding = {
                "kind": "collation",
                "schema": collation["schema"],
                "collation": collation["collation"],
                "identifier": collation["identifier"],
                "provider": collation["provider"],
                "recorded_version": collation["recorded_version"],
                "actual_version": collation["actual_version"],
            }
            await self._attach_indexes(finding, int(collation["collation_oid"]))
            finding["fix"] = _collation_fix(finding)
            findings.append(finding)

        metrics: dict[str, Any] = {
            "databases_examined": len(databases),
            "database_versions_recorded": version >= DATABASE_VERSION_MIN_VERSION,
            "collations_examined": len(collations),
            "collations_examined_names": [f"{c['schema']}.{c['collation']}" for c in collations],
        }

        lines: list[str] = []
        if findings:
            lines.append("Collations whose library version changed since they were recorded; the indexes on them may be out of order:")
            lines += [_describe(finding) for finding in findings]
            lines.append(
                "A mismatch proves the library changed, not that an index is corrupt; amcheck's bt_index_check shows whether one is. "
                "Rebuild the indexes before recording the new version: recording it first silences the warning and leaves them as they are."
            )
        else:
            examined = f"{len(collations)} {'collation' if len(collations) == 1 else 'collations'} used by this database's indexes"
            if version >= DATABASE_VERSION_MIN_VERSION:
                examined += f" and the default collation of {len(databases)} {'database' if len(databases) == 1 else 'databases'}"
            lines.append(
                f"No recorded collation version differs from its library: compared {examined}. "
                "A collation that records no version, as C and POSIX do not, has nothing to compare and is not counted."
            )
        if version < DATABASE_VERSION_MIN_VERSION:
            lines.append(
                f"PostgreSQL {version} does not record the database default collation's version, so a change under it cannot be seen here; "
                "the OS upgrade history and amcheck are the evidence."
            )

        return HealthCheckResult(
            name=COLLATION_HEALTH,
            healthy=not findings,
            summary="\n".join(lines),
            findings=findings,
            metrics=metrics,
        )

    async def _attach_indexes(self, finding: dict[str, Any], collation_oid: int) -> None:
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, INDEXES_SQL, [collation_oid])
        indexes = [dict(row.cells) for row in rows or []]
        finding["index_count"] = len(indexes)
        finding["indexes"] = indexes[:MAX_INDEXES]


def _drifted(row: dict[str, Any]) -> bool:
    """A recorded version the library now reports differently. No version from the library proves nothing."""
    return row.get("actual_version") is not None and row.get("recorded_version") != row.get("actual_version")


def _reindex(finding: dict[str, Any]) -> list[str]:
    return [f"REINDEX INDEX CONCURRENTLY {index['index']};" for index in finding.get("indexes") or []]


def _database_fix(finding: dict[str, Any]) -> list[str]:
    name = finding["identifier"]
    if finding.get("indexes") is None:
        # Not the database connected to: its indexes cannot be listed from here.
        return [
            f"-- connected to {name}: rebuild the indexes on text columns that use the default collation",
            f"ALTER DATABASE {name} REFRESH COLLATION VERSION;",
        ]
    return [*_reindex(finding), f"ALTER DATABASE {name} REFRESH COLLATION VERSION;"]


def _collation_fix(finding: dict[str, Any]) -> list[str]:
    return [*_reindex(finding), f"ALTER COLLATION {finding['identifier']} REFRESH VERSION;"]


def _describe(finding: dict[str, Any]) -> str:
    versions = f"recorded {finding['recorded_version']}, library now {finding['actual_version']}"
    if finding["kind"] == "database":
        subject = f"Database '{finding['database']}' default collation ({versions})"
        if finding.get("indexes") is None:
            return f"- {subject}: connect to it to list the indexes that use it."
    else:
        subject = f"Collation '{finding['schema']}.{finding['collation']}', {finding['provider']} ({versions})"
    count = int(finding.get("index_count") or 0)
    if not count:
        return f"- {subject}: no index uses it."
    names = ", ".join(index["index"] for index in finding["indexes"][:5])
    more = f" and {count - 5} more" if count > 5 else ""
    return f"- {subject}: {count} {'index depends' if count == 1 else 'indexes depend'} on it: {names}{more}."


__all__ = ["COLLATION_HEALTH", "CollationHealthCalc"]

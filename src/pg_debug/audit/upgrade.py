"""Will ``pg_upgrade`` take this cluster to the next major version, and what will it leave behind?

``pg_upgrade --check`` answers the first half, but only once the new version
is installed beside the old one, on the machine, with both data directories
at hand. This answers what it can from a connection, before any of that: the
things ``pg_upgrade`` refuses outright, the things it carries across silently
incomplete, and the work it leaves for after. Each check names the source it
rests on, and the rules are those of the PostgreSQL 18 ``pg_upgrade``:

- ``reg*`` columns: ``src/bin/pg_upgrade/check.c``, and the "Notes" of the
  ``pg_upgrade`` documentation: ``regcollation``, ``regconfig``,
  ``regdictionary``, ``regnamespace``, ``regoper``, ``regoperator``,
  ``regproc`` and ``regprocedure`` are refused, in a column of their own or
  inside a domain, array, composite or range over one; ``regclass``,
  ``regrole`` and ``regtype`` are not, their OIDs being preserved.
- Prepared transactions: ``check_for_prepared_transactions`` in the same file
  refuses a cluster that has any.
- Logical slots and subscriptions: "Migration of logical slots is only
  supported when the old cluster is version 17.0 or later. Logical slots on
  clusters before version 17.0 will silently be ignored" (and the same of
  subscriptions), from "Upgrade" in the logical replication chapter.
- Checksums: ``controldata.c`` refuses old and new clusters whose checksum
  settings differ, and initdb enables checksums by default from 18 (18
  release notes).
- Statistics: ``pg_upgrade`` 18 carries optimizer statistics, but "Extended
  statistics are not preserved" (18 release notes); before 18 it carried
  none.
- MD5 passwords: deprecated in 18, to be removed in a future major release
  (18 release notes).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..db.extensions import get_postgres_version
from ..errors import InvalidArgumentError
from ..platform import PlatformDetection
from ..severity import Confidence
from ..severity import Severity
from .result import AuditFinding
from .result import AuditReport
from .result import CheckOutcome
from .result import not_applicable
from .result import outcome_of
from .result import plural
from .result import unknown
from .versions import NEWEST_MAJOR
from .versions import split_version

logger = logging.getLogger(__name__)

UPGRADE_CHECK = "upgrade-check"

Checked = tuple[CheckOutcome, list[AuditFinding]]

#: The ``reg*`` types whose values are OIDs ``pg_upgrade`` does not preserve.
REFUSED_REG_TYPES = (
    "regcollation",
    "regconfig",
    "regdictionary",
    "regnamespace",
    "regoper",
    "regoperator",
    "regproc",
    "regprocedure",
)

SERVER_SQL = """
SELECT
    current_setting('server_version_num')::int AS version_num,
    current_setting('server_version') AS version,
    current_setting('data_checksums') AS data_checksums,
    current_database() AS database,
    has_table_privilege('pg_catalog.pg_authid', 'SELECT') AS reads_authid,
    (
        SELECT array_agg(datname ORDER BY datname)
        FROM pg_database
        WHERE datallowconn AND NOT datistemplate AND datname <> current_database()
    ) AS other_databases
"""

PREPARED_TRANSACTIONS_SQL = """
SELECT gid, prepared, owner, database, now() - prepared AS held_for
FROM pg_prepared_xacts
ORDER BY prepared
"""

# What pg_upgrade's check_for_data_types_usage() runs, over the same types:
# the refused types, then every domain, array, composite and range built on
# any type found so far, then the columns of tables, materialized views and
# indexes that store one. Older servers lack some of the types, which is why
# they are matched by name rather than cast to regtype.
REG_TYPE_COLUMNS_SQL = """
WITH RECURSIVE oids AS (
    SELECT t.oid
    FROM pg_type t
    WHERE t.typnamespace = 'pg_catalog'::regnamespace
      AND t.typname = ANY({}::text[])
    UNION ALL
    SELECT * FROM (
        WITH x AS (SELECT oid FROM oids)
        SELECT t.oid FROM pg_type t, x WHERE t.typbasetype = x.oid AND t.typtype = 'd'
        UNION ALL
        SELECT t.oid FROM pg_type t, x WHERE t.typelem = x.oid AND t.typtype = 'b'
        UNION ALL
        SELECT t.oid
        FROM pg_type t, pg_class c, pg_attribute a, x
        WHERE t.typtype = 'c' AND t.oid = c.reltype AND c.oid = a.attrelid AND NOT a.attisdropped AND a.atttypid = x.oid
        UNION ALL
        SELECT t.oid FROM pg_type t, pg_range r, x WHERE t.typtype = 'r' AND r.rngtypid = t.oid AND r.rngsubtype = x.oid
    ) foo
)
SELECT
    n.nspname AS schema,
    c.relname AS relation,
    c.relkind,
    a.attname AS column,
    format_type(a.atttypid, a.atttypmod) AS column_type,
    format('%I.%I', n.nspname, c.relname) AS relation_ident,
    quote_ident(a.attname) AS column_ident
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE NOT a.attisdropped
  AND a.atttypid IN (SELECT oid FROM oids)
  AND c.relkind IN ('r', 'm', 'i')
  AND n.nspname !~ '^pg_temp_'
  AND n.nspname !~ '^pg_toast_temp_'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1, 2, 4
"""

LOGICAL_SLOTS_SQL = """
SELECT slot_name, plugin, database, active, wal_status
FROM pg_replication_slots
WHERE slot_type = 'logical' AND NOT temporary
ORDER BY slot_name
"""

# PostgreSQL 17 records why a slot was invalidated; pg_upgrade refuses a
# cluster with an invalidated logical slot.
LOGICAL_SLOTS_17_SQL = """
SELECT slot_name, plugin, database, active, wal_status, invalidation_reason
FROM pg_replication_slots
WHERE slot_type = 'logical' AND NOT temporary
ORDER BY slot_name
"""

# Only the columns PUBLIC may read: subconninfo is revoked from it.
SUBSCRIPTIONS_SQL = """
SELECT s.subname, d.datname AS database
FROM pg_subscription s
JOIN pg_database d ON d.oid = s.subdbid
ORDER BY d.datname, s.subname
"""

EXTENDED_STATISTICS_SQL = """
SELECT
    sn.nspname AS schema,
    s.stxname AS statistics,
    format('%I.%I', n.nspname, c.relname) AS table_ident,
    n.nspname || '.' || c.relname AS table_name,
    s.stxkind::text[] AS kinds
FROM pg_statistic_ext s
JOIN pg_namespace sn ON sn.oid = s.stxnamespace
JOIN pg_class c ON c.oid = s.stxrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%'
ORDER BY 1, 2
"""

USER_TABLES_SQL = """
SELECT count(*) AS tables
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'm', 'p')
  AND n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%'
"""

EXTENSION_VERSIONS_SQL = """
SELECT
    e.extname AS extension,
    e.extversion AS installed_version,
    a.default_version,
    quote_ident(e.extname) AS extension_ident,
    count(*) OVER () AS examined
FROM pg_extension e
LEFT JOIN pg_available_extensions a ON a.name = e.extname
ORDER BY e.extname
"""

# pg_authid is the one place a role's password hash is, and it is readable
# by superusers only; the caller checks before running this.
MD5_PASSWORDS_SQL = """
SELECT
    rolname AS role,
    rolcanlogin AS can_login,
    count(*) OVER () AS examined
FROM pg_authid
WHERE rolpassword LIKE 'md5%'
ORDER BY rolname
"""

ROLES_WITH_PASSWORDS_SQL = """
SELECT count(*) AS roles FROM pg_authid WHERE rolpassword IS NOT NULL
"""


class UpgradeChecker:
    """Reads what stands between this cluster and a ``pg_upgrade`` to a newer major version."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver, platform: PlatformDetection | None = None):
        """
        Args:
            sql_driver: The driver to read through. Every statement is a read.
            platform: The managed platform the server runs on, if any. There
                the provider creates the new cluster, so its checksum setting
                is not the reader's to choose.
        """
        self.sql_driver = sql_driver
        self.platform = platform

    async def check(self, to: int | None = None) -> AuditReport:
        """Check the cluster for an upgrade to major version ``to``.

        Args:
            to: The major version to upgrade to. Defaults to the newest major
                version pg-debug knows was released, or the next one when the
                server already runs that.

        Raises:
            InvalidArgumentError: If ``to`` is not newer than the server.
        """
        async with self.sql_driver.session() as driver:
            rows = await driver.execute_query(SERVER_SQL)
            server = dict(rows[0].cells) if rows else {}
            major = await get_postgres_version(driver)

            target = to if to is not None else max(NEWEST_MAJOR, major + 1)
            if target <= major:
                raise InvalidArgumentError(f"--to {target} is not an upgrade: the server runs PostgreSQL {server.get('version') or major}.")

            _, minor = split_version(int(server.get("version_num") or 0))
            report = AuditReport(command=UPGRADE_CHECK, server_version=f"{major}.{minor}", target_version=target)
            if target > NEWEST_MAJOR:
                report.notes.append(
                    f"pg-debug knows pg_upgrade's rules up to PostgreSQL {NEWEST_MAJOR}; anything {target} adds is not checked here. "
                    f"Run pg_upgrade --check from the {target} binaries before the upgrade."
                )
            others = list(server.get("other_databases") or [])
            if others:
                report.notes.append(
                    f"Column types and extensions are read from database {server.get('database')} only; pg_upgrade checks every "
                    f"database, so run this against each of the others too: {', '.join(others)}."
                )

            # One after another: a pinned connection carries one statement at a time.
            checks: list[Callable[[], Awaitable[Checked]]] = [
                lambda: self._prepared_transactions(driver),
                lambda: self._reg_type_columns(driver),
                lambda: self._logical_replication(driver, major),
                lambda: self._data_checksums(server, target),
                lambda: self._extended_statistics(driver, target),
                lambda: self._extension_versions(driver),
                lambda: self._md5_passwords(driver, server),
            ]
            for run in checks:
                outcome, findings = await run()
                report.checks.append(outcome)
                report.findings.extend(findings)

        report.findings.sort(key=lambda finding: finding.sort_key)
        return report

    # -- What pg_upgrade refuses ----------------------------------------------

    async def _prepared_transactions(self, driver: SqlDriver) -> Checked:
        check = "prepared_transactions"
        rows = [dict(row.cells) for row in await driver.execute_query(PREPARED_TRANSACTIONS_SQL) or []]
        findings = [
            AuditFinding(
                check=check,
                subject=f"transaction {row['gid']}",
                severity=Severity.HIGH,
                confidence=Confidence.PROVEN,
                summary=f"Prepared transaction {row['gid']!r} in database {row['database']} has been waiting since {row['prepared']}.",
                detail=(
                    'pg_upgrade refuses a cluster with any prepared transaction ("The source cluster contains prepared '
                    'transactions"). A prepared transaction also holds its locks and keeps VACUUM from removing rows '
                    "newer than it until someone commits or rolls it back, so one left since "
                    f"{row['prepared']} is worth resolving now, upgrade or not."
                ),
                remediation=[
                    f"-- In database {row['database']}, once you know which outcome its client intended:",
                    f"COMMIT PREPARED '{_literal(row['gid'])}';  -- or: ROLLBACK PREPARED '{_literal(row['gid'])}';",
                ],
                evidence={
                    "gid": row["gid"],
                    "prepared": str(row["prepared"]),
                    "owner": row["owner"],
                    "database": row["database"],
                    "held_for": str(row["held_for"]),
                },
            )
            for row in rows
        ]
        return outcome_of(check, f"{plural(len(rows), 'prepared transaction')} in the cluster", findings), findings

    async def _reg_type_columns(self, driver: SqlDriver) -> Checked:
        check = "reg_type_columns"
        rows = await SafeSqlDriver.execute_param_query(driver, REG_TYPE_COLUMNS_SQL, [list(REFUSED_REG_TYPES)])
        findings = []
        for row in [dict(r.cells) for r in rows or []]:
            kind = {"r": "table", "m": "materialized view", "i": "index"}.get(row["relkind"], "relation")
            findings.append(
                AuditFinding(
                    check=check,
                    subject=f"{row['schema']}.{row['relation']}.{row['column']}",
                    severity=Severity.HIGH,
                    confidence=Confidence.PROVEN,
                    summary=f"{row['schema']}.{row['relation']}.{row['column']} is {row['column_type']}, which pg_upgrade refuses.",
                    detail=(
                        f"The {kind}'s column stores OIDs of type {row['column_type']}, and pg_upgrade does not preserve the "
                        'OIDs it refers to, so it stops with "Your installation contains one of the reg* data types in '
                        'user tables". regclass, regrole and regtype are allowed; this type, or the type it is built on, '
                        "is not. Store the name instead, as text, and convert where it is read."
                    ),
                    remediation=(
                        [f"ALTER TABLE {row['relation_ident']} ALTER COLUMN {row['column_ident']} TYPE text;"]
                        if row["relkind"] == "r" and row["column_type"] in REFUSED_REG_TYPES
                        else [f"-- Rebuild {row['relation_ident']} without the {row['column_type']} column before upgrading."]
                    ),
                    evidence={"relkind": row["relkind"], "column_type": row["column_type"]},
                )
            )
        return outcome_of(check, "every column in this database, for the types pg_upgrade refuses", findings), findings

    async def _logical_replication(self, driver: SqlDriver, major: int) -> Checked:
        check = "logical_replication"
        slots_sql = LOGICAL_SLOTS_17_SQL if major >= 17 else LOGICAL_SLOTS_SQL
        slots = [dict(row.cells) for row in await driver.execute_query(slots_sql) or []]
        subscriptions = [dict(row.cells) for row in await driver.execute_query(SUBSCRIPTIONS_SQL) or []]
        examined = f"{plural(len(slots), 'logical replication slot')} and {plural(len(subscriptions), 'subscription')}"

        findings: list[AuditFinding] = []
        if major >= 17:
            # From 17 pg_upgrade carries both, and refuses a slot it cannot use.
            for slot in slots:
                if slot.get("invalidation_reason") is None and slot.get("wal_status") != "lost":
                    continue
                findings.append(
                    AuditFinding(
                        check=check,
                        subject=f"slot {slot['slot_name']}",
                        severity=Severity.HIGH,
                        confidence=Confidence.PROVEN,
                        summary=f"Logical slot {slot['slot_name']} is invalidated, and pg_upgrade refuses a cluster with one.",
                        detail=(
                            f"The slot can no longer decode ({slot.get('invalidation_reason') or 'wal_status lost'}), and pg_upgrade "
                            "requires every logical slot to be usable. Drop it, and resynchronize whatever consumed it."
                        ),
                        remediation=[f"SELECT pg_drop_replication_slot('{_literal(slot['slot_name'])}');"],
                        evidence={k: slot.get(k) for k in ("plugin", "database", "active", "wal_status", "invalidation_reason")},
                    )
                )
            return outcome_of(check, examined, findings), findings

        for slot in slots:
            findings.append(
                AuditFinding(
                    check=check,
                    subject=f"slot {slot['slot_name']}",
                    severity=Severity.HIGH,
                    confidence=Confidence.PROVEN,
                    summary=f"Logical slot {slot['slot_name']} will not exist after pg_upgrade from {major}.",
                    detail=(
                        f'pg_upgrade migrates logical slots only from PostgreSQL 17 on; from {major} they "will silently be '
                        'ignored". Whatever reads this slot (a subscriber, a CDC pipeline) loses its place, and the changes '
                        "made between the upgrade and recreating the slot are never decoded. Plan to recreate the slot and "
                        "resynchronize its consumer, or upgrade the publisher by logical replication instead."
                    ),
                    remediation=[
                        f"-- After the upgrade: SELECT pg_create_logical_replication_slot('{_literal(slot['slot_name'])}', '{slot['plugin']}');"
                    ],
                    evidence={k: slot.get(k) for k in ("plugin", "database", "active", "wal_status")},
                )
            )
        for subscription in subscriptions:
            findings.append(
                AuditFinding(
                    check=check,
                    subject=f"subscription {subscription['subname']}",
                    severity=Severity.HIGH,
                    confidence=Confidence.PROVEN,
                    summary=f"Subscription {subscription['subname']} will lose its replication progress in pg_upgrade from {major}.",
                    detail=(
                        f"pg_upgrade migrates a subscription's table states and replication origin only from PostgreSQL 17 on; "
                        f"from {major} they are silently ignored, so the subscription comes back without knowing where it "
                        "had got to, and has to copy its tables again."
                    ),
                    remediation=[
                        f"-- In database {subscription['database']}, after the upgrade: drop and recreate the subscription, which copies "
                        "its tables again, or upgrade this subscriber with logical replication instead."
                    ],
                    evidence={"database": subscription["database"]},
                )
            )
        return outcome_of(check, examined, findings), findings

    async def _data_checksums(self, server: dict[str, Any], target: int) -> Checked:
        check = "data_checksums"
        if self.platform is not None and self.platform.known:
            return not_applicable(
                check, f"On {self.platform.platform.label} the provider creates the new cluster and chooses its checksum setting."
            ), []

        enabled = str(server.get("data_checksums") or "").lower() == "on"
        new_default = target >= 18
        examined = f"data_checksums = {'on' if enabled else 'off'}; initdb {target} defaults to {'on' if new_default else 'off'}"
        if enabled == new_default:
            return outcome_of(check, examined, []), []

        if new_default:
            detail = (
                f"From PostgreSQL 18, initdb enables data checksums unless told not to, and pg_upgrade refuses an old and new "
                f'cluster whose settings differ ("old cluster does not use data checksums but the new one does"). This cluster '
                f"has them off, so a new {target} cluster initialized with the defaults cannot receive it."
            )
            remediation = [
                f"-- Either initialize the {target} cluster without them:",
                "initdb --no-data-checksums ...",
                "-- or enable them on this cluster first, with it shut down (reads and rewrites every block):",
                "pg_checksums --enable -D <old data directory>",
            ]
        else:
            detail = (
                f"This cluster has data checksums on, and initdb {target} leaves them off by default; pg_upgrade refuses an old "
                'and new cluster whose settings differ ("old cluster uses data checksums but the new one does not").'
            )
            remediation = [f"initdb --data-checksums ...  -- for the {target} cluster"]

        finding = AuditFinding(
            check=check,
            subject="data_checksums",
            severity=Severity.MEDIUM,
            confidence=Confidence.PROVEN,
            summary=(
                f"data_checksums is {'on' if enabled else 'off'}, and a default initdb {target} cluster has them {'on' if new_default else 'off'}."
            ),
            detail=detail,
            remediation=remediation,
            evidence={"data_checksums": "on" if enabled else "off", "target_initdb_default": "on" if new_default else "off"},
        )
        return outcome_of(check, examined, [finding]), [finding]

    # -- What pg_upgrade leaves for after -------------------------------------

    async def _extended_statistics(self, driver: SqlDriver, target: int) -> Checked:
        check = "statistics_after_upgrade"
        if target < 18:
            rows = await driver.execute_query(USER_TABLES_SQL)
            tables = int(rows[0].cells["tables"]) if rows else 0
            finding = AuditFinding(
                check=check,
                subject="optimizer statistics",
                severity=Severity.LOW,
                confidence=Confidence.PROVEN,
                summary=f"pg_upgrade to {target} carries no optimizer statistics; all {plural(tables, 'table')} need ANALYZE after it.",
                detail=(
                    "Until the tables are analyzed, the planner estimates every one of them from defaults, which is how a fast "
                    "query becomes a slow one on the first morning after an upgrade. --analyze-in-stages gets rough statistics "
                    "in place quickly and refines them after."
                ),
                remediation=["vacuumdb --all --analyze-in-stages"],
                evidence={"tables": tables},
            )
            return outcome_of(check, f"{plural(tables, 'table')} in this database", [finding]), [finding]

        rows = [dict(row.cells) for row in await driver.execute_query(EXTENDED_STATISTICS_SQL) or []]
        findings = [
            AuditFinding(
                check=check,
                subject=f"{row['schema']}.{row['statistics']}",
                severity=Severity.LOW,
                confidence=Confidence.PROVEN,
                summary=f"Extended statistics {row['schema']}.{row['statistics']} on {row['table_name']} will be empty after pg_upgrade.",
                detail=(
                    'pg_upgrade carries the per-column optimizer statistics from 18 on, but "Extended statistics are not '
                    'preserved": the object comes across without its data, and the planner treats the columns as '
                    "independent again until the table is analyzed. That is the misestimate CREATE STATISTICS was made to fix."
                ),
                remediation=[
                    f"ANALYZE {row['table_ident']};",
                    "-- or, for every relation missing statistics at once (needs a superuser):",
                    "vacuumdb --all --analyze-in-stages --missing-stats-only",
                ],
                evidence={"kinds": list(row["kinds"] or []), "table": row["table_name"]},
            )
            for row in rows
        ]
        return outcome_of(check, f"{plural(len(rows), 'extended statistics object')} in this database", findings), findings

    async def _extension_versions(self, driver: SqlDriver) -> Checked:
        check = "extension_versions"
        rows = [dict(row.cells) for row in await driver.execute_query(EXTENSION_VERSIONS_SQL) or []]
        findings = []
        for row in rows:
            if row["default_version"] is None or row["default_version"] == row["installed_version"]:
                continue
            findings.append(
                AuditFinding(
                    check=check,
                    subject=f"extension {row['extension']}",
                    severity=Severity.LOW,
                    confidence=Confidence.PROVEN,
                    summary=(
                        f"Extension {row['extension']} is installed at {row['installed_version']}; this server's files make "
                        f"{row['default_version']} the default."
                    ),
                    detail=(
                        f"The extension's objects in this database are those of version {row['installed_version']}, while the "
                        f"files installed on the server offer {row['default_version']}. pg_upgrade keeps the installed version, so "
                        "the gap crosses the upgrade with it, on top of whatever the new major version's packages change. "
                        "Updating now separates the two changes."
                    ),
                    remediation=[f"ALTER EXTENSION {row['extension_ident']} UPDATE;"],
                    evidence={"installed_version": row["installed_version"], "default_version": row["default_version"]},
                )
            )
        return outcome_of(check, f"{plural(len(rows), 'extension')} installed in this database", findings), findings

    async def _md5_passwords(self, driver: SqlDriver, server: dict[str, Any]) -> Checked:
        check = "md5_passwords"
        if not server.get("reads_authid"):
            return (
                unknown(
                    check,
                    "nothing",
                    "Password hashes are in pg_authid, which only a superuser may read, and the connecting role cannot. "
                    "A superuser can run: SELECT rolname FROM pg_authid WHERE rolpassword LIKE 'md5%';",
                ),
                [],
            )

        rows = [dict(row.cells) for row in await driver.execute_query(MD5_PASSWORDS_SQL) or []]
        counted = await driver.execute_query(ROLES_WITH_PASSWORDS_SQL)
        with_passwords = int(counted[0].cells["roles"]) if counted else 0
        findings = [
            AuditFinding(
                check=check,
                subject=f"role {row['role']}",
                severity=Severity.MEDIUM if row["can_login"] else Severity.LOW,
                confidence=Confidence.PROVEN,
                summary=f"Role {row['role']} has an MD5 password hash.",
                detail=(
                    'PostgreSQL 18 deprecates MD5 passwords, and says support "will be removed in a future major version '
                    'release"; setting one there emits a warning. The hash is carried across pg_upgrade as it is, so the role '
                    "keeps it until its password is set again with password_encryption = 'scram-sha-256'. A pg_hba.conf "
                    "rule of md5 accepts a SCRAM password too, so the change needs no coordinated switch."
                    + ("" if row["can_login"] else " The role cannot log in, so the hash is unused today.")
                ),
                remediation=[
                    "SET password_encryption = 'scram-sha-256';",
                    f"\\password {row['role']}  -- in psql, so the password is hashed before it is sent",
                ],
                evidence={"can_login": bool(row["can_login"])},
            )
            for row in rows
        ]
        return outcome_of(check, f"{plural(with_passwords, 'role')} with a password", findings), findings


def _literal(value: Any) -> str:
    """A value fit to sit between single quotes in a statement shown to the reader."""
    return str(value).replace("'", "''")


__all__ = ["REFUSED_REG_TYPES", "UPGRADE_CHECK", "UpgradeChecker"]

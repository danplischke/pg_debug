"""What the server's own catalog and settings say about who can get in, and what they can do there.

This is not a penetration test, and it does not guess. Each check reads one
thing the server records (an authentication rule, a setting, a role's
attributes, a schema's privileges, a table's row-level security) and argues
from it. Two of them need more than an ordinary role: the authentication
rules are readable by a superuser only, and the server's version is judged
against releases pg-debug knew of when it was built. When a check cannot
read what it needs, it says so, and the report says it is incomplete.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import date
from typing import Any

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..platform import PlatformDetection
from ..severity import SEVERITY_ORDER
from ..severity import Confidence
from ..severity import Severity
from .result import AuditFinding
from .result import AuditReport
from .result import CheckOutcome
from .result import outcome_of
from .result import plural
from .result import unknown
from .versions import KNOWN_AS_OF
from .versions import LATEST_MINOR_RELEASE_DATE
from .versions import NEWEST_MAJOR
from .versions import RELEASES
from .versions import split_version

logger = logging.getLogger(__name__)

SECURITY = "security"

Checked = tuple[CheckOutcome, list[AuditFinding]]

#: Support ending within this many days is reported before it ends.
SUPPORT_ENDING_DAYS = 180

SERVER_SQL = """
SELECT
    current_setting('server_version_num')::int AS version_num,
    current_setting('server_version') AS version,
    current_setting('ssl') AS ssl,
    current_setting('password_encryption') AS password_encryption,
    current_date AS today,
    current_user AS current_role,
    has_table_privilege('pg_catalog.pg_hba_file_rules', 'SELECT')
        AND has_function_privilege('pg_catalog.pg_hba_file_rules()', 'EXECUTE') AS reads_hba
"""

# The columns every supported version has; rule_number and file_name came in 15.
HBA_RULES_SQL = """
SELECT line_number, type, database, user_name, address, netmask, auth_method, error
FROM pg_hba_file_rules
ORDER BY line_number
"""

PUBLIC_CREATE_SQL = """
SELECT n.nspname AS schema, quote_ident(n.nspname) AS schema_ident, n.nspacl::text AS acl
FROM pg_namespace n
CROSS JOIN LATERAL aclexplode(n.nspacl) a
WHERE a.grantee = 0
  AND a.privilege_type = 'CREATE'
  AND n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%'
ORDER BY n.nspname
"""

USER_SCHEMAS_SQL = """
SELECT count(*) AS schemas
FROM pg_namespace n
WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%'
"""

PRIVILEGED_ROLES_SQL = """
SELECT
    r.rolname AS role,
    quote_ident(r.rolname) AS role_ident,
    r.rolsuper AS superuser,
    r.rolbypassrls AS bypassrls,
    r.oid = 10 AS bootstrap,
    r.rolname = current_user AS connected_as,
    (SELECT count(*) FROM pg_roles WHERE rolcanlogin) AS login_roles,
    EXISTS (SELECT 1 FROM pg_class WHERE relrowsecurity) AS rls_in_use
FROM pg_roles r
WHERE r.rolcanlogin AND (r.rolsuper OR r.rolbypassrls)
ORDER BY r.rolname
"""

LOGIN_ROLES_SQL = "SELECT count(*) AS login_roles FROM pg_roles WHERE rolcanlogin"

# Tables with row-level security on and no policy at all: everyone but the
# owner (and superusers, and BYPASSRLS roles) sees no rows and may write none.
RLS_WITHOUT_POLICY_SQL = """
SELECT
    n.nspname AS schema,
    c.relname AS table,
    format('%I.%I', n.nspname, c.relname) AS table_ident,
    c.relforcerowsecurity AS forced,
    pg_get_userbyid(c.relowner) AS owner
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND c.relrowsecurity
  AND NOT EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid)
  AND n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%'
ORDER BY 1, 2
"""

# A schema where some tables have row-level security and others, granted to
# roles other than their owner, do not. A partition is left out: a query
# through its parent is governed by the parent's policies.
RLS_COVERAGE_SQL = """
WITH tables AS (
    SELECT c.oid, c.relname, c.relrowsecurity, c.relowner, c.relacl, n.nspname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p')
      AND NOT c.relispartition
      AND n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%'
),
schemas AS (
    SELECT nspname, count(*) FILTER (WHERE relrowsecurity) AS with_rls, count(*) AS tables
    FROM tables
    GROUP BY nspname
    HAVING count(*) FILTER (WHERE relrowsecurity) > 0
)
SELECT
    t.nspname AS schema,
    t.relname AS table,
    format('%I.%I', t.nspname, t.relname) AS table_ident,
    s.with_rls,
    s.tables,
    pg_get_userbyid(t.relowner) AS owner,
    (
        SELECT array_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END)
        FROM aclexplode(t.relacl) a
        WHERE a.grantee <> t.relowner
    ) AS granted_to
FROM tables t
JOIN schemas s ON s.nspname = t.nspname
WHERE NOT t.relrowsecurity
ORDER BY 1, 2
"""


class SecurityAuditor:
    """Reads the server's authentication, roles, privileges and row-level security for what lets too much in."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver, platform: PlatformDetection | None = None):
        """
        Args:
            sql_driver: The driver to read through. Every statement is a read.
            platform: The managed platform the server runs on, if any, which
                changes what ``ssl = off`` and the minor version mean.
        """
        self.sql_driver = sql_driver
        self.platform = platform

    async def audit(self) -> AuditReport:
        """Audit the server.

        Returns:
            An :class:`~pg_debug.audit.AuditReport`: the findings, most severe
            first, and what each check examined or why it could not tell.
        """
        async with self.sql_driver.session() as driver:
            rows = await driver.execute_query(SERVER_SQL)
            server = dict(rows[0].cells) if rows else {}
            major, minor = split_version(int(server.get("version_num") or 0))
            report = AuditReport(command=SECURITY, server_version=f"{major}.{minor}")

            checks: list[Callable[[], Awaitable[Checked]]] = [
                lambda: self._hba_rules(driver, server),
                lambda: self._ssl(server),
                lambda: self._password_encryption(server),
                lambda: self._public_create(driver),
                lambda: self._privileged_roles(driver),
                lambda: self._rls_without_policy(driver),
                lambda: self._rls_coverage(driver),
                lambda: self._version_currency(server),
            ]
            for run in checks:
                outcome, findings = await run()
                report.checks.append(outcome)
                report.findings.extend(findings)

        report.findings.sort(key=lambda finding: finding.sort_key)
        return report

    # -- Getting in ------------------------------------------------------------

    async def _hba_rules(self, driver: SqlDriver, server: dict[str, Any]) -> Checked:
        check = "hba_rules"
        if not server.get("reads_hba"):
            where = (
                f" On {self.platform.platform.label} the provider manages them and does not expose them."
                if self.platform and self.platform.known
                else ""
            )
            return (
                unknown(
                    check,
                    "nothing",
                    "The authentication rules are read through pg_hba_file_rules, which only a superuser may read by default, "
                    f"and the connecting role cannot.{where} A superuser can run: SELECT * FROM pg_hba_file_rules;",
                ),
                [],
            )

        rows = [dict(row.cells) for row in await driver.execute_query(HBA_RULES_SQL) or []]
        findings = [finding for finding in (_hba_finding(row) for row in rows) if finding is not None]
        outcome = outcome_of(check, f"{plural(len(rows), 'line')} of pg_hba.conf as the file stands on disk", findings)
        return outcome, findings

    async def _ssl(self, server: dict[str, Any]) -> Checked:
        check = "ssl"
        value = str(server.get("ssl") or "")
        examined = f"ssl = {value}"
        if value != "off":
            return outcome_of(check, examined, []), []
        managed = self.platform is not None and self.platform.known
        detail = (
            "The server accepts no encrypted connections, so every query, every result and, with any password method but "
            "SCRAM, every password crosses the network in the clear."
        )
        if managed and self.platform is not None:
            detail += (
                f" On {self.platform.platform.label} clients may reach the server through the provider's proxy, which can "
                "encrypt the connection up to itself; check the provider's setting before acting on this."
            )
        finding = AuditFinding(
            check=check,
            subject="ssl",
            severity=Severity.LOW if managed else Severity.MEDIUM,
            confidence=Confidence.PROVEN,
            summary="ssl is off: the server accepts no encrypted connections.",
            detail=detail,
            remediation=[
                "-- Install a certificate and key (ssl_cert_file, ssl_key_file), then:",
                "ALTER SYSTEM SET ssl = on;",
                "SELECT pg_reload_conf();",
                "-- and require it in pg_hba.conf with hostssl rules.",
            ],
            evidence={"ssl": value},
        )
        return outcome_of(check, examined, [finding]), [finding]

    async def _password_encryption(self, server: dict[str, Any]) -> Checked:
        check = "password_encryption"
        value = str(server.get("password_encryption") or "")
        examined = f"password_encryption = {value}"
        # Before 14, 'on' was a synonym for md5.
        if value not in ("md5", "on"):
            return outcome_of(check, examined, []), []
        finding = AuditFinding(
            check=check,
            subject="password_encryption",
            severity=Severity.MEDIUM,
            confidence=Confidence.PROVEN,
            summary=f"password_encryption is {value}: every password set from now on is stored as an MD5 hash.",
            detail=(
                "An MD5 hash is enough to log in as the role with md5 authentication, so the hash is as good as the "
                "password to whoever reads it, and PostgreSQL 18 deprecates MD5 passwords altogether. scram-sha-256, the "
                "default since 14, stores a salted verifier that cannot be replayed. Existing hashes stay as they are until "
                "each password is set again."
            ),
            remediation=["ALTER SYSTEM SET password_encryption = 'scram-sha-256';", "SELECT pg_reload_conf();"],
            evidence={"password_encryption": value},
        )
        return outcome_of(check, examined, [finding]), [finding]

    # -- Once in -------------------------------------------------------------

    async def _public_create(self, driver: SqlDriver) -> Checked:
        check = "public_create"
        rows = [dict(row.cells) for row in await driver.execute_query(PUBLIC_CREATE_SQL) or []]
        counted = await driver.execute_query(USER_SCHEMAS_SQL)
        schemas = int(counted[0].cells["schemas"]) if counted else 0
        findings = [
            AuditFinding(
                check=check,
                subject=f"schema {row['schema']}",
                severity=Severity.MEDIUM,
                confidence=Confidence.PROVEN,
                summary=f"Every role may create objects in schema {row['schema']}.",
                detail=(
                    f"PUBLIC holds CREATE on {row['schema']} ({row['acl']}). Any role that can connect can put a table, "
                    "a view or a function there, and a function whose name and arguments fit a call better than the one "
                    "intended is the one that runs, with the caller's privileges, for every role whose search_path "
                    "includes the schema (CVE-2018-1058). PostgreSQL 15 stopped granting this on public by default; "
                    "a database created earlier, or restored from one, still has it."
                ),
                remediation=[f"REVOKE CREATE ON SCHEMA {row['schema_ident']} FROM PUBLIC;"],
                evidence={"acl": row["acl"]},
            )
            for row in rows
        ]
        return outcome_of(check, f"the privileges on {plural(schemas, 'schema')}", findings), findings

    async def _privileged_roles(self, driver: SqlDriver) -> Checked:
        check = "privileged_roles"
        rows = [dict(row.cells) for row in await driver.execute_query(PRIVILEGED_ROLES_SQL) or []]
        if rows:
            login_roles = int(rows[0]["login_roles"] or 0)
        else:
            counted = await driver.execute_query(LOGIN_ROLES_SQL)
            login_roles = int(counted[0].cells["login_roles"]) if counted else 0
        findings = [_privileged_role_finding(row) for row in rows]
        return outcome_of(check, f"{plural(login_roles, 'role')} that can log in", findings), findings

    async def _rls_without_policy(self, driver: SqlDriver) -> Checked:
        check = "rls_without_policy"
        rows = [dict(row.cells) for row in await driver.execute_query(RLS_WITHOUT_POLICY_SQL) or []]
        findings = [
            AuditFinding(
                check=check,
                subject=f"{row['schema']}.{row['table']}",
                severity=Severity.LOW,
                confidence=Confidence.PROVEN,
                summary=f"{row['schema']}.{row['table']} has row-level security enabled and no policy.",
                detail=(
                    "With no policy, row-level security denies everything: every role but the owner "
                    f"({row['owner']}), superusers and BYPASSRLS roles sees no rows and may write none"
                    + (", and FORCE ROW LEVEL SECURITY denies the owner too" if row["forced"] else "")
                    + ". That is sometimes the intent, to keep a table to a service role, and sometimes a migration "
                    "that enabled security and never added the policies. Either way it is worth a policy or a comment saying which."
                ),
                remediation=[
                    f"CREATE POLICY <name> ON {row['table_ident']} FOR SELECT TO <role> USING (<condition>);",
                    f"-- or, if it should be readable without restriction: ALTER TABLE {row['table_ident']} DISABLE ROW LEVEL SECURITY;",
                ],
                evidence={"owner": row["owner"], "forced": bool(row["forced"])},
            )
            for row in rows
        ]
        return outcome_of(check, "the tables with row-level security enabled", findings), findings

    async def _rls_coverage(self, driver: SqlDriver) -> Checked:
        check = "rls_coverage"
        rows = [dict(row.cells) for row in await driver.execute_query(RLS_COVERAGE_SQL) or []]
        findings = []
        for row in rows:
            granted_to = sorted(row["granted_to"] or [])
            if not granted_to:
                # Nobody but its owner may touch it: not exposed, whatever its neighbours do.
                continue
            findings.append(
                AuditFinding(
                    check=check,
                    subject=f"{row['schema']}.{row['table']}",
                    severity=Severity.MEDIUM,
                    confidence=Confidence.CANDIDATE,
                    summary=(
                        f"{row['schema']}.{row['table']} has no row-level security, while {row['with_rls']} of the "
                        f"{row['tables']} tables in {row['schema']} do."
                    ),
                    detail=(
                        f"The other tables in the schema restrict rows by policy, and this one is granted to "
                        f"{', '.join(granted_to)} without any, so those roles see and change every row in it. That pattern "
                        "is how a table added after the policies were written gets exposed to the same roles they guard "
                        "against. It is a candidate: the catalog cannot say whether this table is meant to be readable in "
                        "full, as a lookup table may be."
                    ),
                    remediation=[
                        f"ALTER TABLE {row['table_ident']} ENABLE ROW LEVEL SECURITY;",
                        f"CREATE POLICY <name> ON {row['table_ident']} USING (<condition>);",
                    ],
                    evidence={
                        "tables_with_rls": int(row["with_rls"]),
                        "tables_in_schema": int(row["tables"]),
                        "granted_to": granted_to,
                        "owner": row["owner"],
                    },
                    verify=(
                        f"SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                        f"WHERE table_schema = '{_literal(row['schema'])}' AND table_name = '{_literal(row['table'])}';"
                    ),
                )
            )
        return outcome_of(check, f"{plural(len(rows), 'table')} without row-level security in schemas that use it", findings), findings

    # -- Staying patched -----------------------------------------------------

    async def _version_currency(self, server: dict[str, Any]) -> Checked:
        check = "version_currency"
        version_num = int(server.get("version_num") or 0)
        major, minor = split_version(version_num)
        # server_version can carry a packager's suffix; before 10 the number has three parts.
        version = f"{major}.{minor}" if major >= 10 else str(server.get("version") or version_num)
        today = server.get("today")
        if not isinstance(today, date):
            today = date.fromisoformat(str(today)) if today else KNOWN_AS_OF
        knowledge = (
            f"pg-debug's list of releases is from {KNOWN_AS_OF.isoformat()} (newest minors released {LATEST_MINOR_RELEASE_DATE.isoformat()}); "
            "a release after that is not known to it, so read this against postgresql.org/support/versioning."
        )
        examined = f"PostgreSQL {version} against the releases known on {KNOWN_AS_OF.isoformat()}"

        release = RELEASES.get(major)
        if release is None:
            if major > NEWEST_MAJOR:
                return unknown(check, examined, f"PostgreSQL {major} is newer than any release pg-debug knows of. {knowledge}"), []
            release_text = "a version before 10, unsupported since 2021 or earlier"
            finding = AuditFinding(
                check=check,
                subject="major_support",
                severity=Severity.HIGH,
                confidence=Confidence.PROVEN,
                summary=f"PostgreSQL {version} has not received fixes for years.",
                detail=f"This is {release_text}. No security fixes are made for it. {knowledge}",
                remediation=["-- Upgrade to a supported major version."],
                evidence={"server_version_num": version_num},
            )
            return outcome_of(check, examined, [finding]), [finding]

        managed = self.platform is not None and self.platform.known
        provider = (
            f" On {self.platform.platform.label} the provider applies minor releases, usually in a maintenance window."
            if managed and self.platform
            else ""
        )
        findings: list[AuditFinding] = []
        if minor < release.latest_minor:
            findings.append(
                AuditFinding(
                    check=check,
                    subject="minor_release",
                    severity=Severity.MEDIUM,
                    confidence=Confidence.PROVEN,
                    summary=f"PostgreSQL {version} is {release.latest_minor - minor} minor release(s) behind {release.latest}.",
                    detail=(
                        "Minor releases carry only fixes, security fixes among them, and need no dump, restore or pg_upgrade: "
                        f"installing the new binaries and restarting is the whole of it.{provider} {knowledge}"
                    ),
                    remediation=[f"-- Install PostgreSQL {release.latest} (or later) and restart."],
                    evidence={"server_version_num": version_num, "latest_known_minor": release.latest, "known_as_of": KNOWN_AS_OF.isoformat()},
                )
            )

        days_left = (release.final_release - today).days
        if days_left < 0:
            findings.append(
                AuditFinding(
                    check=check,
                    subject="major_support",
                    severity=Severity.HIGH,
                    confidence=Confidence.PROVEN,
                    summary=f"PostgreSQL {major} stopped receiving fixes on {release.final_release.isoformat()}.",
                    detail=f"Its last release was {release.latest}; no security fix made since will reach it. {knowledge}",
                    remediation=["-- Upgrade to a supported major version; pg-debug upgrade-check says what stands in the way."],
                    evidence={"final_release": release.final_release.isoformat(), "server_date": today.isoformat()},
                )
            )
        elif days_left <= SUPPORT_ENDING_DAYS:
            findings.append(
                AuditFinding(
                    check=check,
                    subject="major_support",
                    severity=Severity.MEDIUM,
                    confidence=Confidence.PROVEN,
                    summary=f"PostgreSQL {major} receives its final release on {release.final_release.isoformat()}, {days_left} days from now.",
                    detail=f"After that no fix, security or otherwise, is made for it. {knowledge}",
                    remediation=["-- Plan the major upgrade; pg-debug upgrade-check says what stands in the way."],
                    evidence={"final_release": release.final_release.isoformat(), "server_date": today.isoformat(), "days_left": days_left},
                )
            )
        findings.sort(key=lambda finding: SEVERITY_ORDER[finding.severity])
        return outcome_of(check, examined, findings), findings


def _is_loopback(row: dict[str, Any]) -> bool:
    address = row.get("address")
    return row.get("type") == "local" or address in ("samehost", "localhost") or (address in ("127.0.0.1", "::1") and _full_mask(row.get("netmask")))


def _full_mask(netmask: Any) -> bool:
    return netmask in ("255.255.255.255", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff")


def _open_to_all(row: dict[str, Any]) -> bool:
    address, netmask = row.get("address"), row.get("netmask")
    return address == "all" or (address == "0.0.0.0" and netmask == "0.0.0.0") or (address == "::" and netmask == "::")


def _rule_text(row: dict[str, Any]) -> str:
    """The rule as it reads in pg_hba.conf, without its line number, which moves when lines are added above it."""
    address = row.get("address") or ""
    if address and row.get("netmask"):
        address = f"{address}/{row['netmask']}"
    parts = [
        str(row.get("type") or ""),
        ",".join(row.get("database") or []),
        ",".join(row.get("user_name") or []),
        address,
        str(row.get("auth_method") or ""),
    ]
    return " ".join(part for part in parts if part)


def _hba_finding(row: dict[str, Any]) -> AuditFinding | None:
    line = row.get("line_number")
    if row.get("error"):
        return AuditFinding(
            check="hba_rules",
            subject=f"pg_hba.conf line {line}",
            severity=Severity.MEDIUM,
            confidence=Confidence.PROVEN,
            summary=f"pg_hba.conf line {line} cannot be loaded: {row['error']}",
            detail=(
                "A reload with this file in place is refused and the server keeps the rules it loaded before, so what is on "
                "disk is not what is in force; a restart with it fails. The rules below this one are read from the file "
                "as it stands, not as loaded."
            ),
            remediation=[f"-- Fix line {line} of pg_hba.conf, then: SELECT pg_reload_conf();"],
            evidence={"line_number": line, "error": row["error"]},
        )

    method = row.get("auth_method")
    remote = not _is_loopback(row)
    reasons: list[str] = []
    steps: list[str] = []
    severity: Severity | None = None

    def raise_to(level: Severity) -> None:
        nonlocal severity
        if severity is None or SEVERITY_ORDER[level] < SEVERITY_ORDER[severity]:
            severity = level

    if method == "trust":
        if remote:
            reasons.append("trust lets anyone who reaches the server over the network connect as any role the rule names, with no password")
            raise_to(Severity.HIGH)
        else:
            reasons.append("trust lets any local user or process connect as any role the rule names, with no password")
            raise_to(Severity.MEDIUM)
        steps.append("replace trust with scram-sha-256" + ("" if remote else ", or with peer for local connections"))
    elif method == "password" and row.get("type") != "hostssl":
        reasons.append("password sends the password in clear text, here over a connection that need not be encrypted")
        raise_to(Severity.HIGH if remote else Severity.MEDIUM)
        steps.append("replace password with scram-sha-256")
    elif method == "md5":
        reasons.append(
            "md5 accepts a role's MD5 hash where it has one, and PostgreSQL 18 deprecates MD5 passwords; scram-sha-256 "
            "refuses them (a role with a SCRAM password is unaffected either way)"
        )
        raise_to(Severity.LOW)
        steps.append("replace md5 with scram-sha-256 once no role that uses this rule has an MD5 password")

    if _open_to_all(row) and method not in ("reject", None):
        reasons.append("it accepts connections from every address")
        raise_to(Severity.MEDIUM)
        steps.append("narrow the address to the networks that need it")

    if severity is None:
        return None
    rule = _rule_text(row)
    return AuditFinding(
        check="hba_rules",
        subject=rule,
        severity=severity,
        confidence=Confidence.PROVEN,
        summary=f"pg_hba.conf line {line}: {rule}",
        detail=(
            "; ".join(reasons).capitalize() + ". pg_hba_file_rules reads the file as it is on disk now, which is what the "
            "server will use after its next reload, not necessarily what it uses this moment."
        ),
        remediation=[
            f"-- Line {line} of pg_hba.conf: {'; '.join(steps)}. Then:",
            "SELECT pg_reload_conf();",
        ],
        evidence={k: row.get(k) for k in ("line_number", "type", "database", "user_name", "address", "netmask", "auth_method")},
    )


def _privileged_role_finding(row: dict[str, Any]) -> AuditFinding:
    connected = " pg-debug is connected as this role now." if row["connected_as"] else ""
    if row["superuser"]:
        if row["bootstrap"]:
            severity = Severity.LOW
            detail = (
                "This is the bootstrap superuser initdb created, which every cluster has. It is a finding only if "
                "applications or people connect as it day to day: a superuser bypasses every privilege check and "
                f"row-level security, and can read and write files on the server.{connected}"
            )
        else:
            severity = Severity.MEDIUM
            detail = (
                "A superuser bypasses every privilege check and row-level security, and can read and write files on the "
                "server and run programs as its operating system user. A role that can log in with that is one leaked "
                f"password from all of it.{connected}"
            )
        summary = f"Role {row['role']} can log in and is a superuser."
        if row["bootstrap"]:
            # The bootstrap superuser cannot lose the attribute.
            remediation = [
                "-- Connect applications as roles created for them, with only the privileges they need, and keep this one for administration."
            ]
        else:
            remediation = [f"ALTER ROLE {row['role_ident']} NOSUPERUSER;  -- after granting it what it actually needs"]
    else:
        severity = Severity.MEDIUM if row["rls_in_use"] else Severity.LOW
        detail = (
            "BYPASSRLS ignores every row-level security policy, so this role reads and writes rows the policies keep "
            "from everyone else"
            + (", and this database has tables that rely on them." if row["rls_in_use"] else "; no table here uses row-level security yet.")
            + connected
        )
        summary = f"Role {row['role']} can log in and bypasses row-level security."
        remediation = [f"ALTER ROLE {row['role_ident']} NOBYPASSRLS;"]
    return AuditFinding(
        check="privileged_roles",
        subject=f"role {row['role']}",
        severity=severity,
        confidence=Confidence.PROVEN,
        summary=summary,
        detail=detail,
        remediation=remediation,
        evidence={
            "superuser": bool(row["superuser"]),
            "bypassrls": bool(row["bypassrls"]),
            "bootstrap": bool(row["bootstrap"]),
            "connected_as": bool(row["connected_as"]),
        },
    )


def _literal(value: Any) -> str:
    return str(value).replace("'", "''")


__all__ = ["SECURITY", "SUPPORT_ENDING_DAYS", "SecurityAuditor"]

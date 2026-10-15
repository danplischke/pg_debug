"""The security audit: what each rule, role and table is argued from, and what it cannot see."""

from datetime import date
from unittest.mock import MagicMock

import pytest

from pg_debug.audit import CheckStatus
from pg_debug.audit import SecurityAuditor
from pg_debug.audit import security
from pg_debug.audit.versions import KNOWN_AS_OF
from pg_debug.audit.versions import RELEASES
from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.platform import Basis
from pg_debug.platform import Platform
from pg_debug.platform import PlatformDetection
from pg_debug.severity import Confidence
from pg_debug.severity import Severity


def rows(*cells):
    return [SqlDriver.RowResult(cells=dict(c)) for c in cells]


def server(version_num=160015, **overrides):
    return {
        "version_num": version_num,
        "version": f"{version_num // 10000}.{version_num % 100} (Debian)",
        "ssl": "on",
        "password_encryption": "scram-sha-256",
        "today": date(2026, 10, 14),
        "current_role": "postgres",
        "reads_hba": True,
        **overrides,
    }


class FakeDriver:
    """Answers each statement by a marker a test names; anything else returns no rows."""

    def __init__(self, answers):
        self.answers = answers
        self.statements = []

    def session(self):
        driver = self

        class Pinned:
            async def __aenter__(self):
                return driver

            async def __aexit__(self, *exc):
                return None

        return Pinned()

    async def execute_query(self, query, *args, **kwargs):
        self.statements.append(str(query))
        if "current_setting('server_version_num')" in str(query):
            return rows(self.answers["server"])
        for marker, answer in self.answers.items():
            if marker != "server" and marker in str(query):
                return rows(*answer) if isinstance(answer, list) else rows(answer)
        return []


async def audit(answers, platform=None):
    driver = FakeDriver(answers)
    return await SecurityAuditor(driver, platform=platform).audit(), driver  # type: ignore[arg-type]


def by_check(report, name):
    return [finding for finding in report.findings if finding.check == name]


def outcome(report, name):
    return next(o for o in report.checks if o.check == name)


def hba(**overrides):
    return {
        "line_number": 90,
        "type": "host",
        "database": ["all"],
        "user_name": ["all"],
        "address": "10.0.0.0",
        "netmask": "255.0.0.0",
        "auth_method": "scram-sha-256",
        "error": None,
        **overrides,
    }


# -- pg_hba.conf ---------------------------------------------------------------


def test_scram_from_a_private_network_is_not_a_finding():
    assert security._hba_finding(hba()) is None


def test_trust_over_the_network_is_high():
    finding = security._hba_finding(hba(auth_method="trust"))

    assert finding is not None
    assert finding.severity is Severity.HIGH
    assert finding.subject == "host all all 10.0.0.0/255.0.0.0 trust"


@pytest.mark.parametrize(
    "rule",
    [
        hba(type="local", address=None, netmask=None, auth_method="trust"),
        hba(address="127.0.0.1", netmask="255.255.255.255", auth_method="trust"),
        hba(address="::1", netmask="ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", auth_method="trust"),
    ],
)
def test_trust_from_this_machine_is_medium_and_suggests_peer_for_a_socket(rule):
    finding = security._hba_finding(rule)

    assert finding is not None
    assert finding.severity is Severity.MEDIUM


def test_a_rule_open_to_every_address_is_reported_even_with_scram():
    finding = security._hba_finding(hba(address="0.0.0.0", netmask="0.0.0.0"))

    assert finding is not None
    assert finding.severity is Severity.MEDIUM
    assert "every address" in finding.detail
    assert "narrow the address" in finding.remediation[0]


def test_a_reject_rule_open_to_every_address_is_what_it_should_be():
    assert security._hba_finding(hba(address="all", netmask=None, auth_method="reject")) is None


def test_cleartext_password_without_ssl_is_high_and_with_ssl_is_not_reported():
    assert security._hba_finding(hba(auth_method="password")).severity is Severity.HIGH  # type: ignore[union-attr]
    assert security._hba_finding(hba(type="hostssl", auth_method="password")) is None


def test_md5_is_low_since_scram_passwords_still_use_scram():
    finding = security._hba_finding(hba(auth_method="md5"))

    assert finding is not None
    assert finding.severity is Severity.LOW


def test_a_line_that_cannot_be_loaded_is_reported_with_its_error():
    finding = security._hba_finding(hba(error='invalid authentication method "trusty"'))

    assert finding is not None
    assert finding.subject == "pg_hba.conf line 90"
    assert "trusty" in finding.summary


def test_the_subject_does_not_move_when_a_line_is_added_above():
    """A baseline that accepted a rule would otherwise call it new after any edit to the file."""
    first = security._hba_finding(hba(auth_method="trust", line_number=1))
    moved = security._hba_finding(hba(auth_method="trust", line_number=40))
    assert first is not None and moved is not None
    assert first.subject == moved.subject


@pytest.mark.asyncio
async def test_rules_a_role_cannot_read_are_unknown_not_clear():
    """Only a superuser reads pg_hba_file_rules. An empty answer would say
    the rules are fine to a role that never saw them."""
    report, driver = await audit({"server": server(reads_hba=False)})

    hba_outcome = outcome(report, "hba_rules")
    assert hba_outcome.status is CheckStatus.UNKNOWN
    assert "superuser" in (hba_outcome.reason or "")
    assert not any("FROM pg_hba_file_rules" in s for s in driver.statements)


# -- Settings ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ssl_off_is_medium_on_a_server_and_low_behind_a_providers_proxy():
    report, _ = await audit({"server": server(ssl="off")})
    assert by_check(report, "ssl")[0].severity is Severity.MEDIUM

    neon = PlatformDetection(platform=Platform.NEON, signal="neon host", basis=Basis.URL)
    report, _ = await audit({"server": server(ssl="off")}, platform=neon)
    assert by_check(report, "ssl")[0].severity is Severity.LOW


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["md5", "on"])
async def test_password_encryption_md5_is_reported(value):
    report, _ = await audit({"server": server(password_encryption=value)})

    [finding] = by_check(report, "password_encryption")
    assert "scram-sha-256" in finding.remediation[0]


# -- Roles and schemas ---------------------------------------------------------


def role(**overrides):
    return {
        "role": "app",
        "role_ident": "app",
        "superuser": True,
        "bypassrls": False,
        "bootstrap": False,
        "connected_as": False,
        "login_roles": 5,
        "rls_in_use": False,
        **overrides,
    }


def test_a_login_superuser_is_medium_and_can_be_demoted():
    finding = security._privileged_role_finding(role())

    assert finding.severity is Severity.MEDIUM
    assert finding.remediation == ["ALTER ROLE app NOSUPERUSER;  -- after granting it what it actually needs"]


def test_the_bootstrap_superuser_is_low_and_is_not_told_to_lose_the_attribute():
    """PostgreSQL refuses NOSUPERUSER on the bootstrap superuser."""
    finding = security._privileged_role_finding(role(role="postgres", role_ident="postgres", bootstrap=True, connected_as=True))

    assert finding.severity is Severity.LOW
    assert "NOSUPERUSER" not in " ".join(finding.remediation)
    assert "connected as this role" in finding.detail


def test_bypassrls_matters_more_where_tables_use_row_level_security():
    assert security._privileged_role_finding(role(superuser=False, bypassrls=True)).severity is Severity.LOW
    assert security._privileged_role_finding(role(superuser=False, bypassrls=True, rls_in_use=True)).severity is Severity.MEDIUM


@pytest.mark.asyncio
async def test_public_create_on_a_schema_is_reported_with_the_revoke():
    answers = {
        "server": server(),
        "a.privilege_type = 'CREATE'": [{"schema": "public", "schema_ident": "public", "acl": "{postgres=UC/postgres,=UC/postgres}"}],
        "count(*) AS schemas": {"schemas": 3},
    }
    report, _ = await audit(answers)

    [finding] = by_check(report, "public_create")
    assert finding.remediation == ["REVOKE CREATE ON SCHEMA public FROM PUBLIC;"]
    assert outcome(report, "public_create").examined == "the privileges on 3 schemas"


# -- Row-level security --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_table_beside_protected_ones_and_granted_to_others_is_a_candidate():
    answers = {
        "server": server(),
        "WITH tables AS": [
            {
                "schema": "api",
                "table": "invoices",
                "table_ident": "api.invoices",
                "with_rls": 4,
                "tables": 5,
                "owner": "owner",
                "granted_to": ["web_anon"],
            },
            {"schema": "api", "table": "internal", "table_ident": "api.internal", "with_rls": 4, "tables": 5, "owner": "owner", "granted_to": None},
        ],
    }
    report, _ = await audit(answers)

    [finding] = by_check(report, "rls_coverage")
    assert finding.subject == "api.invoices"
    assert finding.confidence is Confidence.CANDIDATE
    assert finding.verify is not None and "role_table_grants" in finding.verify


@pytest.mark.asyncio
async def test_row_level_security_without_a_policy_is_reported():
    answers = {
        "server": server(),
        "NOT EXISTS (SELECT 1 FROM pg_policy": [
            {"schema": "app", "table": "secrets", "table_ident": "app.secrets", "forced": True, "owner": "owner"}
        ],
    }
    report, _ = await audit(answers)

    [finding] = by_check(report, "rls_without_policy")
    assert finding.confidence is Confidence.PROVEN
    assert "FORCE ROW LEVEL SECURITY denies the owner too" in finding.detail


# -- Version currency ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_latest_known_minor_is_clear():
    report, _ = await audit({"server": server(160000 + RELEASES[16].latest_minor)})

    assert outcome(report, "version_currency").status is CheckStatus.CLEAR


@pytest.mark.asyncio
async def test_a_minor_behind_says_how_far_and_when_that_was_known():
    report, _ = await audit({"server": server(160004)})

    [finding] = by_check(report, "version_currency")
    assert finding.subject == "minor_release"
    assert f"{RELEASES[16].latest_minor - 4} minor release(s) behind" in finding.summary
    assert KNOWN_AS_OF.isoformat() in finding.detail
    assert finding.evidence["known_as_of"] == KNOWN_AS_OF.isoformat()


@pytest.mark.asyncio
async def test_a_major_past_its_final_release_is_high_by_the_servers_date():
    report, _ = await audit({"server": server(130023, today=date(2026, 10, 14))})

    [finding] = by_check(report, "version_currency")
    assert finding.subject == "major_support"
    assert finding.severity is Severity.HIGH


@pytest.mark.asyncio
async def test_a_major_close_to_its_final_release_is_said_in_advance():
    report, _ = await audit({"server": server(140000 + RELEASES[14].latest_minor, today=date(2026, 10, 14))})

    [finding] = by_check(report, "version_currency")
    assert finding.severity is Severity.MEDIUM
    assert finding.evidence["days_left"] == (RELEASES[14].final_release - date(2026, 10, 14)).days


@pytest.mark.asyncio
async def test_a_major_newer_than_pg_debug_knows_is_unknown():
    report, _ = await audit({"server": server(190000)})

    currency = outcome(report, "version_currency")
    assert currency.status is CheckStatus.UNKNOWN
    assert KNOWN_AS_OF.isoformat() in (currency.reason or "")


# -- Restricted mode -----------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        security.SERVER_SQL,
        security.HBA_RULES_SQL,
        security.PUBLIC_CREATE_SQL,
        security.USER_SCHEMAS_SQL,
        security.PRIVILEGED_ROLES_SQL,
        security.LOGIN_ROLES_SQL,
        security.RLS_WITHOUT_POLICY_SQL,
        security.RLS_COVERAGE_SQL,
    ],
)
def test_every_statement_passes_restricted_mode(statement):
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(statement)


@pytest.mark.asyncio
async def test_the_audit_runs_through_a_restricted_driver():
    inner = FakeDriver({"server": server()})

    report = await SecurityAuditor(SafeSqlDriver(inner)).audit()  # type: ignore[arg-type]

    assert len(report.checks) == 8
    assert report.server_version == "16.15"

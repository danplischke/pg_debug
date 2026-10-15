"""upgrade-check and security against a real server: each lint fires on a fixture built to trip it."""

import uuid
from typing import LiteralString
from typing import cast
from urllib.parse import urlsplit

import psycopg
import pytest

from pg_debug import PgDebug
from pg_debug.audit import CheckStatus


def q(statement: str) -> LiteralString:
    """A statement built from this module's own fixtures, as psycopg's types want it."""
    return cast(LiteralString, statement)


@pytest.fixture(scope="class")
def url(test_postgres_connection_string) -> str:
    return test_postgres_connection_string[0]


def _execute(url, *statements):
    with psycopg.connect(url, autocommit=True) as conn:
        for statement in statements:
            conn.execute(q(statement))


def _value(url, statement):
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(q(statement)).fetchone()
        return row[0] if row else None


PLAIN_PASSWORD = "pgd-plain-password"


def as_role(url, role):
    """The same server and database, connecting as another role."""
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[1]
    return f"{parts.scheme}://{role}:{PLAIN_PASSWORD}@{host}{parts.path}"


def by_check(report, check):
    return {finding.subject: finding for finding in report.findings if finding.check == check}


def outcome(report, check):
    return next(o for o in report.checks if o.check == check)


@pytest.fixture(scope="class")
def roles(url):
    """Roles are cluster-wide, so each run's names are its own and are dropped after."""
    suffix = uuid.uuid4().hex[:8]
    names = {
        "md5": f"pgd_md5_{suffix}",
        "admin": f"pgd_admin_{suffix}",
        "bypass": f"pgd_bypass_{suffix}",
        "web": f"pgd_web_{suffix}",
        "plain": f"pgd_plain_{suffix}",
    }
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SET password_encryption = 'md5'")
        conn.execute(q(f"CREATE ROLE {names['md5']} LOGIN PASSWORD 'not-a-real-password'"))
        conn.execute(q(f"CREATE ROLE {names['admin']} LOGIN SUPERUSER"))
        conn.execute(q(f"CREATE ROLE {names['bypass']} LOGIN BYPASSRLS"))
        conn.execute(q(f"CREATE ROLE {names['web']} NOLOGIN"))
        conn.execute("SET password_encryption = 'scram-sha-256'")
        conn.execute(q(f"CREATE ROLE {names['plain']} LOGIN PASSWORD '{PLAIN_PASSWORD}'"))
    try:
        yield names
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            for name in names.values():
                conn.execute(q(f"DROP OWNED BY {name}"))
                conn.execute(q(f"DROP ROLE IF EXISTS {name}"))


@pytest.fixture(scope="class")
def upgrade_fixtures(url):
    _execute(
        url,
        "CREATE TABLE pgd_jobs (id bigint PRIMARY KEY, handler regproc, kept regclass)",
        "CREATE DOMAIN pgd_proc AS regprocedure",
        "CREATE TABLE pgd_hooks (id bigint PRIMARY KEY, hook pgd_proc)",
        "CREATE TABLE pgd_orders (a int, b int)",
        "CREATE STATISTICS pgd_orders_dep (dependencies) ON a, b FROM pgd_orders",
    )


@pytest.fixture(scope="class")
def security_fixtures(url, roles):
    database = url.rsplit("/", 1)[1]
    _execute(
        url,
        "GRANT CREATE ON SCHEMA public TO PUBLIC",
        "CREATE SCHEMA pgd_api",
        "CREATE TABLE pgd_api.accounts (id int)",
        "CREATE TABLE pgd_api.invoices (id int)",
        "CREATE TABLE pgd_api.owner_only (id int)",
        "ALTER TABLE pgd_api.accounts ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY own ON pgd_api.accounts TO {roles['web']} USING (true)",
        f"GRANT SELECT ON pgd_api.accounts, pgd_api.invoices TO {roles['web']}",
        "CREATE TABLE pgd_api.locked (id int)",
        "ALTER TABLE pgd_api.locked ENABLE ROW LEVEL SECURITY",
        f"ALTER DATABASE \"{database}\" SET password_encryption = 'md5'",
    )


@pytest.mark.usefixtures("roles", "upgrade_fixtures")
class TestUpgradeCheck:
    @pytest.mark.asyncio
    async def test_reg_types_pg_upgrade_refuses_are_found_and_the_ones_it_keeps_are_not(self, url):
        async with PgDebug(url, access_mode="restricted") as pg:
            report = await pg.check_upgrade(to=18)

        found = by_check(report, "reg_type_columns")
        assert "public.pgd_jobs.handler" in found
        assert "public.pgd_hooks.hook" in found, "a domain over a refused type is refused too"
        assert "public.pgd_jobs.kept" not in found

    @pytest.mark.asyncio
    async def test_extended_statistics_are_named_for_an_upgrade_to_18(self, url):
        async with PgDebug(url) as pg:
            report = await pg.check_upgrade(to=18)

        assert "public.pgd_orders_dep" in by_check(report, "statistics_after_upgrade")

    @pytest.mark.asyncio
    async def test_checksums_are_compared_with_the_targets_initdb_default(self, url):
        enabled = _value(url, "SHOW data_checksums") == "on"
        async with PgDebug(url) as pg:
            report = await pg.check_upgrade(to=18)

        assert bool(by_check(report, "data_checksums")) is (not enabled)

    @pytest.mark.asyncio
    async def test_an_extension_behind_its_default_version_is_found(self, url):
        versions = _value(
            url,
            "SELECT array_agg(v.version ORDER BY v.version) FROM pg_available_extension_versions v "
            "JOIN pg_available_extensions a ON a.name = v.name WHERE v.name = 'pgstattuple' AND v.version <> a.default_version",
        )
        if not versions:
            pytest.skip("no older pgstattuple version to install")
        _execute(url, "DROP EXTENSION IF EXISTS pgstattuple", f"CREATE EXTENSION pgstattuple VERSION '{versions[0]}'")
        try:
            async with PgDebug(url) as pg:
                report = await pg.check_upgrade(to=18)
        finally:
            _execute(url, "DROP EXTENSION pgstattuple")

        assert by_check(report, "extension_versions")["extension pgstattuple"].remediation == ["ALTER EXTENSION pgstattuple UPDATE;"]

    @pytest.mark.asyncio
    async def test_an_md5_password_is_found_by_a_superuser(self, url, roles):
        async with PgDebug(url) as pg:
            report = await pg.check_upgrade(to=18)

        assert f"role {roles['md5']}" in by_check(report, "md5_passwords")

    @pytest.mark.asyncio
    async def test_md5_passwords_are_unknown_to_a_role_that_cannot_read_them(self, url, roles):
        as_plain = as_role(url, roles["plain"])
        async with PgDebug(as_plain, access_mode="restricted") as pg:
            report = await pg.check_upgrade(to=18)

        assert outcome(report, "md5_passwords").status is CheckStatus.UNKNOWN

    @pytest.mark.asyncio
    async def test_a_prepared_transaction_is_found(self, url):
        if int(str(_value(url, "SHOW max_prepared_transactions"))) == 0:
            pytest.skip("max_prepared_transactions is 0 on this server")
        gid = f"pgd_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("BEGIN")
            conn.execute("CREATE TABLE pgd_prepared (id int)")
            conn.execute(q(f"PREPARE TRANSACTION '{gid}'"))
        try:
            async with PgDebug(url) as pg:
                report = await pg.check_upgrade(to=18)
        finally:
            _execute(url, f"ROLLBACK PREPARED '{gid}'")

        assert f"transaction {gid}" in by_check(report, "prepared_transactions")

    @pytest.mark.asyncio
    async def test_a_logical_slot_before_17_is_found(self, url):
        if _value(url, "SHOW wal_level") != "logical":
            pytest.skip("wal_level is not logical on this server")
        if int(str(_value(url, "SHOW server_version_num"))) >= 170000:
            pytest.skip("from 17 pg_upgrade carries logical slots")
        slot = f"pgd_{uuid.uuid4().hex[:8]}"
        _execute(url, f"SELECT pg_create_logical_replication_slot('{slot}', 'pgoutput')")
        try:
            async with PgDebug(url) as pg:
                report = await pg.check_upgrade(to=18)
        finally:
            _execute(url, f"SELECT pg_drop_replication_slot('{slot}')")

        assert f"slot {slot}" in by_check(report, "logical_replication")


@pytest.mark.usefixtures("roles", "security_fixtures")
class TestSecurityAudit:
    @pytest.mark.asyncio
    async def test_each_lint_fires_on_its_fixture(self, url, roles):
        async with PgDebug(url, access_mode="restricted") as pg:
            report = await pg.audit_security()

        assert "schema public" in by_check(report, "public_create")
        privileged = by_check(report, "privileged_roles")
        assert f"role {roles['admin']}" in privileged
        assert f"role {roles['bypass']}" in privileged
        assert f"role {roles['md5']}" not in privileged
        assert "pgd_api.locked" in by_check(report, "rls_without_policy")
        gaps = by_check(report, "rls_coverage")
        assert "pgd_api.invoices" in gaps
        assert "pgd_api.owner_only" not in gaps, "granted to nobody but its owner, it is not exposed"
        assert "password_encryption" in by_check(report, "password_encryption")
        assert outcome(report, "version_currency").status in (CheckStatus.CLEAR, CheckStatus.FOUND)

    @pytest.mark.asyncio
    async def test_settings_and_rules_are_read_as_the_server_has_them(self, url):
        ssl = _value(url, "SHOW ssl")
        trust_rules = _value(url, "SELECT count(*) FROM pg_hba_file_rules WHERE auth_method = 'trust'")
        async with PgDebug(url) as pg:
            report = await pg.audit_security()

        assert bool(by_check(report, "ssl")) is (ssl == "off")
        hba = by_check(report, "hba_rules")
        assert sum(1 for f in hba.values() if f.evidence.get("auth_method") == "trust") == trust_rules

    @pytest.mark.asyncio
    async def test_a_role_that_cannot_read_pg_hba_is_told_so(self, url, roles):
        as_plain = as_role(url, roles["plain"])
        async with PgDebug(as_plain, access_mode="restricted") as pg:
            report = await pg.audit_security()

        assert outcome(report, "hba_rules").status is CheckStatus.UNKNOWN
        assert report.unknown

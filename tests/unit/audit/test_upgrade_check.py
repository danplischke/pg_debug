"""The upgrade check: what pg_upgrade refuses, what it drops, and what it cannot be asked."""

from unittest.mock import MagicMock

import pytest

from pg_debug.audit import CheckStatus
from pg_debug.audit import UpgradeChecker
from pg_debug.audit import upgrade
from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.errors import InvalidArgumentError
from pg_debug.platform import Basis
from pg_debug.platform import Platform
from pg_debug.platform import PlatformDetection
from pg_debug.severity import Severity


def rows(*cells):
    return [SqlDriver.RowResult(cells=dict(c)) for c in cells]


def server(version_num=160004, **overrides):
    return {
        "version_num": version_num,
        "version": f"{version_num // 10000}.{version_num % 100}",
        "data_checksums": "off",
        "database": "app",
        "reads_authid": True,
        "other_databases": None,
        **overrides,
    }


class FakeDriver:
    """Answers each statement by the first of its lines that a test named.

    Pinning a session is a no-op, and a statement nobody named returns no
    rows, which is how a clear check reads.
    """

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


@pytest.fixture(autouse=True)
def version_from_the_fake(monkeypatch):
    async def version(driver):
        fake = getattr(driver, "sql_driver", driver)
        return fake.answers["server"]["version_num"] // 10000

    monkeypatch.setattr(upgrade, "get_postgres_version", version)


async def check(answers, to=None, platform=None):
    driver = FakeDriver(answers)
    report = await UpgradeChecker(driver, platform=platform).check(to=to)  # type: ignore[arg-type]
    return report, driver


def by_check(report, name):
    return [finding for finding in report.findings if finding.check == name]


def outcome(report, name):
    return next(o for o in report.checks if o.check == name)


@pytest.mark.asyncio
async def test_the_default_target_is_the_newest_known_major():
    report, _ = await check({"server": server()})

    assert report.target_version == 18
    assert report.server_version == "16.4"


@pytest.mark.asyncio
async def test_a_target_that_is_not_newer_is_refused():
    with pytest.raises(InvalidArgumentError, match="not an upgrade"):
        await check({"server": server()}, to=16)


@pytest.mark.asyncio
async def test_a_server_already_on_the_newest_major_is_checked_for_the_next_and_told_what_is_unknown():
    report, _ = await check({"server": server(180001, data_checksums="on")})

    assert report.target_version == 19
    assert any("knows pg_upgrade's rules up to PostgreSQL 18" in note for note in report.notes)


@pytest.mark.asyncio
async def test_a_prepared_transaction_blocks_the_upgrade():
    answers = {
        "server": server(),
        "pg_prepared_xacts": [{"gid": "tx'1", "prepared": "2026-10-01", "owner": "app", "database": "app", "held_for": "13 days"}],
    }
    report, _ = await check(answers)

    [finding] = by_check(report, "prepared_transactions")
    assert finding.severity is Severity.HIGH
    assert "COMMIT PREPARED 'tx''1'" in finding.remediation[1]


@pytest.mark.asyncio
async def test_reg_type_columns_are_looked_for_by_the_types_pg_upgrade_refuses():
    """regclass, regrole and regtype survive pg_upgrade; flagging them would
    send someone to rewrite columns that upgrade fine."""
    report, driver = await check({"server": server()})

    statement = next(s for s in driver.statements if "WITH RECURSIVE oids" in s)
    looked_for = set(statement.split("ANY('{", 1)[1].split("}'", 1)[0].split(","))
    assert looked_for == {
        "regcollation",
        "regconfig",
        "regdictionary",
        "regnamespace",
        "regoper",
        "regoperator",
        "regproc",
        "regprocedure",
    }
    assert outcome(report, "reg_type_columns").status is CheckStatus.CLEAR


@pytest.mark.asyncio
async def test_a_reg_type_column_is_a_high_finding_with_a_fix_for_a_plain_column():
    answers = {
        "server": server(),
        "WITH RECURSIVE oids": [
            {
                "schema": "public",
                "relation": "jobs",
                "relkind": "r",
                "column": "handler",
                "column_type": "regproc",
                "relation_ident": "public.jobs",
                "column_ident": "handler",
            }
        ],
    }
    report, _ = await check(answers)

    [finding] = by_check(report, "reg_type_columns")
    assert finding.subject == "public.jobs.handler"
    assert finding.severity is Severity.HIGH
    assert finding.remediation == ["ALTER TABLE public.jobs ALTER COLUMN handler TYPE text;"]


@pytest.mark.asyncio
async def test_logical_slots_before_17_are_reported_as_dropped():
    answers = {
        "server": server(160004),
        "slot_type = 'logical'": [{"slot_name": "cdc", "plugin": "pgoutput", "database": "app", "active": True, "wal_status": "reserved"}],
        "pg_subscription": [{"subname": "from_primary", "database": "app"}],
    }
    report, driver = await check(answers)

    subjects = {f.subject for f in by_check(report, "logical_replication")}
    assert subjects == {"slot cdc", "subscription from_primary"}
    assert not any("invalidation_reason" in s for s in driver.statements)


@pytest.mark.asyncio
async def test_from_17_only_an_invalidated_slot_is_reported():
    answers = {
        "server": server(170002, data_checksums="on"),
        "slot_type = 'logical'": [
            {"slot_name": "ok", "plugin": "pgoutput", "database": "app", "active": True, "wal_status": "reserved", "invalidation_reason": None},
            {
                "slot_name": "gone",
                "plugin": "pgoutput",
                "database": "app",
                "active": False,
                "wal_status": "lost",
                "invalidation_reason": "wal_removed",
            },
        ],
    }
    report, driver = await check(answers)

    assert [f.subject for f in by_check(report, "logical_replication")] == ["slot gone"]
    assert any("invalidation_reason" in s for s in driver.statements)


@pytest.mark.asyncio
async def test_checksums_off_meet_18s_default_of_on():
    report, _ = await check({"server": server(data_checksums="off")}, to=18)

    [finding] = by_check(report, "data_checksums")
    assert "initdb --no-data-checksums ..." in finding.remediation


@pytest.mark.asyncio
async def test_checksums_on_meet_17s_default_of_off():
    report, _ = await check({"server": server(150004, data_checksums="on")}, to=17)

    [finding] = by_check(report, "data_checksums")
    assert "--data-checksums" in finding.remediation[0]


@pytest.mark.asyncio
async def test_checksums_that_match_the_new_default_are_clear():
    report, _ = await check({"server": server(data_checksums="on")}, to=18)

    assert outcome(report, "data_checksums").status is CheckStatus.CLEAR


@pytest.mark.asyncio
async def test_checksums_are_not_the_readers_to_choose_on_a_managed_platform():
    rds = PlatformDetection(platform=Platform.RDS, signal="rds_tools", basis=Basis.SERVER)
    report, _ = await check({"server": server()}, platform=rds)

    assert outcome(report, "data_checksums").status is CheckStatus.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_extended_statistics_need_analyze_after_an_upgrade_to_18():
    answers = {
        "server": server(data_checksums="on"),
        "pg_statistic_ext": [
            {"schema": "public", "statistics": "orders_dep", "table_ident": "public.orders", "table_name": "public.orders", "kinds": ["d"]}
        ],
    }
    report, _ = await check(answers, to=18)

    [finding] = by_check(report, "statistics_after_upgrade")
    assert finding.subject == "public.orders_dep"
    assert "ANALYZE public.orders;" in finding.remediation


@pytest.mark.asyncio
async def test_before_18_no_statistics_cross_at_all():
    answers = {"server": server(140010), "count(*) AS tables": {"tables": 12}}
    report, _ = await check(answers, to=17)

    [finding] = by_check(report, "statistics_after_upgrade")
    assert finding.subject == "optimizer statistics"
    assert finding.evidence == {"tables": 12}


@pytest.mark.asyncio
async def test_an_extension_behind_its_default_version_is_reported():
    answers = {
        "server": server(data_checksums="on"),
        "pg_available_extensions": [
            {"extension": "plpgsql", "installed_version": "1.0", "default_version": "1.0", "extension_ident": "plpgsql", "examined": 3},
            {
                "extension": "pg_stat_statements",
                "installed_version": "1.9",
                "default_version": "1.10",
                "extension_ident": "pg_stat_statements",
                "examined": 3,
            },
            {"extension": "gone", "installed_version": "1.0", "default_version": None, "extension_ident": "gone", "examined": 3},
        ],
    }
    report, _ = await check(answers)

    [finding] = by_check(report, "extension_versions")
    assert finding.remediation == ["ALTER EXTENSION pg_stat_statements UPDATE;"]
    assert outcome(report, "extension_versions").examined == "3 extensions installed in this database"


@pytest.mark.asyncio
async def test_md5_passwords_are_unknown_to_a_role_that_cannot_read_pg_authid():
    """The hashes are a superuser's to read. Reporting none would tell a
    role that cannot see them that there are none."""
    report, driver = await check({"server": server(reads_authid=False, data_checksums="on")})

    md5 = outcome(report, "md5_passwords")
    assert md5.status is CheckStatus.UNKNOWN
    assert "superuser" in (md5.reason or "")
    assert not any("pg_authid\nWHERE rolpassword" in s for s in driver.statements)
    assert report.unknown == [md5]


@pytest.mark.asyncio
async def test_an_md5_password_is_reported_by_role():
    answers = {
        "server": server(data_checksums="on"),
        "rolpassword LIKE 'md5%'": [{"role": "legacy", "can_login": True, "examined": 1}],
        "rolpassword IS NOT NULL": {"roles": 4},
    }
    report, _ = await check(answers)

    [finding] = by_check(report, "md5_passwords")
    assert finding.subject == "role legacy"
    assert finding.severity is Severity.MEDIUM
    assert outcome(report, "md5_passwords").examined == "4 roles with a password"


@pytest.mark.asyncio
async def test_other_databases_are_named_since_only_this_one_was_read():
    report, _ = await check({"server": server(other_databases=["billing", "reports"], data_checksums="on")})

    assert any("billing, reports" in note for note in report.notes)


# -- Restricted mode -----------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        upgrade.SERVER_SQL,
        upgrade.PREPARED_TRANSACTIONS_SQL,
        SafeSqlDriver.param_sql_to_query(upgrade.REG_TYPE_COLUMNS_SQL, [list(upgrade.REFUSED_REG_TYPES)]),
        upgrade.LOGICAL_SLOTS_SQL,
        upgrade.LOGICAL_SLOTS_17_SQL,
        upgrade.SUBSCRIPTIONS_SQL,
        upgrade.EXTENDED_STATISTICS_SQL,
        upgrade.USER_TABLES_SQL,
        upgrade.EXTENSION_VERSIONS_SQL,
        upgrade.MD5_PASSWORDS_SQL,
        upgrade.ROLES_WITH_PASSWORDS_SQL,
    ],
)
def test_every_statement_passes_restricted_mode(statement):
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(statement)


@pytest.mark.asyncio
async def test_the_check_runs_through_a_restricted_driver():
    """End to end through SafeSqlDriver, so a statement it would refuse fails here."""
    inner = FakeDriver({"server": server(data_checksums="on")})
    safe = SafeSqlDriver(inner)  # type: ignore[arg-type]

    report = await UpgradeChecker(safe).check()

    assert len(report.checks) == 7

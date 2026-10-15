"""Multixact wraparound: the ages against their limits, and what is said of member space."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.health.checks.multixact import DATABASES_SQL
from pg_debug.health.checks.multixact import MEMBER_FILES_ALLOWED_SQL
from pg_debug.health.checks.multixact import MULTIXACT_STOP_AGE
from pg_debug.health.checks.multixact import SETTINGS_SQL
from pg_debug.health.checks.multixact import TABLES_SQL
from pg_debug.health.checks.multixact import MultixactCalc
from pg_debug.health.checks.multixact import slru_sql

GIB = 1024**3


class FakeDriver(SqlDriver):
    """Answers each read from the server it describes."""

    def __init__(
        self,
        databases: list[dict[str, Any]] | None = None,
        tables: list[dict[str, Any]] | None = None,
        version: int = 16,
        files_allowed: bool = True,
        member_bytes: int = 8192,
        freeze_max_age: int = 400_000_000,
    ):
        self.databases = databases if databases is not None else [{"database": "app", "mxid_age": 1_000, "allows_connections": True}]
        self.tables = tables or []
        self.version = version
        self.files_allowed = files_allowed
        self.member_bytes = member_bytes
        self.freeze_max_age = freeze_max_age
        self.queries: list[str] = []

    async def execute_query(self, query, params=None, force_readonly=False, *, in_context=False, cleanup=None):  # type: ignore[override]
        self.queries.append(query)
        rows: list[dict[str, Any]]
        if "server_version_num" in query:
            rows = [{"server_version_num": str(self.version * 10000)}]
        elif "AS freeze_max_age" in query and "pg_class" not in query:
            rows = [{"freeze_max_age": self.freeze_max_age}]
        elif "FROM pg_database" in query:
            rows = self.databases
        elif "FROM pg_class" in query:
            rows = self.tables
        elif "pg_stat_slru" in query:
            rows = [
                {"name": "MultiXactMember", "blks_zeroed": 12, "blks_hit": 0, "blks_read": 0, "blks_written": 3, "truncates": 1, "stats_reset": None}
            ]
        elif "has_function_privilege" in query:
            rows = [{"allowed": self.files_allowed}]
        elif "pg_ls_dir" in query:
            rows = [{"files": 3, "bytes": self.member_bytes}]
        else:
            raise AssertionError(f"unexpected query: {query}")
        return [SqlDriver.RowResult(cells=cells) for cells in rows]


def table(age: int, freeze_max_age: int = 400_000_000) -> dict[str, Any]:
    return {"schema": "public", "table": "orders", "mxid_age": age, "freeze_max_age": freeze_max_age}


@pytest.mark.asyncio
async def test_young_multixacts_are_healthy_and_the_oldest_is_named():
    result = await MultixactCalc(FakeDriver()).multixact_check()

    assert result.healthy
    assert "The oldest multixact is 1,000 old, in database 'app'" in result.summary
    assert result.metrics["autovacuum_multixact_freeze_max_age"] == 400_000_000


@pytest.mark.asyncio
async def test_past_the_alert_age_is_flagged_before_the_setting_is_reached():
    """Emergency vacuums have begun near 200 million with the setting at 400 million."""
    driver = FakeDriver(databases=[{"database": "app", "mxid_age": 210_000_000, "allows_connections": True}])

    result = await MultixactCalc(driver).multixact_check()

    assert not result.healthy
    finding = result.findings[0]
    assert finding["kind"] == "database"
    assert finding["level"] == "approaching"
    assert finding["confidence"] == "proven"
    assert "Database 'app' has a multixact age of 210,000,000, past the alert age of 200,000,000" in result.summary


@pytest.mark.asyncio
async def test_a_table_past_its_own_freeze_max_age_means_autovacuum_is_not_keeping_up():
    driver = FakeDriver(tables=[table(150_000_000, freeze_max_age=100_000_000)])

    result = await MultixactCalc(driver).multixact_check()

    finding = result.findings[0]
    assert (finding["kind"], finding["level"]) == ("table", "past_freeze_max_age")
    assert "Table 'public.orders' has a multixact age of 150,000,000, past autovacuum_multixact_freeze_max_age of 100,000,000" in result.summary


@pytest.mark.asyncio
async def test_near_the_hard_limit_says_how_many_are_left():
    age = MULTIXACT_STOP_AGE - 5_000_000
    driver = FakeDriver(databases=[{"database": "app", "mxid_age": age, "allows_connections": True}])

    result = await MultixactCalc(driver).multixact_check()

    assert result.findings[0]["level"] == "wraparound"
    assert "5,000,000 multixacts from the point where PostgreSQL stops assigning them" in result.summary


@pytest.mark.asyncio
async def test_no_member_space_percentage_is_ever_given():
    result = await MultixactCalc(FakeDriver()).multixact_check()

    assert "PostgreSQL publishes no member-space percentage" in result.summary
    assert result.metrics["member_space_percent"] is None
    assert result.metrics["member_files"] == {"files": 3, "bytes": 8192}
    assert result.metrics["member_slru"]["blks_zeroed"] == 12


@pytest.mark.asyncio
async def test_large_member_files_are_a_candidate_finding():
    result = await MultixactCalc(FakeDriver(member_bytes=12 * GIB)).multixact_check()

    assert not result.healthy
    finding = result.findings[0]
    assert finding["kind"] == "member_space"
    assert finding["confidence"] == "candidate"
    assert "pg_multixact/members takes 12.0 GiB on disk, past the 10.0 GiB" in result.summary
    assert "A candidate" in result.summary


@pytest.mark.asyncio
async def test_member_files_unreadable_by_the_role_are_said_to_be_unread():
    driver = FakeDriver(files_allowed=False)

    result = await MultixactCalc(driver).multixact_check()

    assert result.metrics["member_files"] is None
    assert "the role may not execute pg_ls_dir and pg_stat_file" in result.summary
    assert not any("pg_ls_dir('pg_multixact" in q for q in driver.queries)


@pytest.mark.asyncio
async def test_restricted_mode_does_not_list_the_data_directory():
    inner = FakeDriver()

    result = await MultixactCalc(SafeSqlDriver(inner)).multixact_check()

    assert result.metrics["member_files"] is None
    assert "restricted mode does not run pg_ls_dir" in result.summary
    assert not any("pg_ls_dir" in q for q in inner.queries)


@pytest.mark.asyncio
async def test_before_13_there_is_no_slru_row():
    driver = FakeDriver(version=12)

    result = await MultixactCalc(driver).multixact_check()

    assert result.metrics["member_slru"] is None
    assert not any("pg_stat_slru" in q for q in driver.queries)


def test_the_slru_row_was_renamed_in_17():
    assert "'MultiXactMember'" in slru_sql(16)
    assert "'multixact_member'" in slru_sql(17)


def test_a_toast_table_counts_against_the_table_it_belongs_to():
    assert "mxid_age(t.relminmxid)" in TABLES_SQL
    assert "c.reltoastrelid" in TABLES_SQL


@pytest.mark.parametrize("version", [13, 17])
def test_the_multixact_reads_run_in_restricted_mode(version):
    safe = SafeSqlDriver(MagicMock(spec=SqlDriver))
    for sql in (SETTINGS_SQL, DATABASES_SQL, TABLES_SQL.format(50), slru_sql(version), MEMBER_FILES_ALLOWED_SQL):
        safe._validate(sql)

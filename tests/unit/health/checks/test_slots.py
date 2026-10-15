"""Replication slot retention: what each slot keeps, and which of it is a problem."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.health import DatabaseHealthTool
from pg_debug.health import HealthType
from pg_debug.health.checks.slots import SETTINGS_SQL
from pg_debug.health.checks.slots import SLOT_RETENTION
from pg_debug.health.checks.slots import SlotRetentionCalc
from pg_debug.health.checks.slots import slots_sql

GIB = 1024**3


def slot(**overrides: Any) -> dict[str, Any]:
    cells = {
        "slot_name": "standby_1",
        "slot_type": "physical",
        "database": None,
        "plugin": None,
        "active": True,
        "temporary": False,
        "wal_status": "reserved",
        "safe_wal_size": None,
        "inactive_since": None,
        "invalidation_reason": None,
        "restart_lsn": "0/3000000",
        "retained_bytes": 16 * 1024**2,
        "xmin_age": None,
        "catalog_xmin_age": None,
    }
    return {**cells, **overrides}


class FakeDriver:
    def __init__(self, slots: list[dict[str, Any]], version: int = 16, keep_mb: int = -1, replica: bool = False):
        self.slots = slots
        self.version = version
        self.keep_mb = keep_mb
        self.replica = replica
        self.queries: list[str] = []

    async def execute_query(self, query: str, *args: Any, **kwargs: Any) -> list[SqlDriver.RowResult]:
        self.queries.append(query)
        if "server_version_num" in query:
            return [SqlDriver.RowResult(cells={"server_version_num": str(self.version * 10000)})]
        if "max_slot_wal_keep_size" in query:
            return [
                SqlDriver.RowResult(
                    cells={
                        "is_replica": self.replica,
                        "max_wal_size_mb": 1024,
                        "max_slot_wal_keep_size_mb": self.keep_mb if self.version >= 13 else None,
                        "autovacuum_freeze_max_age": 200_000_000,
                    }
                )
            ]
        if "pg_replication_slots" in query:
            return [SqlDriver.RowResult(cells=cells) for cells in self.slots]
        raise AssertionError(f"unexpected query: {query}")


async def check(slots: list[dict[str, Any]], **kwargs: Any):
    return await SlotRetentionCalc(FakeDriver(slots, **kwargs)).slot_retention_check()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_no_slots_is_healthy_and_says_so():
    result = await check([])

    assert result.healthy
    assert result.summary == "No replication slots."
    assert result.metrics["slots_examined"] == 0


@pytest.mark.asyncio
async def test_a_connected_slot_within_max_wal_size_is_listed_as_examined_not_flagged():
    result = await check([slot()], keep_mb=10240)

    assert result.healthy
    assert result.findings == []
    assert result.metrics["examined"][0]["slot_name"] == "standby_1"
    assert "Examined and within max_wal_size (1.0 GiB): 'standby_1' (connected, retains 16.0 MiB)" in result.summary


@pytest.mark.asyncio
async def test_an_inactive_slot_with_unbounded_retention_is_the_disk_filling():
    result = await check([slot(active=False, retained_bytes=40 * GIB, wal_status="extended")])

    assert not result.healthy
    finding = result.findings[0]
    assert finding["kind"] == "unbounded"
    assert finding["confidence"] == "proven"
    assert finding["retained_bytes"] == 40 * GIB
    assert "is inactive and retains 40.0 GiB of WAL; max_slot_wal_keep_size is -1" in result.summary
    assert "until the disk holding pg_wal fills" in result.summary


@pytest.mark.asyncio
async def test_an_inactive_slot_within_a_bound_says_how_much_is_left_before_it_is_invalidated():
    result = await check([slot(active=False, retained_bytes=2 * GIB, safe_wal_size=3 * GIB)], keep_mb=5120)

    assert result.findings[0]["kind"] == "inactive"
    assert "retains 2.0 GiB of WAL, 3.0 GiB before it is invalidated" in result.summary
    assert result.metrics["max_slot_wal_keep_size_bytes"] == 5 * GIB


@pytest.mark.asyncio
async def test_a_lost_slot_is_reported_as_lost_whatever_else_it_is():
    result = await check([slot(active=False, wal_status="lost", restart_lsn=None, retained_bytes=None, invalidation_reason="wal_removed")])

    assert result.findings[0]["kind"] == "lost"
    assert "has lost the WAL its consumer needs (wal_removed)" in result.summary


@pytest.mark.asyncio
async def test_an_unreserved_slot_is_about_to_be_lost():
    result = await check([slot(wal_status="unreserved", retained_bytes=6 * GIB)], keep_mb=5120)

    assert result.findings[0]["kind"] == "unreserved"
    assert "the next checkpoint removes WAL it needs" in result.summary


@pytest.mark.asyncio
async def test_a_connected_slot_past_max_wal_size_has_a_consumer_behind():
    result = await check([slot(wal_status="extended", retained_bytes=3 * GIB)], keep_mb=10240)

    assert result.findings[0]["kind"] == "extended"
    assert "more than max_wal_size (1.0 GiB): its consumer is behind" in result.summary


@pytest.mark.asyncio
async def test_a_logical_slot_holding_the_catalog_past_freeze_max_age_is_reported():
    logical = slot(slot_name="sub", slot_type="logical", database="app", plugin="pgoutput", catalog_xmin_age=250_000_000)

    result = await check([logical], keep_mb=10240)

    assert result.findings[0]["kind"] == "horizon"
    assert "its catalog_xmin holds catalog rows 250,000,000 transactions back" in result.summary
    assert "older than autovacuum_freeze_max_age (200,000,000)" in result.summary


@pytest.mark.asyncio
async def test_an_inactive_logical_slot_also_says_what_catalog_horizon_it_holds():
    logical = slot(slot_name="sub", slot_type="logical", active=False, catalog_xmin_age=1_000)

    result = await check([logical])

    assert "(its catalog_xmin holds catalog rows 1,000 transactions back)" in result.summary


@pytest.mark.asyncio
async def test_a_slot_that_reserves_nothing_retains_nothing():
    result = await check([slot(active=False, restart_lsn=None, retained_bytes=None)])

    assert result.healthy
    assert "'standby_1' (reserves no WAL)" in result.summary


@pytest.mark.asyncio
async def test_before_13_there_is_no_bound_and_no_wal_status():
    driver = FakeDriver([slot(active=False, wal_status=None, retained_bytes=GIB)], version=12)

    result = await SlotRetentionCalc(driver).slot_retention_check()  # type: ignore[arg-type]

    assert result.findings[0]["kind"] == "unbounded"
    assert "PostgreSQL before 13 cannot bound it" in result.summary
    sql = next(q for q in driver.queries if "pg_replication_slots" in q)
    assert "NULL::text AS wal_status" in sql


@pytest.mark.asyncio
async def test_before_13_a_connected_slot_past_max_wal_size_is_behind():
    result = await check([slot(wal_status=None, retained_bytes=2 * GIB)], version=12)

    assert result.findings[0]["kind"] == "extended"


def test_the_columns_follow_the_version():
    assert "s.wal_status, s.safe_wal_size" not in slots_sql(12)
    assert "s.wal_status, s.safe_wal_size" in slots_sql(13)
    assert "s.inactive_since" not in slots_sql(16)
    assert "s.inactive_since, s.invalidation_reason" in slots_sql(17)


def test_a_standby_measures_from_what_it_has_replayed():
    """A standby writes no WAL; pg_current_wal_lsn() raises there."""
    assert "CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() ELSE pg_current_wal_lsn() END" in slots_sql(16)


@pytest.mark.parametrize("version", [12, 13, 16, 17])
def test_the_slot_reads_run_in_restricted_mode(version):
    safe = SafeSqlDriver(MagicMock(spec=SqlDriver))
    safe._validate(slots_sql(version))
    safe._validate(SETTINGS_SQL)


def test_slot_retention_runs_with_the_replication_checks():
    names = [name for name, _ in DatabaseHealthTool(FakeDriver([]))._selected_checks({HealthType.REPLICATION})]

    assert names == ["Replication health", SLOT_RETENTION]

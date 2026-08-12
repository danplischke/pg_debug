"""Why dead rows accumulate: the three reasons, told apart."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.database_health import DatabaseHealthTool
from pg_debug.database_health.autovacuum_calc import BACKLOG_SQL
from pg_debug.database_health.autovacuum_calc import DEFAULT_LARGE_TABLE_ROWS
from pg_debug.database_health.autovacuum_calc import DEFAULT_MIN_DEAD_ROWS
from pg_debug.database_health.autovacuum_calc import HORIZON_SQL
from pg_debug.database_health.autovacuum_calc import AutovacuumCalc
from pg_debug.sql import SqlDriver


def driver_returning(rows):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=cells) for cells in rows])
    return driver


def table(**overrides):
    cells = {
        "schema": "public",
        "table": "events",
        "dead_rows": 500_000,
        "live_rows": 2_000_000,
        "estimated_rows": 2_000_000,
        "table_bytes": 4 * 1024**3,
        "vacuum_threshold": 400_050,
        "effective_scale": 0.2,
        "scale_factor_is_set": False,
        "autovacuum_enabled": True,
        "autovacuum_count": 3,
        "last_autovacuum": "2026-08-01 03:00:00+00",
        "last_vacuum": None,
        "reason": "behind",
    }
    return {**cells, **overrides}


# -- Backlog ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_to_report_is_healthy():
    check = await AutovacuumCalc(driver_returning([])).autovacuum_backlog_check()

    assert check.healthy
    assert check.summary == "Autovacuum is keeping up."
    assert check.metrics == {"min_dead_rows": DEFAULT_MIN_DEAD_ROWS, "large_table_rows": DEFAULT_LARGE_TABLE_ROWS}


@pytest.mark.asyncio
async def test_a_table_past_its_threshold_reports_when_it_was_last_vacuumed():
    check = await AutovacuumCalc(driver_returning([table()])).autovacuum_backlog_check()

    assert not check.healthy
    assert "500,000 dead rows, past its threshold of 400,050" in check.summary
    assert "last vacuumed 2026-08-01 03:00:00+00" in check.summary


@pytest.mark.asyncio
async def test_a_table_never_vacuumed_says_so_rather_than_showing_a_blank():
    check = await AutovacuumCalc(driver_returning([table(last_autovacuum=None)])).autovacuum_backlog_check()

    assert "never vacuumed" in check.summary


@pytest.mark.asyncio
async def test_autovacuum_switched_off_is_reported_as_that_and_not_as_lateness():
    """The fix is completely different, so the two must not read the same."""
    check = await AutovacuumCalc(driver_returning([table(reason="disabled")])).autovacuum_backlog_check()

    assert "autovacuum switched off for it" in check.summary
    assert "past its threshold" not in check.summary


@pytest.mark.asyncio
async def test_a_large_table_on_the_default_scale_factor_is_reported_before_it_is_late():
    """A fifth of ten million rows is two million dead rows of waiting. That is
    a decision nobody made deliberately."""
    check = await AutovacuumCalc(driver_returning([table(reason="threshold_too_high", dead_rows=50_000)])).autovacuum_backlog_check()

    assert "autovacuum waits for 400,050 dead rows before it runs" in check.summary


@pytest.mark.asyncio
async def test_the_thresholds_reach_the_query():
    driver = driver_returning([])

    await AutovacuumCalc(driver, min_dead_rows=42, large_table_rows=99).autovacuum_backlog_check()

    query = driver.execute_query.call_args[0][0]
    assert "42" in query
    assert "99" in query


def test_a_per_table_override_wins_over_the_server_default():
    """Setting the scale factor per table is the fix, so a table that has one
    must not keep being reported."""
    assert "coalesce(t.own_scale, d.scale)" in BACKLOG_SQL
    assert "coalesce(t.own_base, d.base)" in BACKLOG_SQL


def test_the_threshold_is_the_one_postgresql_actually_uses():
    assert "autovacuum_vacuum_threshold" in BACKLOG_SQL
    assert "autovacuum_vacuum_scale_factor" in BACKLOG_SQL


# -- Horizon ----------------------------------------------------------------


def holder(**overrides):
    cells = {
        "kind": "transaction",
        "holder": "4242",
        "state": "idle in transaction",
        "holds": "a snapshot",
        "transactions_held": 1_500_000,
        "seconds_held": 3600,
        "detail": "SELECT ...",
    }
    return {**cells, **overrides}


@pytest.mark.asyncio
async def test_an_unheld_horizon_is_healthy():
    check = await AutovacuumCalc(driver_returning([])).vacuum_horizon_check()

    assert check.healthy
    assert check.summary == "Nothing is holding back the vacuum horizon."


@pytest.mark.asyncio
async def test_a_backend_is_reported_with_what_it_holds_and_for_how_long():
    check = await AutovacuumCalc(driver_returning([holder()])).vacuum_horizon_check()

    assert not check.healthy
    assert "Backend 4242 has been idle in transaction for 60 minutes, holding a snapshot" in check.summary
    assert "1,500,000 transactions" in check.summary


@pytest.mark.asyncio
async def test_an_abandoned_replication_slot_is_reported():
    check = await AutovacuumCalc(driver_returning([holder(kind="replication slot", holder="forgotten", state="inactive")])).vacuum_horizon_check()

    assert "Replication slot 'forgotten' is inactive" in check.summary


@pytest.mark.asyncio
async def test_a_prepared_transaction_is_reported():
    check = await AutovacuumCalc(driver_returning([holder(kind="prepared transaction", holder="txn-1")])).vacuum_horizon_check()

    assert "Prepared transaction 'txn-1' has been uncommitted" in check.summary


def test_only_backends_that_really_hold_the_horizon_are_considered():
    """Measured: a READ COMMITTED transaction that has only read holds neither
    a snapshot nor an xid between statements, so 'idle in transaction' on its
    own is not the condition — despite the usual advice."""
    assert "a.backend_xmin IS NOT NULL OR a.backend_xid IS NOT NULL" in HORIZON_SQL
    assert "a.state = 'idle in transaction'" not in HORIZON_SQL


def test_all_three_kinds_of_holder_are_looked_for():
    assert "pg_stat_activity" in HORIZON_SQL
    assert "pg_replication_slots" in HORIZON_SQL
    assert "pg_prepared_xacts" in HORIZON_SQL


def test_our_own_connection_is_not_reported_as_the_culprit():
    assert "a.pid <> pg_backend_pid()" in HORIZON_SQL


# -- Wiring -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_vacuum_checks_run_together():
    """Wraparound, backlog and horizon are three views of the same subject."""
    results = await DatabaseHealthTool(driver_returning([])).run_checks("vacuum")

    assert list(results) == ["Vacuum health", "Autovacuum backlog", "Vacuum horizon"]

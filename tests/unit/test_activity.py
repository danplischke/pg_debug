from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.activity import ActivityInspector
from pg_debug.db import SqlDriver


def make_driver(rows=()):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=row) for row in rows])
    return driver


def executed_query(driver):
    return driver.execute_query.call_args[0][0]


@pytest.fixture
def pg16():
    """Report PostgreSQL 16, the common case for both queries."""
    with patch("pg_debug.activity.get_postgres_version", AsyncMock(return_value=16)):
        yield


# -- Backends ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_backends_exclude_our_own_connection(pg16):
    driver = make_driver()

    await ActivityInspector(driver).list_backends()

    assert "a.pid <> pg_backend_pid()" in executed_query(driver)


@pytest.mark.asyncio
async def test_backends_hide_idle_by_default(pg16):
    driver = make_driver()

    await ActivityInspector(driver).list_backends()

    query = executed_query(driver)
    assert "a.state IS DISTINCT FROM 'idle'" in query
    # Idle auxiliary processes report an 'Activity' wait.
    assert "wait_event_type = 'Activity'" in query


@pytest.mark.asyncio
async def test_backends_can_include_idle(pg16):
    driver = make_driver()

    await ActivityInspector(driver).list_backends(include_idle=True)

    assert "IS DISTINCT FROM 'idle'" not in executed_query(driver)


@pytest.mark.asyncio
async def test_backends_filter_by_duration(pg16):
    driver = make_driver()

    await ActivityInspector(driver).list_backends(min_duration_seconds=30)

    query = executed_query(driver)
    assert "clock_timestamp() - a.state_change >=" in query
    assert "make_interval(secs => 30" in query


@pytest.mark.asyncio
async def test_backends_return_rows(pg16):
    driver = make_driver([{"pid": 42, "blocked_by": [41], "state": "active"}])

    assert await ActivityInspector(driver).list_backends() == [{"pid": 42, "blocked_by": [41], "state": "active"}]


@pytest.mark.asyncio
async def test_query_id_is_selected_on_pg14_and_later():
    driver = make_driver()
    with patch("pg_debug.activity.get_postgres_version", AsyncMock(return_value=14)):
        await ActivityInspector(driver).list_backends()

    assert "a.query_id" in executed_query(driver)


@pytest.mark.asyncio
async def test_query_id_is_null_before_pg14():
    driver = make_driver()
    with patch("pg_debug.activity.get_postgres_version", AsyncMock(return_value=13)):
        await ActivityInspector(driver).list_backends()

    assert "NULL::bigint AS query_id" in executed_query(driver)


@pytest.mark.asyncio
async def test_blocked_backends_keep_only_waiters(pg16):
    driver = make_driver(
        [
            {"pid": 1, "blocked_by": []},
            {"pid": 2, "blocked_by": [1]},
            {"pid": 3, "blocked_by": None},
        ]
    )

    assert [row["pid"] for row in await ActivityInspector(driver).blocked_backends()] == [2]


@pytest.mark.asyncio
async def test_blocked_backends_look_at_idle_ones_too(pg16):
    """A backend can be blocked while idle in transaction."""
    driver = make_driver()

    await ActivityInspector(driver).blocked_backends()

    assert "IS DISTINCT FROM 'idle'" not in executed_query(driver)


# -- Vacuum progress --------------------------------------------------------


@pytest.mark.asyncio
async def test_vacuum_progress_uses_legacy_counters_before_pg17():
    driver = make_driver()
    with patch("pg_debug.activity.get_postgres_version", AsyncMock(return_value=16)):
        await ActivityInspector(driver).vacuum_progress()

    query = executed_query(driver)
    assert "v.num_dead_tuples AS dead_item_ids" in query
    assert "v.max_dead_tuples AS max_dead_item_ids" in query
    assert "NULL::bigint AS dead_tuple_bytes" in query
    assert "num_dead_item_ids" not in query.replace("AS dead_item_ids", "")


@pytest.mark.asyncio
async def test_vacuum_progress_uses_byte_counters_on_pg17():
    driver = make_driver()
    with patch("pg_debug.activity.get_postgres_version", AsyncMock(return_value=17)):
        await ActivityInspector(driver).vacuum_progress()

    query = executed_query(driver)
    assert "v.num_dead_item_ids AS dead_item_ids" in query
    assert "v.dead_tuple_bytes" in query
    assert "v.indexes_processed" in query
    assert "v.max_dead_tuples" not in query


@pytest.mark.asyncio
async def test_vacuum_progress_reports_percentage(pg16):
    driver = make_driver([{"pid": 7, "percent_scanned": 79.0, "phase": "scanning heap"}])

    rows = await ActivityInspector(driver).vacuum_progress()

    assert rows[0]["percent_scanned"] == 79.0
    assert "percent_scanned" in executed_query(driver)


@pytest.mark.asyncio
async def test_vacuum_progress_when_nothing_is_running(pg16):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=None)

    assert await ActivityInspector(driver).vacuum_progress() == []

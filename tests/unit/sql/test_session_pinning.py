"""SqlDriver.session() pins one pooled connection.

Session state — hypothetical indexes, SET, temp tables, advisory locks — lives
in the connection that created it, so anything that depends on it has to be
told which connection to use.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.sql import DbConnPool
from pg_debug.sql import SafeSqlDriver
from pg_debug.sql import SqlDriver


class RotatingPool:
    """A pool handing out a different connection object on every borrow."""

    def __init__(self):
        self.handed_out = []

    @asynccontextmanager
    async def connection(self):
        conn = f"connection-{len(self.handed_out)}"
        self.handed_out.append(conn)
        yield conn


class RotatingConnPool(DbConnPool):
    def __init__(self):
        super().__init__("postgresql://localhost/test")
        self.rotating = RotatingPool()

    async def pool_connect(self, connection_url=None):  # type: ignore[override]
        return self.rotating  # type: ignore[return-value]


class RecordingDriver(SqlDriver):
    """Records which connection each statement was executed on."""

    def __init__(self, conn):
        super().__init__(conn=conn)
        self.connections_used = []

    async def _execute_with_connection(self, connection, query, params, force_readonly):
        self.connections_used.append(connection)
        return None


@pytest.fixture
def driver():
    return RecordingDriver(RotatingConnPool())


@pytest.mark.asyncio
async def test_without_a_session_each_query_may_land_on_a_different_connection(driver):
    await driver.execute_query("SELECT 1")
    await driver.execute_query("SELECT 2")

    assert driver.connections_used == ["connection-0", "connection-1"]


@pytest.mark.asyncio
async def test_a_session_pins_every_query_to_one_connection(driver):
    async with driver.session():
        await driver.execute_query("SELECT hypopg_create_index('CREATE INDEX ON t (c)')")
        await driver.execute_query("SELECT * FROM hypopg()")
        await driver.execute_query("SELECT hypopg_reset()")

    assert driver.connections_used == ["connection-0"] * 3
    assert driver.conn.rotating.handed_out == ["connection-0"]


@pytest.mark.asyncio
async def test_the_pin_is_released_when_the_session_ends(driver):
    async with driver.session():
        await driver.execute_query("SELECT 1")

    assert driver._pinned_connection is None

    await driver.execute_query("SELECT 2")

    assert driver.connections_used == ["connection-0", "connection-1"]


@pytest.mark.asyncio
async def test_the_pin_is_released_when_the_body_raises(driver):
    with pytest.raises(RuntimeError):
        async with driver.session():
            await driver.execute_query("SELECT 1")
            raise RuntimeError("boom")

    assert driver._pinned_connection is None


@pytest.mark.asyncio
async def test_a_nested_session_reuses_the_outer_pin(driver):
    async with driver.session():
        await driver.execute_query("SELECT 1")
        async with driver.session():
            await driver.execute_query("SELECT 2")
        # The inner block exiting must not release the outer pin.
        await driver.execute_query("SELECT 3")

    assert driver.connections_used == ["connection-0"] * 3
    assert driver.conn.rotating.handed_out == ["connection-0"]


@pytest.mark.asyncio
async def test_a_direct_connection_needs_no_pin():
    """A driver holding one connection is already a session."""
    driver = RecordingDriver(MagicMock())
    driver.is_pool = False

    async with driver.session():
        await driver.execute_query("SELECT 1")

    assert driver.connections_used == [driver.conn]


@pytest.mark.asyncio
async def test_safe_sql_driver_proxies_the_session():
    """Restricted mode has to pin the same way, or hypopg breaks there instead."""
    inner = RecordingDriver(RotatingConnPool())
    safe = SafeSqlDriver(sql_driver=inner)

    async with safe.session():
        await safe.execute_query("SELECT 1")
        await safe.execute_query("SELECT 2")

    assert inner.connections_used == ["connection-0"] * 2


@pytest.mark.asyncio
async def test_index_tuning_runs_inside_a_session():
    """Otherwise the hypothetical indexes it creates can vanish mid-analysis."""
    from pg_debug.index.dta_calc import DatabaseTuningAdvisor

    sql_driver = MagicMock(spec=SqlDriver)
    sql_driver.execute_query = AsyncMock(return_value=[])
    entered = []

    @asynccontextmanager
    async def tracking_session():
        entered.append(True)
        yield sql_driver

    sql_driver.session = tracking_session

    advisor = DatabaseTuningAdvisor(sql_driver)
    # Prechecks fail on the empty response, which is enough: the session has to
    # be entered before them.
    await advisor.analyze_workload(query_list=["SELECT 1"])

    assert entered == [True]

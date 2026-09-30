"""SqlDriver.session() pins one pooled connection.

Session state — hypothetical indexes, SET, temp tables, advisory locks — lives
in the connection that created it, so anything that depends on it has to be
told which connection to use.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import QUERY_MARKER
from pg_debug.db import DbConnPool
from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver


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
        self.executed = []

    async def _execute_with_connection(self, connection, query, params, force_readonly):
        self.connections_used.append(connection)
        self.executed.append(query)
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
    from pg_debug.index.dta import DatabaseTuningAdvisor

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


class FailingDriver(SqlDriver):
    """Fails one nominated statement, and records rollbacks on the pin."""

    def __init__(self, conn, failing_query: str):
        super().__init__(conn=conn)
        self.failing_query = failing_query
        self.rollbacks = 0

    async def _execute_with_connection(self, connection, query, params, force_readonly):
        # The statement arrives tagged, so match on what the caller asked for.
        if self.failing_query in query:
            raise RuntimeError("operator does not exist: information_schema.character_data = integer")
        return None

    async def _reset_pinned_transaction(self):
        self.rollbacks += 1
        await super()._reset_pinned_transaction()


@pytest.mark.asyncio
async def test_a_failed_statement_does_not_poison_the_rest_of_the_session():
    """Outside a session the connection goes back to the pool and is reset. Pinned,
    it stays — so without this every later statement fails with 'current
    transaction is aborted' and one recoverable error takes the whole session.
    """
    driver = FailingDriver(RotatingConnPool(), failing_query="BOOM")

    async with driver.session():
        with pytest.raises(RuntimeError):
            await driver.execute_query("BOOM")
        # The session is still usable.
        await driver.execute_query("SELECT 1")

    assert driver.rollbacks == 1


@pytest.mark.asyncio
async def test_the_original_error_survives_a_failed_rollback():
    """Whatever went wrong clearing the transaction, the caller needs to see the
    error that caused it, not the one from the cleanup."""
    driver = FailingDriver(RotatingConnPool(), failing_query="BOOM")

    async with driver.session():
        driver._pinned_connection = MagicMock()
        driver._pinned_connection.rollback = AsyncMock(side_effect=RuntimeError("rollback failed"))

        with pytest.raises(RuntimeError, match="operator does not exist"):
            await driver.execute_query("BOOM")


@pytest.mark.asyncio
async def test_a_failure_outside_a_session_needs_no_reset():
    """The pool resets a returned connection, so there is nothing to clear."""
    driver = FailingDriver(RotatingConnPool(), failing_query="BOOM")

    with pytest.raises(RuntimeError):
        await driver.execute_query("BOOM")

    assert driver.rollbacks == 0


# -- Tagging ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_statement_is_tagged_as_ours(driver):
    """Unrestricted mode is the default, and a statement pg_debug ran to answer
    a question about the workload is not part of that workload."""
    await driver.execute_query("SELECT 1")

    assert driver.executed == ["/* pg_debug */ SELECT 1"]


@pytest.mark.asyncio
async def test_a_statement_is_not_tagged_twice(driver):
    """SafeSqlDriver used to tag, and delegates to this driver, which now does."""
    await driver.execute_query("/* pg_debug */ SELECT 1")

    assert driver.executed == ["/* pg_debug */ SELECT 1"]


@pytest.mark.asyncio
async def test_restricted_mode_tags_exactly_once():
    inner = RecordingDriver(RotatingConnPool())
    safe = SafeSqlDriver(sql_driver=inner)

    await safe.execute_query("SELECT 1")

    assert inner.executed == ["/* pg_debug */ SELECT 1"]


def test_the_marker_leads_the_statement():
    """PostgreSQL keeps a leading comment in the text pg_stat_statements
    records; a marker placed elsewhere would be harder to match on."""
    assert SqlDriver.tagged("SELECT 1").startswith(QUERY_MARKER)

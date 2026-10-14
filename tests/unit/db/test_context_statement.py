"""How the driver sets up a transaction: the query context inside the statement's own transaction, and only when
asked; the pool's timeout and search path in every transaction; a cleanup after the statement, even a failed one."""

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import psycopg
import pytest

from pg_debug import QueryContext
from pg_debug.db import QUERY_MARKER
from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.db.driver import DbConnPool
from pg_debug.db.driver import build_transaction_setup
from pg_debug.errors import InvalidArgumentError
from pg_debug.errors import QueryError

CONTEXT = QueryContext(role="app_user", settings={"app.tenant_id": "t7", "work_mem": "64MB"})
APPLIED = f"{QUERY_MARKER} SELECT set_config('app.tenant_id', 't7', true), set_config('work_mem', '64MB', true), set_config('role', 'app_user', true)"


class FakeCursor:
    def __init__(self, connection: "FakeConnection"):
        self.connection = connection
        self.description = [("x",)]

    async def execute(self, query: Any, params: Any = None) -> None:
        text = query if isinstance(query, str) else query.as_string(None)
        self.connection.executed.append(text)
        if self.connection.refuse and "set_config" in text:
            raise psycopg.errors.InsufficientPrivilege('permission denied to set role "app_user"')
        if self.connection.fail_on and self.connection.fail_on in text:
            if self.connection.cancel:
                # As psycopg raises it once it has cancelled the statement on the server.
                raise asyncio.CancelledError
            raise psycopg.errors.UndefinedColumn('column "no_such_column" does not exist')

    def nextset(self) -> None:
        return None

    async def fetchall(self) -> list[dict[str, Any]]:
        return [{"x": 1}]


class FakeConnection:
    """A direct connection that records the statements run on it."""

    def __init__(self, autocommit: bool = False, refuse: bool = False, fail_on: str | None = None, cancel: bool = False):
        self.autocommit = autocommit
        self.refuse = refuse
        self.fail_on = fail_on
        self.cancel = cancel
        self.executed: list[str] = []
        self.rollbacks = 0

    @asynccontextmanager
    async def cursor(self, row_factory: Any = None):
        yield FakeCursor(self)

    async def rollback(self) -> None:
        self.rollbacks += 1


def test_an_empty_context_applies_nothing():
    assert build_transaction_setup(context=None) is None
    assert build_transaction_setup(context=QueryContext()) is None


def test_settings_are_applied_in_order_and_the_role_last():
    """The role last, so a setting only the connecting user may change is set before the switch."""
    statement = build_transaction_setup(context=CONTEXT)

    assert statement is not None
    assert statement.as_string(None) == APPLIED


def test_names_and_values_are_quoted_as_literals():
    statement = build_transaction_setup(context=QueryContext(role='x"; DROP ROLE y; --', settings={"app.name": "it's"}))

    assert statement is not None
    assert statement.as_string(None).endswith("set_config('app.name', 'it''s', true), set_config('role', 'x\"; DROP ROLE y; --', true)")


@pytest.mark.asyncio
async def test_the_context_is_applied_inside_the_read_only_transaction():
    connection = FakeConnection()

    await SqlDriver(conn=connection, context=CONTEXT).execute_query("SELECT 1", force_readonly=True, in_context=True)

    assert connection.executed == ["BEGIN TRANSACTION READ ONLY", APPLIED, f"{QUERY_MARKER} SELECT 1", "ROLLBACK"]


@pytest.mark.asyncio
async def test_the_context_shares_the_transaction_psycopg_opens():
    connection = FakeConnection()

    await SqlDriver(conn=connection, context=CONTEXT).execute_query("SELECT 1", in_context=True)

    assert connection.executed == [APPLIED, f"{QUERY_MARKER} SELECT 1", "COMMIT"]


@pytest.mark.asyncio
async def test_an_autocommit_connection_gets_a_transaction_for_the_context_to_last_in():
    """Outside a transaction block a set_config(..., true) ends with its own statement."""
    connection = FakeConnection(autocommit=True)

    await SqlDriver(conn=connection, context=CONTEXT).execute_query("SELECT 1", in_context=True)

    assert connection.executed == ["BEGIN", APPLIED, f"{QUERY_MARKER} SELECT 1", "COMMIT"]


@pytest.mark.asyncio
async def test_a_statement_not_in_context_runs_as_the_connecting_user():
    connection = FakeConnection()

    await SqlDriver(conn=connection, context=CONTEXT).execute_query("SELECT 1", force_readonly=True)

    assert APPLIED not in connection.executed


@pytest.mark.asyncio
async def test_a_rolled_back_statement_runs_in_the_context_too():
    connection = FakeConnection(autocommit=True)

    await SqlDriver(conn=connection, context=CONTEXT).execute_query_and_rollback("EXPLAIN ANALYZE DELETE FROM t", in_context=True)

    assert connection.executed == ["BEGIN", APPLIED, f"{QUERY_MARKER} EXPLAIN ANALYZE DELETE FROM t"]
    assert connection.rollbacks == 1


@pytest.mark.asyncio
async def test_a_context_the_server_refuses_says_which_and_why():
    connection = FakeConnection(refuse=True)

    with pytest.raises(InvalidArgumentError) as excinfo:
        await SqlDriver(conn=connection, context=CONTEXT).execute_query("SELECT 1", in_context=True)

    assert str(excinfo.value) == (
        'Could not apply the query context (role app_user, app.tenant_id=t7, work_mem=64MB): permission denied to set role "app_user"'
    )
    assert isinstance(excinfo.value.__cause__, psycopg.Error)
    assert f"{QUERY_MARKER} SELECT 1" not in connection.executed


@pytest.mark.asyncio
async def test_check_context_applies_it_once_and_rolls_back():
    connection = FakeConnection()

    await SqlDriver(conn=connection, context=CONTEXT).check_context()

    assert connection.executed == ["BEGIN TRANSACTION READ ONLY", APPLIED, f"{QUERY_MARKER} SELECT 1", "ROLLBACK"]


@pytest.mark.asyncio
async def test_check_context_without_a_context_runs_nothing():
    connection = FakeConnection()

    await SqlDriver(conn=connection).check_context()

    assert connection.executed == []


@pytest.mark.asyncio
async def test_restricted_mode_validates_the_query_and_then_applies_the_context():
    connection = FakeConnection()
    driver = SafeSqlDriver(SqlDriver(conn=connection, context=CONTEXT))

    await driver.execute_query("SELECT 1", in_context=True)

    assert connection.executed == ["BEGIN TRANSACTION READ ONLY", APPLIED, f"{QUERY_MARKER} SELECT 1", "ROLLBACK"]


@pytest.mark.asyncio
async def test_restricted_mode_still_refuses_a_write_in_context():
    connection = FakeConnection()
    driver = SafeSqlDriver(SqlDriver(conn=connection, context=CONTEXT))

    with pytest.raises(QueryError, match="Restricted mode refused"):
        await driver.execute_query("DELETE FROM orders", in_context=True)

    assert connection.executed == []


@pytest.mark.asyncio
async def test_restricted_mode_checks_the_context_it_wraps():
    connection = FakeConnection()

    await SafeSqlDriver(SqlDriver(conn=connection, context=CONTEXT)).check_context()

    assert APPLIED in connection.executed


# -- The pool's timeout and search path ------------------------------------

POOL_SETUP = f"""{QUERY_MARKER} SELECT set_config('statement_timeout', '5s', true), set_config('search_path', '"reporting"', true)"""


def pooled(connection: FakeConnection, **settings: Any) -> SqlDriver:
    """A driver on a pool whose every connection is the fake one."""
    pool = DbConnPool("postgresql://localhost/db", **settings)

    class Borrowed:
        @asynccontextmanager
        async def connection(self):
            yield connection

    async def pool_connect(connection_url=None):
        return Borrowed()

    pool.pool_connect = pool_connect  # type: ignore[method-assign]
    return SqlDriver(conn=pool, context=CONTEXT)


@pytest.mark.asyncio
async def test_the_pools_settings_are_set_in_every_transaction():
    connection = FakeConnection()

    await pooled(connection, statement_timeout=5, search_path="reporting").execute_query("SELECT 1")

    assert connection.executed == [POOL_SETUP, f"{QUERY_MARKER} SELECT 1", "COMMIT"]


@pytest.mark.asyncio
async def test_the_pools_settings_and_the_context_share_one_statement():
    connection = FakeConnection()

    await pooled(connection, statement_timeout=5, search_path="reporting").execute_query("SELECT 1", force_readonly=True, in_context=True)

    setup = POOL_SETUP + APPLIED.replace(f"{QUERY_MARKER} SELECT", ",")
    assert connection.executed == ["BEGIN TRANSACTION READ ONLY", setup, f"{QUERY_MARKER} SELECT 1", "ROLLBACK"]


@pytest.mark.asyncio
async def test_a_rolled_back_statement_gets_the_pools_settings_too():
    connection = FakeConnection()

    await pooled(connection, statement_timeout=5, search_path="reporting").execute_query_and_rollback("EXPLAIN ANALYZE DELETE FROM t")

    assert connection.executed == [POOL_SETUP, f"{QUERY_MARKER} EXPLAIN ANALYZE DELETE FROM t"]


@pytest.mark.asyncio
async def test_a_pool_without_settings_sets_nothing():
    connection = FakeConnection()

    await pooled(connection).execute_query("SELECT 1")

    assert connection.executed == [f"{QUERY_MARKER} SELECT 1", "COMMIT"]


# -- A cleanup after the statement ------------------------------------------

SAVEPOINT = f"{QUERY_MARKER} SAVEPOINT pg_debug_cleanup"
RESET = f"{QUERY_MARKER} SELECT hypopg_reset()"


@pytest.mark.asyncio
async def test_the_cleanup_runs_after_the_statement_in_its_transaction():
    connection = FakeConnection()

    rows = await SqlDriver(conn=connection).execute_query("EXPLAIN SELECT 1", cleanup="SELECT hypopg_reset()")

    assert connection.executed == [SAVEPOINT, f"{QUERY_MARKER} EXPLAIN SELECT 1", RESET, "COMMIT"]
    assert rows is not None and rows[0].cells == {"x": 1}


@pytest.mark.asyncio
async def test_the_cleanup_runs_when_the_statement_fails_and_the_error_is_kept():
    """The failure aborts the transaction; back at the savepoint the cleanup can still run in it."""
    connection = FakeConnection(fail_on="no_such_column")

    with pytest.raises(QueryError, match="no_such_column"):
        await SqlDriver(conn=connection).execute_query("EXPLAIN SELECT no_such_column", cleanup="SELECT hypopg_reset()")

    assert connection.executed == [
        SAVEPOINT,
        f"{QUERY_MARKER} EXPLAIN SELECT no_such_column",
        f"{QUERY_MARKER} ROLLBACK TO SAVEPOINT pg_debug_cleanup",
        RESET,
    ]


@pytest.mark.asyncio
async def test_the_cleanup_runs_when_the_statement_is_cancelled():
    """Restricted mode's own timeout, and Ctrl-C, arrive as a cancellation, not as a psycopg.Error."""
    connection = FakeConnection(fail_on="pg_sleep", cancel=True)

    with pytest.raises(asyncio.CancelledError):
        await SqlDriver(conn=connection).execute_query("SELECT pg_sleep(60)", cleanup="SELECT hypopg_reset()")

    assert connection.executed[-2:] == [f"{QUERY_MARKER} ROLLBACK TO SAVEPOINT pg_debug_cleanup", RESET]
    assert connection.rollbacks == 1


@pytest.mark.asyncio
async def test_a_failure_on_a_callers_connection_leaves_nothing_of_pg_debugs_transaction():
    """psycopg began the transaction, and the context switched its role; the
    caller's next statement must not run in it, as that role."""
    connection = FakeConnection(fail_on="no_such_column")

    with pytest.raises(QueryError):
        await SqlDriver(conn=connection, context=CONTEXT).execute_query(
            "EXPLAIN SELECT no_such_column", in_context=True, cleanup="SELECT hypopg_reset()"
        )

    assert connection.rollbacks == 1


@pytest.mark.asyncio
async def test_an_autocommit_connection_gets_a_transaction_for_the_cleanup():
    connection = FakeConnection(autocommit=True)

    await SqlDriver(conn=connection).execute_query("EXPLAIN SELECT 1", cleanup="SELECT hypopg_reset()")

    assert connection.executed == ["BEGIN", SAVEPOINT, f"{QUERY_MARKER} EXPLAIN SELECT 1", RESET, "COMMIT"]


@pytest.mark.asyncio
async def test_restricted_mode_validates_the_cleanup():
    connection = FakeConnection()
    driver = SafeSqlDriver(SqlDriver(conn=connection))

    with pytest.raises(QueryError, match="Restricted mode refused"):
        await driver.execute_query("SELECT 1", cleanup="DROP TABLE orders")

    assert connection.executed == []


# -- Another context --------------------------------------------------------


def test_with_context_keeps_the_connection_and_changes_only_the_context():
    connection = FakeConnection()
    driver = SqlDriver(conn=connection, context=CONTEXT)
    forced = CONTEXT.with_overrides(settings={"enable_seqscan": "off"})

    other = driver.with_context(forced)

    assert other is not driver and other.conn is connection
    assert other.context == forced and driver.context == CONTEXT


@pytest.mark.asyncio
async def test_with_context_does_not_keep_a_sessions_pin():
    """A pinned connection goes back to the pool when the session ends; a copy that kept it
    would run its statements on a connection another task has borrowed since."""
    connection = FakeConnection()
    driver = pooled(connection)

    async with driver.session():
        other = driver.with_context(None)

    assert other._pinned_connection is None
    await other.execute_query("SELECT 1")


def test_with_context_shares_the_pool_of_a_driver_not_yet_connected():
    driver = SqlDriver(engine_url="postgresql://localhost/db")

    other = driver.with_context(CONTEXT)

    assert other.conn is driver.conn and isinstance(driver.conn, DbConnPool)


def test_restricted_mode_keeps_its_restrictions_with_another_context():
    driver = SafeSqlDriver(SqlDriver(conn=FakeConnection(), context=CONTEXT), timeout=3)

    other = driver.with_context(None)

    assert isinstance(other, SafeSqlDriver)
    assert other.timeout == 3 and other.context is None
    assert driver.context == CONTEXT

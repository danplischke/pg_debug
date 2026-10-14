"""How the driver applies a query context: inside the statement's own transaction, and only when asked."""

from contextlib import asynccontextmanager
from typing import Any

import psycopg
import pytest

from pg_debug import QueryContext
from pg_debug.db import QUERY_MARKER
from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.db.driver import build_context_statement
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

    def nextset(self) -> None:
        return None

    async def fetchall(self) -> list[dict[str, Any]]:
        return [{"x": 1}]


class FakeConnection:
    """A direct connection that records the statements run on it."""

    def __init__(self, autocommit: bool = False, refuse: bool = False):
        self.autocommit = autocommit
        self.refuse = refuse
        self.executed: list[str] = []
        self.rollbacks = 0

    @asynccontextmanager
    async def cursor(self, row_factory: Any = None):
        yield FakeCursor(self)

    async def rollback(self) -> None:
        self.rollbacks += 1


def test_an_empty_context_applies_nothing():
    assert build_context_statement(None) is None
    assert build_context_statement(QueryContext()) is None


def test_settings_are_applied_in_order_and_the_role_last():
    """The role last, so a setting only the connecting user may change is set before the switch."""
    statement = build_context_statement(CONTEXT)

    assert statement is not None
    assert statement.as_string(None) == APPLIED


def test_names_and_values_are_quoted_as_literals():
    statement = build_context_statement(QueryContext(role='x"; DROP ROLE y; --', settings={"app.name": "it's"}))

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

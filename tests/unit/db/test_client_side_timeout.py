"""A timeout kept on this side, for a server that refuses statement_timeout.

Aurora DSQL refuses it at any scope, by its documentation. A pool that keeps
its timeout here leaves it out of every transaction's setup, and bounds the
wait for each statement instead.
"""

import asyncio
from unittest.mock import patch

import pytest

from pg_debug.db import QUERY_MARKER
from pg_debug.db.driver import DbConnPool
from pg_debug.db.driver import SqlDriver
from pg_debug.errors import QueryError


async def never_finishes(*args, **kwargs):
    await asyncio.sleep(60)


def test_a_timeout_kept_here_is_left_out_of_the_setup():
    pool = DbConnPool("postgresql://x/db", search_path="reporting", statement_timeout=5, server_side_timeout=False)

    setup = SqlDriver(conn=pool)._transaction_setup(in_context=False)

    assert setup is not None
    assert setup.as_string(None) == f"""{QUERY_MARKER} SELECT set_config('search_path', '"reporting"', true)"""


def test_by_default_the_timeout_is_the_servers():
    pool = DbConnPool("postgresql://x/db", statement_timeout=5)

    setup = SqlDriver(conn=pool)._transaction_setup(in_context=False)

    assert pool.server_side_timeout is True
    assert setup is not None
    assert "statement_timeout" in setup.as_string(None)


@pytest.mark.asyncio
async def test_a_timeout_kept_here_bounds_each_statement_and_each_rolled_back_one():
    driver = SqlDriver(conn=DbConnPool("postgresql://x/db", statement_timeout=1, server_side_timeout=False))

    with (
        patch.object(SqlDriver, "_execute_query", never_finishes),
        patch.object(SqlDriver, "_execute_query_and_rollback", never_finishes),
    ):
        results = await asyncio.wait_for(
            asyncio.gather(
                driver.execute_query("SELECT 1"),
                driver.execute_query_and_rollback("SELECT 1"),
                return_exceptions=True,
            ),
            timeout=10,
        )

    for result in results:
        assert isinstance(result, QueryError)
        assert "did not finish within the 1-second timeout" in str(result)
        assert "does not take statement_timeout" in str(result)


@pytest.mark.asyncio
async def test_a_timeout_on_the_server_is_not_kept_here_as_well():
    driver = SqlDriver(conn=DbConnPool("postgresql://x/db", statement_timeout=1))

    async def answered(*args, **kwargs):
        return [SqlDriver.RowResult(cells={"answer": 1})]

    with patch.object(SqlDriver, "_execute_query", answered), patch("pg_debug.db.driver.asyncio.timeout") as timeout:
        assert await driver.execute_query("SELECT 1") == [SqlDriver.RowResult(cells={"answer": 1})]

    timeout.assert_not_called()


@pytest.mark.asyncio
async def test_no_timeout_at_all_is_not_a_zero_second_one():
    driver = SqlDriver(conn=DbConnPool("postgresql://x/db", statement_timeout=0, server_side_timeout=False))

    async def answered(*args, **kwargs):
        return None

    with patch.object(SqlDriver, "_execute_query", answered), patch("pg_debug.db.driver.asyncio.timeout") as timeout:
        assert await driver.execute_query("SELECT 1") is None

    timeout.assert_not_called()

"""Activity lists culprits before victims: root blockers first, by how many wait behind them."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.activity import ActivityInspector
from pg_debug.activity import culprits_first
from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver


def backend(pid, blocked_by=(), blocking_total=0):
    return {"pid": pid, "blocked_by": list(blocked_by), "blocking_total": blocking_total}


def test_root_blockers_come_first_by_how_many_they_block():
    rows = [
        backend(1),
        backend(2, blocked_by=[5]),
        backend(3, blocking_total=1),
        backend(4, blocked_by=[5], blocking_total=1),
        backend(5, blocking_total=3),
    ]

    assert [row["pid"] for row in culprits_first(rows)] == [5, 3, 4, 2, 1]


def test_without_blocking_the_order_the_rows_came_in_is_kept():
    rows = [backend(9), backend(3), backend(7)]

    assert [row["pid"] for row in culprits_first(rows)] == [9, 3, 7]


@pytest.mark.asyncio
async def test_the_transitive_count_comes_from_the_server_and_runs_in_restricted_mode():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=backend(1)), SqlDriver.RowResult(cells=backend(2, blocking_total=2))])
    with patch("pg_debug.activity.get_postgres_version", AsyncMock(return_value=16)):
        rows = await ActivityInspector(driver).list_backends()

    assert [row["pid"] for row in rows] == [2, 1]
    query = driver.execute_query.call_args[0][0]
    assert "blocking_total" in query
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(query)

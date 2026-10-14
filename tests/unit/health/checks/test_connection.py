"""Connection health: the count judged against the server's own limit, and idle in transaction by how long."""

from typing import Any

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.health.checks.connection import ConnectionHealthCalc


class FakeDriver:
    """Answers the check's one query with what the server would say."""

    def __init__(self, **row: Any):
        self.row = {
            "client_connections": 5,
            "hidden_backends": 0,
            "idle_in_transaction": 0,
            "long_idle_in_transaction": 0,
            "longest_idle_in_transaction_seconds": None,
            "max_connections": 100,
            "superuser_reserved_connections": 3,
            "reserved_connections": 0,
            **row,
        }
        self.queries: list[str] = []

    async def execute_query(self, query: str, *args: Any, **kwargs: Any) -> list[SqlDriver.RowResult]:
        self.queries.append(query)
        return [SqlDriver.RowResult(cells=self.row)]


async def check(driver: Any, **thresholds: Any):
    return await ConnectionHealthCalc(driver, **thresholds).connection_health_check()


@pytest.mark.asyncio
async def test_a_small_server_is_flagged_as_it_runs_out_of_slots():
    """Supabase's smaller computes run max_connections 60: 50 connections is
    nearly all of it, and was reported healthy against a fixed 500."""
    result = await check(FakeDriver(client_connections=50, max_connections=60))

    assert result.healthy is False
    assert result.summary.startswith("High number of connections: 50 client connections use 88% of the 57 slots ordinary roles can use")
    assert "max_connections 60, less 3 superuser_reserved_connections" in result.summary
    assert result.metrics["available_connections"] == 57
    assert result.metrics["in_use_percent"] == pytest.approx(87.7)
    assert result.metrics["max_in_use_percent"] == 80.0


@pytest.mark.asyncio
async def test_a_large_server_is_not_flagged_for_a_count_that_is_small_for_it():
    """Neon's largest computes run max_connections 4000: 600 connections is a
    sixth of it, and was reported as too many against a fixed 500."""
    result = await check(FakeDriver(client_connections=600, max_connections=4000))

    assert result.healthy is True
    assert result.summary.startswith("Connections healthy: 600 client connections of the 3,997 slots ordinary roles can use")
    assert result.metrics["in_use_percent"] == pytest.approx(15.0)


@pytest.mark.asyncio
async def test_slots_reserved_for_privileged_roles_are_not_counted_as_available():
    """From PostgreSQL 16, reserved_connections keeps slots back for roles
    granted pg_use_reserved_connections; an ordinary role cannot have them."""
    reserved = await check(FakeDriver(client_connections=70, reserved_connections=10))
    before_16 = await check(FakeDriver(client_connections=70, reserved_connections=None))

    assert reserved.healthy is False
    assert reserved.metrics["available_connections"] == 87
    assert "less 10 reserved_connections" in reserved.summary
    assert before_16.healthy is True
    assert before_16.metrics["available_connections"] == 97
    assert before_16.metrics["reserved_connections"] is None


@pytest.mark.asyncio
async def test_many_short_waits_idle_in_transaction_are_not_a_problem():
    """A busy application shows sessions idle in transaction between its
    statements at any instant; a count of them says nothing about harm."""
    result = await check(FakeDriver(client_connections=300, max_connections=1000, idle_in_transaction=200, longest_idle_in_transaction_seconds=0))

    assert result.healthy is True
    assert result.metrics["idle_in_transaction"] == 200
    assert result.metrics["long_idle_in_transaction"] == 0


@pytest.mark.asyncio
async def test_one_session_left_idle_in_transaction_is_flagged_with_how_long():
    result = await check(FakeDriver(idle_in_transaction=1, long_idle_in_transaction=1, longest_idle_in_transaction_seconds=300))

    assert result.healthy is False
    assert result.summary == (
        "1 connection idle in transaction for a minute or more, the longest for 5 minutes; each holds its locks and its connection until it ends."
    )
    assert result.metrics["max_idle_in_transaction_seconds"] == 60
    assert result.metrics["longest_idle_in_transaction_seconds"] == 300


@pytest.mark.asyncio
async def test_backends_hidden_from_the_role_are_said_rather_than_counted_as_nothing():
    """Without pg_read_all_stats another role's backends show no type: they
    may be client connections, and enough of them to cross the line."""
    result = await check(FakeDriver(client_connections=2, hidden_backends=90))

    assert result.healthy is True
    assert result.summary.startswith("Connection use cannot be judged: between 2 and 92 client connections of the 97 slots")
    assert "90 backends of other roles hide their type and state from this role" in result.summary
    assert result.metrics["hidden_backends"] == 90


@pytest.mark.asyncio
async def test_hidden_backends_too_few_to_matter_still_leave_a_verdict_and_a_note():
    result = await check(FakeDriver(client_connections=2, hidden_backends=3))

    assert result.healthy is True
    assert result.summary.startswith("Connections healthy: between 2 and 5 client connections of the 97 slots")
    assert "not judged for idle in transaction" in result.summary


@pytest.mark.asyncio
async def test_the_check_runs_in_restricted_mode():
    """Restricted mode validates the statement before it reaches the server;
    every function the check calls has to be on its allowlist."""
    driver = FakeDriver(client_connections=80)

    result = await check(SafeSqlDriver(driver), max_idle_in_transaction_seconds=120)  # type: ignore[arg-type]

    assert result.healthy is False
    assert result.metrics["available_connections"] == 97
    assert "make_interval(secs => 120)" in driver.queries[0]

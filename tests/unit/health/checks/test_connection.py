"""Connection health: the count judged against the server's own limit, and idle in transaction by how long."""

from typing import Any

import pytest

from pg_debug.baseline import health_keys
from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.health.checks.connection import ConnectionHealthCalc

NO_SESSION = {"pid": None, "usename": None, "datname": None, "application_name": None, "idle_in_transaction_seconds": None, "query": None}


def session(pid: int, usename: str = "app", datname: str = "shop", application_name: str = "worker", seconds: int = 300, query: str = "SELECT 1"):
    """A session idle in transaction past the limit, as the check's query returns it."""
    return {
        "pid": pid,
        "usename": usename,
        "datname": datname,
        "application_name": application_name,
        "idle_in_transaction_seconds": seconds,
        "query": query,
    }


class FakeDriver:
    """Answers the check's one query with what the server would say: the
    counts, on one row per session idle in transaction past the limit, or on
    one row of its own when there is none."""

    def __init__(self, *sessions: dict[str, Any], **row: Any):
        self.row = {
            "client_connections": 5,
            "hidden_backends": 0,
            "idle_in_transaction": len(sessions),
            "longest_idle_in_transaction_seconds": max((s["idle_in_transaction_seconds"] for s in sessions), default=None),
            "max_connections": 100,
            "superuser_reserved_connections": 3,
            "reserved_connections": 0,
            **row,
        }
        self.sessions = list(sessions) or [NO_SESSION]
        self.queries: list[str] = []

    async def execute_query(self, query: str, *args: Any, **kwargs: Any) -> list[SqlDriver.RowResult]:
        self.queries.append(query)
        return [SqlDriver.RowResult(cells={**self.row, **s}) for s in self.sessions]


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
async def test_each_session_left_idle_in_transaction_is_a_finding_with_its_evidence():
    """A reader has to be able to find the session and the code that left it
    open: the verdict alone named neither."""
    result = await check(
        FakeDriver(
            session(4242, "app", "shop", "checkout", 300, "SELECT *\n  FROM orders WHERE id = 7"),
            session(4343, "report", "shop", "", 75, ""),
        )
    )

    assert result.healthy is False
    assert result.findings == [
        {
            "kind": "idle_in_transaction",
            "pid": 4242,
            "usename": "app",
            "datname": "shop",
            "application_name": "checkout",
            "idle_in_transaction_seconds": 300,
            "query": "SELECT *\n  FROM orders WHERE id = 7",
        },
        {
            "kind": "idle_in_transaction",
            "pid": 4343,
            "usename": "report",
            "datname": "shop",
            "application_name": "",
            "idle_in_transaction_seconds": 75,
            "query": "",
        },
    ]
    assert result.summary.splitlines() == [
        "2 connections idle in transaction for a minute or more, each holding its locks and its connection until it ends:",
        "Backend 4242 (role app, database shop, application checkout) has been idle in transaction for 5 minutes; "
        "its last statement: SELECT * FROM orders WHERE id = 7",
        "Backend 4343 (role report, database shop) has been idle in transaction for a minute",
    ]
    assert result.metrics["long_idle_in_transaction"] == 2
    assert result.metrics["longest_idle_in_transaction_seconds"] == 300
    assert result.metrics["max_idle_in_transaction_seconds"] == 60


@pytest.mark.asyncio
async def test_a_last_statement_cut_short_says_so():
    result = await check(FakeDriver(session(4242, query="SELECT " + "x" * 193)))

    assert result.summary.splitlines()[1].endswith("x…")


@pytest.mark.asyncio
async def test_running_out_of_connections_is_a_finding_that_carries_the_counts():
    result = await check(FakeDriver(client_connections=80))

    assert result.findings == [
        {
            "kind": "connections_in_use",
            "client_connections": 80,
            "max_connections": 100,
            "superuser_reserved_connections": 3,
            "reserved_connections": 0,
            "available_connections": 97,
            "in_use_percent": 82.5,
            "max_in_use_percent": 80.0,
        }
    ]


@pytest.mark.asyncio
async def test_running_out_of_connections_and_an_idle_session_are_known_apart_in_a_baseline():
    """Both verdicts once shared the key {"check": "Connection health"}, so a
    forgotten psql session accepted into a baseline also accepted running out
    of connections, and the gate never failed on it again."""
    idle = await check(FakeDriver(session(4242)))
    both = await check(FakeDriver(session(4343), client_connections=80))

    assert health_keys({"Connection health": idle}) == [
        {"check": "Connection health", "kind": "idle_in_transaction", "usename": "app", "datname": "shop", "application_name": "worker"}
    ]
    assert {"check": "Connection health", "kind": "connections_in_use"} in health_keys({"Connection health": both})
    assert {"check": "Connection health", "kind": "connections_in_use"} not in health_keys({"Connection health": idle})


@pytest.mark.asyncio
async def test_the_same_job_idle_again_under_a_new_pid_is_the_same_finding():
    """A pid is gone when the session ends; what leaves transactions open is
    the job, which reconnects under a new one."""
    monday = await check(FakeDriver(session(4242, seconds=120)))
    tuesday = await check(FakeDriver(session(9191, seconds=600)))
    another_job = await check(FakeDriver(session(9192, application_name="nightly-export")))

    assert health_keys({"Connection health": monday}) == health_keys({"Connection health": tuesday})
    assert health_keys({"Connection health": monday}) != health_keys({"Connection health": another_job})


@pytest.mark.asyncio
async def test_backends_hidden_from_the_role_are_said_rather_than_counted_as_nothing():
    """Without pg_read_all_stats another role's backends show no type: they
    may be client connections, and enough of them to cross the line."""
    result = await check(FakeDriver(client_connections=2, hidden_backends=90))

    assert result.healthy is True
    assert result.summary.startswith("Connection use cannot be judged: between 2 and 92 client connections of the 97 slots")
    assert "90 backends of other roles hide their type and state from this role, so they were counted" in result.summary
    assert result.metrics["hidden_backends"] == 90


@pytest.mark.asyncio
async def test_none_idle_is_claimed_only_of_the_sessions_the_role_can_see():
    """Another role's session idle in transaction shows this role no state:
    "none idle" would be a claim about sessions it could not see."""
    hidden = await check(FakeDriver(client_connections=2, hidden_backends=3))
    seen = await check(FakeDriver(client_connections=2))

    assert hidden.healthy is True
    assert hidden.summary.startswith(
        "Connections healthy: between 2 and 5 client connections of the 97 slots ordinary roles can use "
        "(max_connections 100, less 3 superuser_reserved_connections), none of the visible ones idle in transaction for a minute or more."
    )
    assert "not judged for idle in transaction" in hidden.summary
    assert "none idle in transaction" in seen.summary


@pytest.mark.asyncio
async def test_one_hidden_backend_is_said_in_the_singular():
    result = await check(FakeDriver(client_connections=2, hidden_backends=1))

    assert result.summary.splitlines()[-1] == (
        "1 backend of another role hides its type and state from this role, so it was counted only as a possible "
        "client connection and not judged for idle in transaction; pg_read_all_stats, which pg_monitor includes, shows it."
    )


@pytest.mark.asyncio
async def test_one_session_idle_in_transaction_is_said_in_the_singular():
    result = await check(FakeDriver(session(4242)))

    assert result.summary.splitlines()[0] == (
        "1 connection idle in transaction for a minute or more, holding its locks and its connection until it ends:"
    )


@pytest.mark.asyncio
async def test_the_check_runs_in_restricted_mode():
    """Restricted mode validates the statement before it reaches the server;
    every function the check calls has to be on its allowlist."""
    driver = FakeDriver(session(4242), client_connections=80)

    result = await check(SafeSqlDriver(driver), max_idle_in_transaction_seconds=120)  # type: ignore[arg-type]

    assert result.healthy is False
    assert [finding["kind"] for finding in result.findings] == ["connections_in_use", "idle_in_transaction"]
    assert result.metrics["available_connections"] == 97
    assert "make_interval(secs => 120)" in driver.queries[0]
    assert "left(query, 200)" in driver.queries[0]

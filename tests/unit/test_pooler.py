"""PgBouncer's admin console: what it shows, and the saturation read from it."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.errors import ConnectionFailedError
from pg_debug.errors import InvalidArgumentError
from pg_debug.errors import QueryError
from pg_debug.pooler import SHOW_COMMANDS
from pg_debug.pooler import _show
from pg_debug.pooler import assess
from pg_debug.pooler import check_admin_url
from pg_debug.pooler import inspect_pgbouncer
from pg_debug.pooler import read_server_connections
from pg_debug.severity import Severity


def pool(database="app", user="app", **counters):
    return {"database": database, "user": user, "cl_active": 0, "cl_waiting": 0, "sv_active": 0, "sv_idle": 0, "maxwait": 0.0, **counters}


DATABASES = [{"name": "app", "pool_size": 20, "reserve_pool": 0, "max_connections": 0}]
CONFIG = {"pool_mode": "transaction", "default_pool_size": 20, "max_client_conn": 1000}


# -- The URL ------------------------------------------------------------------


def test_the_admin_console_is_the_pgbouncer_database():
    check_admin_url("postgresql://admin@pooler:6432/pgbouncer")


@pytest.mark.parametrize("url", ["postgresql://app@pooler:6432/app", "postgresql://app@pooler:6432"])
def test_a_url_to_another_database_is_refused_with_the_one_to_use(url):
    with pytest.raises(InvalidArgumentError, match="admin console is the database named pgbouncer"):
        check_admin_url(url)


# -- Only SHOW ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["RELOAD", "PAUSE app", "SHOW POOLS; KILL app", "SHUTDOWN", "SET default_pool_size = 1"])
async def test_nothing_but_the_fixed_show_commands_is_sent(command):
    """The console accepts PAUSE, KILL, SHUTDOWN and SET from an admin user; pg-debug never sends them."""
    connection = MagicMock()
    connection.execute = AsyncMock()

    with pytest.raises(QueryError, match="refused"):
        await _show(connection, command)  # type: ignore[arg-type]

    connection.execute.assert_not_awaited()


def test_every_command_sent_is_a_show():
    assert all(command.startswith("SHOW ") for command in SHOW_COMMANDS)


@pytest.mark.asyncio
async def test_a_console_that_cannot_be_reached_is_a_connection_error_without_the_password():
    with pytest.raises(ConnectionFailedError) as error:
        await inspect_pgbouncer("postgresql://admin:s3cret@127.0.0.1:1/pgbouncer?connect_timeout=1")

    assert "s3cret" not in str(error.value)


# -- Saturation ---------------------------------------------------------------


def test_no_finding_when_no_client_waits():
    pools = [pool(cl_active=5, sv_active=5, sv_idle=15)]

    assert assess(pools, [], DATABASES, CONFIG, None) == []


def test_clients_waiting_now_is_a_finding_with_the_pool_counters():
    pools = [pool(cl_active=20, cl_waiting=7, sv_active=20, maxwait=0.25)]

    (finding,) = assess(pools, [], DATABASES, CONFIG, None)

    assert finding.kind == "clients_waiting"
    assert finding.severity is Severity.MEDIUM
    assert finding.subject == "app/app"
    assert finding.evidence["cl_waiting"] == 7
    assert finding.evidence["pool_size"] == 20
    assert "7 clients are waiting" in finding.summary


def test_a_client_waiting_a_second_or_more_is_high():
    (finding,) = assess([pool(cl_waiting=1, sv_active=20, maxwait=3.5)], [], DATABASES, CONFIG, None)

    assert finding.severity is Severity.HIGH
    assert "3.500s" in finding.summary


def test_average_wait_over_the_stats_period_is_reported_from_a_millisecond():
    stats = [
        {"database": "app", "avg_wait_time": 250_000, "avg_xact_time": 4_000, "avg_query_time": 1_000},
        {"database": "quiet", "avg_wait_time": 300, "avg_xact_time": 4_000},
        {"database": "pgbouncer", "avg_wait_time": 900_000},
    ]

    (finding,) = assess([], stats, DATABASES, CONFIG, None)

    assert (finding.kind, finding.subject, finding.severity) == ("clients_waited", "app", Severity.MEDIUM)
    assert "250.0ms on average" in finding.summary


def test_pools_that_can_open_more_than_the_server_accepts():
    """Two users' pools of 20 on one database, against a server accepting 30 from ordinary roles."""
    pools = [pool(user="a", sv_active=3), pool(user="b", sv_idle=2)]
    server = {"max_connections": 33, "superuser_reserved_connections": 3, "reserved_connections": 0, "client_backends": 9}

    (finding,) = assess(pools, [], DATABASES, CONFIG, server)

    assert finding.kind == "pool_exceeds_server"
    assert finding.evidence["pool_connections_possible"] == 40
    assert finding.evidence["server_accepts"] == 30
    assert finding.evidence["server_connections_open"] == 5


def test_a_database_connection_cap_bounds_what_its_pools_can_open():
    pools = [pool(user="a"), pool(user="b")]
    databases = [{"name": "app", "pool_size": 20, "reserve_pool": 0, "max_connections": 25}]
    server = {"max_connections": 100, "superuser_reserved_connections": 3, "reserved_connections": 0}

    assert assess(pools, [], databases, CONFIG, server) == []


def test_waiting_comes_before_what_could_happen():
    pools = [pool(user="a", cl_waiting=2, maxwait=2.0), pool(user="b")]
    server = {"max_connections": 20, "superuser_reserved_connections": 3, "reserved_connections": 0}

    findings = assess(pools, [], DATABASES, CONFIG, server)

    assert [f.kind for f in findings] == ["clients_waiting", "pool_exceeds_server"]


# -- The report -----------------------------------------------------------------


CONSOLE = {
    "SHOW VERSION": [{"version": "PgBouncer 1.22.0"}],
    "SHOW POOLS": [
        {"database": "pgbouncer", "user": "pgbouncer", "cl_active": 1, "cl_waiting": 0, "maxwait": 0, "maxwait_us": 0},
        {"database": "app", "user": "app", "cl_active": 3, "cl_waiting": 1, "sv_active": 3, "maxwait": 2, "maxwait_us": 500_000},
    ],
    "SHOW STATS": [{"database": "app", "avg_wait_time": 0}, {"database": "pgbouncer", "avg_wait_time": 0}],
    "SHOW DATABASES": [{"name": "app", "pool_size": 3, "reserve_pool": 0, "max_connections": 0}, {"name": "pgbouncer", "pool_size": 2}],
    "SHOW CONFIG": [{"key": "pool_mode", "value": "transaction"}, {"key": "default_pool_size", "value": "3"}, {"key": "logfile", "value": "/x"}],
}


@pytest.mark.asyncio
async def test_the_report_leaves_out_the_console_and_folds_maxwait():
    with patch("pg_debug.pooler.read_admin_console", AsyncMock(return_value=(CONSOLE, {}))):
        report = await inspect_pgbouncer("postgresql://admin@pooler:6432/pgbouncer", server_refused="no database URL")

    assert report.version == "PgBouncer 1.22.0"
    assert [p["database"] for p in report.pools] == ["app"]
    assert report.pools[0]["maxwait"] == 2.5
    assert "maxwait_us" not in report.pools[0]
    assert report.config == {"pool_mode": "transaction", "default_pool_size": 3}
    assert [d["name"] for d in report.databases] == ["app"]
    assert report.server is None and report.server_refused == "no database URL"
    assert [f.kind for f in report.findings] == ["clients_waiting"]
    assert report.to_dict()["findings"][0]["severity"] == "high"


@pytest.mark.asyncio
async def test_the_server_is_read_after_the_console():
    """Through a full pool the read waits in line; the console is read before pg-debug joins it."""
    order: list[str] = []

    async def console(url):
        order.append("console")
        return CONSOLE, {}

    async def server():
        order.append("server")
        raise ConnectionFailedError("query_wait_timeout")

    with patch("pg_debug.pooler.read_admin_console", console):
        report = await inspect_pgbouncer("postgresql://admin@pooler:6432/pgbouncer", read_server=server)

    assert order == ["console", "server"]
    assert report.server is None
    assert report.server_refused is not None and "query_wait_timeout" in report.server_refused


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [15, 16])
async def test_the_server_read_runs_in_restricted_mode(version):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells={"max_connections": 100})])
    with patch("pg_debug.pooler.get_postgres_version", AsyncMock(return_value=version)):
        await read_server_connections(driver)

    query = driver.execute_query.call_args[0][0]
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(query)
    assert ("current_setting('reserved_connections')" in query) == (version >= 16)

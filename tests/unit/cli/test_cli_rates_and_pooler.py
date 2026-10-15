"""The server-rates and pooler commands, and the change column of a top-queries sample."""

import contextlib
import io
import json
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from pg_debug import cli
from pg_debug.pooler import PoolerFinding
from pg_debug.pooler import PoolerReport
from pg_debug.server_rates import CounterRate
from pg_debug.server_rates import RateGroup
from pg_debug.server_rates import ServerRates
from pg_debug.severity import Severity
from pg_debug.top_queries import QuerySample


@pytest.fixture
def pg_client():
    with patch("pg_debug.cli.PgDebug") as client_cls:
        client = client_cls.return_value
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        yield client


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


RATES = ServerRates(
    interval_seconds=10,
    server_version=16,
    groups=[
        RateGroup(
            "database",
            "pg_stat_database",
            "app",
            10.0,
            counters=[
                CounterRate("xact_commit", 100, 600, 500, 50.0),
                CounterRate("temp_bytes", 0, 10 * 1024 * 1024, 10 * 1024 * 1024, 1024 * 1024),
                CounterRate("deadlocks", 9, 2, None, None, refused="deadlocks went backwards, from 9 to 2"),
            ],
        ),
        RateGroup("wal", "pg_stat_wal", None, None, refused="pg_stat_wal (wal) was reset during the interval"),
        RateGroup(
            "replication_slot",
            "pg_replication_slots",
            "standby",
            10.0,
            counters=[CounterRate("retained_bytes", 0, 2048, 2048, 204.8)],
            details={"slot_type": "physical", "active": False, "wal_status": "reserved"},
        ),
    ],
    idle_io=4,
)


def test_server_rates_shows_rates_and_refusals(pg_client):
    pg_client.sample_server_rates = AsyncMock(return_value=RATES)

    code, out, _ = run(["server-rates", "-i", "10"])

    assert code == 0
    pg_client.sample_server_rates.assert_awaited_once_with(10.0)
    assert "database app (pg_stat_database, 10.0s)" in out
    assert "xact_commit  50/s  (+500)" in out
    assert "temp_bytes   1.0 MiB/s  (+10.0 MiB)" in out
    assert "refused: deadlocks went backwards, from 9 to 2" in out
    assert "refused: pg_stat_wal (wal) was reset during the interval" in out
    assert "retaining 2.0 KiB" in out
    assert "active False" in out
    assert "4 pg_stat_io rows did no I/O" in out


def test_server_rates_json_is_the_groups(pg_client):
    pg_client.sample_server_rates = AsyncMock(return_value=RATES)

    code, out, _ = run(["--json", "server-rates"])

    payload = json.loads(out)
    assert code == 0
    assert payload["groups"][0]["counters"][0]["per_second"] == 50.0
    assert payload["groups"][1]["refused"].startswith("pg_stat_wal")
    pg_client.sample_server_rates.assert_awaited_once_with(10.0)


REPORT = PoolerReport(
    version="PgBouncer 1.22.0",
    pools=[
        {"database": "app", "user": "app", "cl_active": 3, "cl_waiting": 2, "sv_active": 3, "sv_idle": 0, "maxwait": 1.5, "pool_mode": "transaction"}
    ],
    stats=[],
    databases=[],
    config={"pool_mode": "transaction", "default_pool_size": 3},
    server=None,
    findings=[PoolerFinding("clients_waiting", Severity.HIGH, "app/app", "2 clients are waiting for a server connection in pool app/app.")],
    server_refused="no database URL was given, so the pool sizes are not compared with the server's max_connections",
)


def test_pooler_shows_pools_findings_and_what_it_did_not_compare(pg_client):
    pg_client.inspect_pooler = AsyncMock(return_value=REPORT)

    code, out, err = run(["pooler", "postgresql://admin@pooler:6432/pgbouncer"])

    assert code == 0
    pg_client.inspect_pooler.assert_awaited_once_with("postgresql://admin@pooler:6432/pgbouncer")
    assert "PgBouncer 1.22.0: pool_mode transaction, default_pool_size 3" in out
    assert "app/app" in out and "1.5s" in out
    assert "[high] 2 clients are waiting" in out
    assert "not compared with the server's max_connections" in err


def test_pooler_says_when_nothing_is_saturated(pg_client):
    quiet = PoolerReport("PgBouncer 1.22.0", [], [], [], {}, {"max_connections": 100}, [])
    pg_client.inspect_pooler = AsyncMock(return_value=quiet)

    _, out, err = run(["pooler", "postgresql://admin@pooler:6432/pgbouncer"])

    assert "No saturation: 0 pool(s) examined, 0 client(s) waiting." in out
    assert err == ""


def test_pooler_json(pg_client):
    pg_client.inspect_pooler = AsyncMock(return_value=REPORT)

    _, out, _ = run(["--json", "pooler", "postgresql://admin@pooler:6432/pgbouncer"])

    assert json.loads(out)["findings"][0]["severity"] == "high"


def test_a_sample_says_what_changed_in_a_word_and_why_it_could_not_judge(pg_client):
    queries = [
        {"queryid": 1, "query": "SELECT 1", "calls": 3, "change": {"kind": "slower", "ratio": 4.2}},
        {"queryid": 2, "query": "SELECT 2", "calls": 3, "change": {"kind": "faster", "ratio": 0.25}},
        {"queryid": 3, "query": "SELECT 3", "calls": 3, "change": {"kind": "new"}},
        {"queryid": 4, "query": "SELECT 4", "calls": 3, "change": None},
    ]
    pg_client.sample_top_queries = AsyncMock(return_value=QuerySample(queries=queries, changes_refused="Not judged: too short."))

    code, out, err = run(["top-queries", "--interval", "2"])

    assert code == 0
    assert "slower 4.2x" in out and "faster 4.0x" in out and " new" in out
    assert err == "pg-debug: Not judged: too short.\n"

    _, out, _ = run(["--json", "top-queries", "--interval", "2"])
    assert json.loads(out)[0]["change"] == {"kind": "slower", "ratio": 4.2}

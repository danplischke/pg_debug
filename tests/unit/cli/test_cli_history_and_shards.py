"""The history and shard-readiness commands: what they print, and what --json prints."""

import contextlib
import io
import json
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from pg_debug import cli
from pg_debug.history import UNTRIED
from pg_debug.history import HistoryReport
from pg_debug.history import HistorySource
from pg_debug.severity import Confidence
from pg_debug.severity import Severity
from pg_debug.sharding import CitusFinding
from pg_debug.sharding import Refusal
from pg_debug.sharding import RefusalReason
from pg_debug.sharding import ShardReadiness
from pg_debug.sharding import Verdict


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


def readiness() -> ShardReadiness:
    return ShardReadiness(
        verdict=Verdict.NOT_YET,
        summary="Not yet: 1 reason not to shard, each with its figure below.",
        refusals=[
            Refusal(
                reason=RefusalReason.READ_LOAD,
                summary="Reads take 92% of the recorded statement time: the load is reads.",
                instead="Add read replicas and send reads to them; sharding splits writes.",
                changes_if="Writes taking more than 20% of the recorded time (now 8%).",
                figures={"read_share": 0.92},
            )
        ],
        routing_keys=[{"column": "tenant_id", "share": 0.7, "tables": ["orders"]}],
        measurements={
            "interval_seconds": 10.0,
            "wal": {"source": "pg_stat_wal", "bytes_in_interval": 2048},
            "io": {"source": "pg_stat_io", "evictions_in_interval": 3, "reads_in_interval": 9},
            "checkpoints": {"timed": 90, "requested": 2, "counting_since": "2026-10-11T00:00:00+00:00"},
            "autovacuum": {"keeping_up": True, "tables_behind": 0},
            "workload": {"statements_read": 40, "read_share": 0.92, "top_share": 0.3, "top_statements": 5},
        },
        citus={
            "version": "13.0",
            "findings": [CitusFinding("multi_shard_calls", Severity.HIGH, Confidence.CANDIDATE, "70% of calls carry no key.", verify="SELECT 1")],
            "errors": {},
        },
    )


def test_shard_readiness_prints_each_refusal_with_its_figure_then_the_measurements(pg_client):
    pg_client.assess_shard_readiness = AsyncMock(return_value=readiness())

    code, out, _ = run(["shard-readiness", "--interval", "3"])

    assert code == 0
    pg_client.assess_shard_readiness.assert_awaited_once_with(interval_seconds=3.0)
    assert out.startswith("[not yet]")
    assert "do not shard: Reads take 92%" in out
    assert "changes if: Writes taking more than 20%" in out
    assert "Measured over 10 seconds" in out
    assert "tenant_id covers 70%" in out
    assert "[high, candidate] 70% of calls carry no key." in out


def test_shard_readiness_json_is_the_result_as_data(pg_client):
    pg_client.assess_shard_readiness = AsyncMock(return_value=readiness())

    _, out, _ = run(["--json", "shard-readiness"])

    payload = json.loads(out)
    assert payload["verdict"] == "not yet"
    assert payload["refusals"][0]["figures"] == {"read_share": 0.92}
    assert payload["citus"]["findings"][0]["confidence"] == "candidate"


def test_history_prints_each_source_with_what_it_keeps(pg_client):
    report = HistoryReport(
        platform={"platform": "azure", "signal": "the server has the setting azure.extensions", "basis": "server"},
        sources=[
            HistorySource(
                "azure_query_store",
                "Azure Query Store",
                present=False,
                capturing=False,
                retention="7 days",
                status="Capture is off: pg_qs.query_capture_mode = none.",
            ),
            HistorySource(
                "pg_wait_sampling",
                "pg_wait_sampling",
                True,
                True,
                "in memory",
                "1 kinds of wait read.",
                rows=[{"event": "DataFileRead", "samples": 4}],
            ),
        ],
    )
    pg_client.read_history = AsyncMock(return_value=report)

    code, out, _ = run(["history", "-n", "3"])

    assert code == 0
    pg_client.read_history.assert_awaited_once_with(limit=3)
    assert "[not here] Azure Query Store" in out
    assert "pg_qs.query_capture_mode = none" in out
    assert "keeps: 7 days" in out
    assert "DataFileRead" in out
    assert UNTRIED in out

"""The build gate on the command line: suppressions, --static, changed definitions, plan shapes, and logs."""

import contextlib
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from pg_debug import cli
from pg_debug.health import HealthCheckResult
from pg_debug.model import Confidence
from pg_debug.model import ModelFinding
from pg_debug.model import RuleId
from pg_debug.plans.shape import PlanShape
from pg_debug.severity import Severity

FIXTURES = Path(__file__).parents[1] / "logs" / "fixtures"


@pytest.fixture
def pg_client():
    with patch("pg_debug.cli.PgDebug") as client_cls:
        client = client_cls.return_value
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.definition_fingerprints = AsyncMock(side_effect=lambda command, keys: [None] * len(keys))
        yield client


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def naive(table="events"):
    return ModelFinding(
        rule=RuleId.NAIVE_TIMESTAMP,
        confidence=Confidence.PROVEN,
        severity=Severity.HIGH,
        schema="public",
        table=table,
        columns=["at"],
        summary="...",
        detail="...",
    )


def duplicate_index():
    return {
        "Duplicate index check": HealthCheckResult(
            name="Duplicate index check",
            healthy=False,
            summary="Duplicate indexes found:",
            findings=[{"schema": "public", "table": "orders", "index": "orders_a", "covered_by": "orders_b"}],
        )
    }


# -- Suppression ------------------------------------------------------------


def test_a_suppressed_rule_is_reported_and_does_not_fail_the_gate(pg_client):
    pg_client.review_data_model = AsyncMock(return_value=[naive()])

    assert run(["model", "--exit-code"])[0] == cli.UNHEALTHY_EXIT_CODE
    code, out, err = run(["model", "--exit-code", "--suppress", "naive_timestamp"])

    assert code == 0
    assert "suppressing naive_timestamp (from the command line)" in err
    assert "Suppressed, so not failing the gate: 1 finding(s)." in out
    assert "  naive_timestamp: 1 finding(s). --suppress on the command line" in out


def test_the_toml_suppresses_with_a_reason_json_carries(pg_client, tmp_path):
    (tmp_path / "pg-debug.toml").write_text('[suppress]\nnaive_timestamp = "UTC by convention."\n', encoding="utf-8")
    pg_client.review_data_model = AsyncMock(return_value=[naive()])

    code, out, err = run(["--json", "model", "--exit-code"])

    assert code == 0
    assert f"suppressing naive_timestamp (from {tmp_path / 'pg-debug.toml'})" in err
    [finding] = json.loads(out)
    assert finding["suppressed"] == "UTC by convention."
    assert finding["rule"] == "naive_timestamp" and finding["docs"].endswith("#naive_timestamp")


def test_no_config_ignores_the_toml(pg_client, tmp_path):
    (tmp_path / "pg-debug.toml").write_text('[suppress]\nnaive_timestamp = "UTC."\n', encoding="utf-8")
    pg_client.review_data_model = AsyncMock(return_value=[naive()])

    assert run(["model", "--exit-code", "--no-config"])[0] == cli.UNHEALTHY_EXIT_CODE


def test_a_suppression_without_a_reason_is_refused(pg_client, tmp_path):
    (tmp_path / "pg-debug.toml").write_text('[suppress]\nnaive_timestamp = ""\n', encoding="utf-8")

    code, _, err = run(["model"])

    assert code == 1 and "give the reason naive_timestamp is suppressed" in err
    pg_client.review_data_model.assert_not_called()


def test_an_unknown_rule_is_refused_before_connecting(pg_client):
    code, _, err = run(["health", "--suppress", "naive_timestamps"])

    assert code == 1 and "--suppress: no rule is called 'naive_timestamps'" in err
    pg_client.analyze_db_health.assert_not_called()


def test_a_suppressed_health_rule_does_not_fail_the_health_gate(pg_client):
    pg_client.analyze_db_health = AsyncMock(return_value=duplicate_index())

    assert run(["health", "--exit-code"])[0] == cli.UNHEALTHY_EXIT_CODE
    assert run(["health", "--exit-code", "--suppress", "duplicate_index"])[0] == 0


# -- Static -----------------------------------------------------------------


def test_static_reaches_the_library(pg_client):
    pg_client.analyze_db_health = AsyncMock(return_value={})
    pg_client.review_data_model = AsyncMock(return_value=[])

    run(["health", "--static"])
    run(["model", "--static"])

    assert pg_client.analyze_db_health.call_args.kwargs["static"] is True
    assert pg_client.review_data_model.call_args.kwargs["static"] is True


# -- Changed definitions ----------------------------------------------------


def test_a_finding_whose_index_was_redefined_fails_as_changed(pg_client, tmp_path):
    path = tmp_path / "health.json"
    pg_client.analyze_db_health = AsyncMock(return_value=duplicate_index())
    pg_client.definition_fingerprints = AsyncMock(return_value=["before"])
    run(["health", "--save-baseline", str(path)])
    assert json.loads(path.read_text())["findings"][0]["fingerprint"] == "before"

    assert run(["health", "--exit-code", "--baseline", str(path)])[0] == 0

    pg_client.definition_fingerprints = AsyncMock(return_value=["after"])
    code, out, _ = run(["health", "--exit-code", "--baseline", str(path)])
    assert code == cli.UNHEALTHY_EXIT_CODE
    assert "0 known finding(s) ignored, 0 new, 1 changed:" in out
    assert "orders_a orders_b (changed: defined otherwise than when it was accepted)" in out

    _, out, _ = run(["--json", "health", "--baseline", str(path)])
    assert json.loads(out)["Duplicate index check"]["findings"][0]["baseline"] == "changed"


def test_no_definition_is_read_without_a_baseline(pg_client):
    pg_client.analyze_db_health = AsyncMock(return_value=duplicate_index())

    run(["health"])

    pg_client.definition_fingerprints.assert_not_called()


# -- Plans ------------------------------------------------------------------


def shape(name, lines, error=None):
    from pg_debug.plans.shape import shape_fingerprint

    return PlanShape(query=f"SELECT * FROM {name}", name=name, shape=lines, fingerprint=None if error else shape_fingerprint(lines), error=error)


def test_a_changed_plan_shape_fails_and_shows_the_difference(pg_client, tmp_path):
    path = tmp_path / "plans.json"
    pg_client.plan_shapes = AsyncMock(return_value=[shape("orders", ["Index Scan on orders using orders_pkey"])])
    code, _, err = run(["plans", "--queries", "SELECT * FROM orders", "--save-baseline", str(path)])
    assert code == 0 and "Saved a baseline of 1 finding(s)" in err
    assert run(["plans", "-q", "SELECT * FROM orders", "--baseline", str(path), "--exit-code"])[0] == 0

    pg_client.plan_shapes = AsyncMock(return_value=[shape("orders", ["Seq Scan on orders"]), shape("users", ["Seq Scan on users"])])
    code, out, _ = run(["plans", "-q", "SELECT * FROM orders", "--baseline", str(path), "--exit-code"])

    assert code == cli.UNHEALTHY_EXIT_CODE
    assert "1 new, 1 changed:" in out
    assert "    -Index Scan on orders using orders_pkey" in out
    assert "    +Seq Scan on orders" in out

    _, out, _ = run(["--json", "plans", "-q", "x", "--baseline", str(path)])
    orders, users = json.loads(out)
    assert orders["baseline"] == "changed" and orders["accepted_shape"] == ["Index Scan on orders using orders_pkey"]
    assert users["baseline"] == "new"


def test_a_new_query_alone_does_not_fail(pg_client, tmp_path):
    path = tmp_path / "plans.json"
    pg_client.plan_shapes = AsyncMock(return_value=[])
    run(["plans", "-q", "x", "--save-baseline", str(path)])
    pg_client.plan_shapes = AsyncMock(return_value=[shape("users", ["Seq Scan on users"])])

    assert run(["plans", "-q", "x", "--baseline", str(path), "--exit-code"])[0] == 0


def test_plans_exit_code_needs_a_baseline(pg_client):
    code, _, err = run(["plans", "-q", "SELECT 1", "--exit-code"])

    assert code == 1 and "needs --baseline" in err
    pg_client.plan_shapes.assert_not_called()


def test_a_query_that_could_not_be_planned_saves_nothing_and_exits_1(pg_client, tmp_path):
    path = tmp_path / "plans.json"
    pg_client.plan_shapes = AsyncMock(return_value=[shape("nope", [], error='relation "nope" does not exist')])

    code, out, err = run(["plans", "-q", "SELECT * FROM nope", "--save-baseline", str(path)])

    assert code == 1
    assert not path.exists()
    assert 'could not be planned: relation "nope" does not exist' in out
    assert "1 query could not be planned: nope." in err


# -- Logs -------------------------------------------------------------------


def test_logs_need_no_database(pg_client):
    code, out, _ = run(["logs", str(FIXTURES / "pg16-user-at-db.log")])

    assert code == 0
    assert "Deadlocks (1):" in out
    assert "process 4344 waited for ShareLock on transaction 747, held by 4335" in out
    assert "1.7s acquired, process 4359 for AccessShareLock on relation 16389 of database 16388, held by 4347" in out
    assert "Checkpoints occurring too frequently: 6 warning(s), the closest 0s apart" in out
    assert "What these logs cannot show" not in out, "every kind of event is in this log"
    pg_client.__aenter__.assert_not_called()


def test_logs_as_json_and_with_a_format(pg_client):
    code, out, _ = run(["--json", "logs", "--format", "csvlog", str(FIXTURES / "pg16.csv")])

    payload = json.loads(out)
    assert code == 0 and payload["deadlocks"][0]["processes"][0]["pid"] == 4344
    assert list(payload["sources"].values()) == ["csvlog"]

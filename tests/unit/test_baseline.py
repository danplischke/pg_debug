"""Baselines hold what a finding is about, never what it measured."""

import json

import pytest

from pg_debug.baseline import Baseline
from pg_debug.baseline import config_finding_key
from pg_debug.baseline import describe_key
from pg_debug.baseline import health_finding_key
from pg_debug.baseline import health_keys
from pg_debug.baseline import model_finding_key
from pg_debug.baseline import partition
from pg_debug.configuration import ConfigFinding
from pg_debug.errors import InvalidArgumentError
from pg_debug.health import HealthCheckResult
from pg_debug.model import Confidence
from pg_debug.model import ModelFinding
from pg_debug.model import RuleId
from pg_debug.severity import Severity


def health(name, healthy, findings=()):
    return HealthCheckResult(name=name, healthy=healthy, summary="...", findings=list(findings))


def model(rule=RuleId.MISSING_PRIMARY_KEY, table="orders", severity=Severity.HIGH, columns=("id",)):
    return ModelFinding(
        rule=rule,
        confidence=Confidence.PROVEN,
        severity=severity,
        schema="public",
        table=table,
        columns=list(columns),
        summary="...",
        detail="...",
    )


def config(setting="work_mem", severity=Severity.HIGH):
    return ConfigFinding(setting=setting, current="4MB", severity=severity, summary="...", detail="...")


# -- Keys -------------------------------------------------------------------


def test_a_health_finding_is_identified_by_what_it_names_not_what_it_measured():
    key = health_finding_key("Index bloat", {"schema": "public", "table": "orders", "index": "orders_idx", "bloat_percent": 31.2, "size_bytes": 10})

    assert key == {"check": "Index bloat", "schema": "public", "table": "orders", "index": "orders_idx"}


def test_a_finding_that_names_nothing_falls_back_to_its_text_fields():
    key = health_finding_key("Replication", {"is_replica": True, "replication_lag_seconds": 12.5, "role": "standby"})

    assert key == {"check": "Replication", "role": "standby"}


def test_health_keys_cover_findings_and_checks_that_are_unhealthy_without_any():
    results = {
        "Connection health": health("Connection health", False),
        "Index bloat": health("Index bloat", False, [{"schema": "s", "table": "t", "index": "i"}]),
        "Vacuum health": health("Vacuum health", True),
    }

    assert health_keys(results) == [{"check": "Connection health"}, {"check": "Index bloat", "schema": "s", "table": "t", "index": "i"}]


def test_a_health_check_that_could_not_run_has_no_key():
    """It is unhealthy and names nothing, like a cache hit rate below its
    limit, but it found nothing: a key for it would accept whatever it finds
    once it runs."""
    results = {"Replication health": HealthCheckResult.could_not_run("Replication health", "permission denied")}

    assert health_keys(results) == []


def test_no_baseline_is_taken_of_a_health_run_in_which_a_check_could_not_run():
    results = {
        "Index bloat": health("Index bloat", False, [{"schema": "s", "table": "t", "index": "i"}]),
        "Replication health": HealthCheckResult.could_not_run("Replication health", "permission denied"),
    }

    with pytest.raises(InvalidArgumentError, match=r"1 of 2 health checks could not run \(Replication health\)"):
        Baseline.of("health", results)


def test_a_session_left_idle_in_transaction_is_known_by_its_job_not_its_pid():
    finding = {
        "kind": "idle_in_transaction",
        "pid": 4242,
        "usename": "app",
        "datname": "shop",
        "application_name": "checkout",
        "idle_in_transaction_seconds": 300,
        "query": "SELECT 1",
    }

    key = health_finding_key("Connection health", finding)

    assert key == {"check": "Connection health", "kind": "idle_in_transaction", "usename": "app", "datname": "shop", "application_name": "checkout"}
    assert describe_key(key) == "Connection health: idle_in_transaction usename=app datname=shop application_name=checkout"


def test_a_baseline_holds_each_identity_once(tmp_path):
    """Three sessions of one job left idle in transaction are one accepted finding."""
    path = tmp_path / "health.json"
    sessions = [{"kind": "idle_in_transaction", "pid": pid, "usename": "app", "datname": "shop"} for pid in (1, 2, 3)]

    Baseline.of("health", {"Connection health": health("Connection health", False, sessions)}).save(path)

    assert json.loads(path.read_text())["findings"] == [
        {"check": "Connection health", "kind": "idle_in_transaction", "usename": "app", "datname": "shop"}
    ]


def test_a_model_finding_is_the_rule_and_the_table_and_its_columns():
    assert model_finding_key(model()) == {"rule": "missing_primary_key", "schema": "public", "table": "orders", "columns": ["id"]}


def test_a_config_finding_is_the_setting():
    assert config_finding_key(config()) == {"setting": "work_mem"}


# -- Files ------------------------------------------------------------------


def test_a_baseline_round_trips_through_a_file_in_a_stable_order(tmp_path):
    results = {
        "Index bloat": health(
            "Index bloat", False, [{"schema": "s", "table": "t", "index": "zeta"}, {"schema": "s", "table": "t", "index": "alpha"}]
        ),
    }
    path = tmp_path / "health.json"

    Baseline.of("health", results).save(path)
    loaded = Baseline.load(path, "health")

    assert len(loaded) == 2
    assert {"check": "Index bloat", "schema": "s", "table": "t", "index": "alpha"} in loaded
    payload = json.loads(path.read_text())
    assert payload["version"] == 1 and payload["command"] == "health"
    assert [f["index"] for f in payload["findings"]] == ["alpha", "zeta"], "sorted, so the file diffs cleanly"
    assert path.read_text().count("\n") == len(payload["findings"]) + 7, "one finding per line, plus the seven lines of framing"


def test_an_empty_baseline_is_a_valid_file(tmp_path):
    path = tmp_path / "empty.json"

    Baseline.of("config", []).save(path)

    assert json.loads(path.read_text())["findings"] == []
    assert '"findings": []' in path.read_text()
    assert len(Baseline.load(path, "config")) == 0


def test_a_baseline_for_another_command_is_refused(tmp_path):
    path = tmp_path / "b.json"
    Baseline.of("health", {}).save(path)

    with pytest.raises(InvalidArgumentError, match="baseline for `pg-debug health`, not `pg-debug model`"):
        Baseline.load(path, "model")


def test_a_missing_baseline_says_how_to_make_one(tmp_path):
    with pytest.raises(InvalidArgumentError, match="--save-baseline"):
        Baseline.load(tmp_path / "none.json", "health")


def test_a_file_that_is_not_a_baseline_is_refused(tmp_path):
    path = tmp_path / "b.json"
    path.write_text('{"findings": "no"}')

    with pytest.raises(InvalidArgumentError, match="not a pg_debug baseline"):
        Baseline.load(path, "health")


# -- Partitions -------------------------------------------------------------


def test_every_new_health_finding_fails_the_gate():
    baseline = Baseline("health", [{"check": "Index bloat", "schema": "s", "table": "t", "index": "known"}])
    results = {
        "Index bloat": health(
            "Index bloat", False, [{"schema": "s", "table": "t", "index": "known"}, {"schema": "s", "table": "t", "index": "fresh"}]
        )
    }

    split = partition("health", results, baseline)

    assert [k["index"] for k in split.known] == ["known"]
    assert [k["index"] for k in split.new] == ["fresh"]
    assert split.new_failures == 1


def test_only_a_new_high_severity_model_finding_fails_the_gate():
    baseline = Baseline("model", [model_finding_key(model(table="accepted"))])
    findings = [model(table="accepted"), model(table="advice", severity=Severity.MEDIUM), model(table="fresh")]

    split = partition("model", findings, baseline)

    assert [k["table"] for k in split.known] == ["accepted"]
    assert [k["table"] for k in split.new] == ["advice", "fresh"]
    assert split.new_failures == 1


def test_config_partitions_by_setting():
    baseline = Baseline("config", [{"setting": "work_mem"}])

    split = partition("config", [config("work_mem"), config("shared_buffers", severity=Severity.LOW)], baseline)

    assert split.new == [{"setting": "shared_buffers"}]
    assert split.new_failures == 0


def test_a_finding_that_went_away_simply_is_not_mentioned():
    baseline = Baseline("config", [{"setting": "work_mem"}, {"setting": "max_wal_size"}])

    split = partition("config", [config("work_mem")], baseline)

    assert split.new == [] and split.known == [{"setting": "work_mem"}]


def test_keys_describe_themselves():
    assert (
        describe_key({"check": "Index bloat", "schema": "public", "table": "orders", "index": "orders_idx"})
        == "Index bloat: public.orders orders_idx"
    )
    assert describe_key({"rule": "missing_primary_key", "schema": "public", "table": "events", "columns": []}) == "missing_primary_key: public.events"
    assert describe_key({"setting": "work_mem"}) == "finding: work_mem"
    assert describe_key({"check": "Connection health"}) == "Connection health"

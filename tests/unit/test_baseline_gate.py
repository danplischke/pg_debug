"""Fingerprints and suppressions: a changed definition fails, a suppressed rule never does."""

import json

from pg_debug.baseline import Baseline
from pg_debug.baseline import model_finding_key
from pg_debug.baseline import partition
from pg_debug.configuration import ConfigFinding
from pg_debug.health import HealthCheckResult
from pg_debug.model import Confidence
from pg_debug.model import ModelFinding
from pg_debug.model import RuleId
from pg_debug.severity import Severity


def health(name, healthy, findings=()):
    return HealthCheckResult(name=name, healthy=healthy, summary="...", findings=list(findings))


def model(rule=RuleId.MISSING_PRIMARY_KEY, table="orders", severity=Severity.HIGH):
    return ModelFinding(
        rule=rule, confidence=Confidence.PROVEN, severity=severity, schema="public", table=table, columns=["id"], summary="...", detail="..."
    )


def config(setting="work_mem", rule="work_mem_spills", severity=Severity.HIGH):
    return ConfigFinding(rule=rule, setting=setting, current="4MB", severity=severity, summary="...", detail="...")


DUPLICATE = {"Duplicate index check": health("Duplicate index check", False, [{"schema": "s", "table": "t", "index": "i"}])}
DUPLICATE_KEY = {"check": "Duplicate index check", "schema": "s", "table": "t", "index": "i"}


# -- Fingerprints -----------------------------------------------------------


def test_a_fingerprint_is_not_part_of_what_a_finding_is_about():
    baseline = Baseline("health", [{**DUPLICATE_KEY, "fingerprint": "aaaa"}])

    assert DUPLICATE_KEY in baseline
    assert baseline.status({**DUPLICATE_KEY, "fingerprint": "aaaa"}) == "known"


def test_a_finding_whose_object_is_defined_otherwise_now_is_changed_and_fails():
    now = [{**DUPLICATE_KEY, "fingerprint": "after"}]

    split = partition("health", DUPLICATE, Baseline("health", [{**DUPLICATE_KEY, "fingerprint": "before"}]), keys=now)

    assert split.changed == now and split.known == [] and split.new == []
    assert split.new_failures == 1
    assert split.status(now[0]) == "changed"


def test_without_a_fingerprint_on_either_side_a_finding_stays_known():
    """A version 1 baseline has none; nothing says the object differs."""
    split = partition("health", DUPLICATE, Baseline("health", [DUPLICATE_KEY]), keys=[{**DUPLICATE_KEY, "fingerprint": "now"}])

    assert split.changed == [] and len(split.known) == 1 and split.new_failures == 0


def test_a_version_1_file_still_loads(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(
        '{\n  "version": 1,\n  "command": "config",\n  "created": "2026-01-01T00:00:00+00:00",\n  "findings": [\n    {"setting":"work_mem"}\n  ]\n}\n'
    )

    loaded = Baseline.load(path, "config")

    assert loaded.version == 1 and {"setting": "work_mem"} in loaded
    assert partition("config", [config()], loaded).known == [{"setting": "work_mem"}]


def test_a_saved_baseline_keeps_the_fingerprints(tmp_path):
    path = tmp_path / "b.json"
    key = {**model_finding_key(model()), "fingerprint": "abc"}

    Baseline.of("model", [model()], keys=[key]).save(path)

    assert json.loads(path.read_text())["findings"] == [key]
    assert json.loads(path.read_text())["version"] == 2
    assert Baseline.load(path, "model").status({**key, "fingerprint": "xyz"}) == "changed"


# -- Suppression ------------------------------------------------------------


def test_a_suppressed_rule_is_reported_but_never_fails():
    findings = [model(rule=RuleId.NAIVE_TIMESTAMP, table="events"), model(table="orders")]

    split = partition("model", findings, None, suppress={"naive_timestamp": "UTC by convention"})

    assert [k["table"] for k in split.suppressed] == ["events"]
    assert [k["table"] for k in split.new] == ["orders"]
    assert split.new_failures == 1
    assert split.suppressed_by_rule == {"naive_timestamp": 1}
    assert split.mark({}, model_finding_key(findings[0]), "naive_timestamp") == {"suppressed": "UTC by convention"}


def test_a_suppressed_rule_does_not_fail_against_a_baseline_either():
    split = partition("model", [model(rule=RuleId.NAIVE_TIMESTAMP)], Baseline("model", []), suppress={"naive_timestamp": "why"})

    assert split.new == [] and split.new_failures == 0
    assert split.mark({}, model_finding_key(model(rule=RuleId.NAIVE_TIMESTAMP)), "naive_timestamp") == {"baseline": "new", "suppressed": "why"}


def test_a_health_check_without_findings_is_suppressed_by_its_rule():
    results = {"Buffer health for tables": health("Buffer health for tables", False)}

    split = partition("health", results, None, suppress={"table_cache_hit_rate": "small server"})

    assert split.suppressed == [{"check": "Buffer health for tables"}] and split.new_failures == 0


def test_a_suppressed_config_rule_is_its_rule_not_its_setting():
    split = partition("config", [config()], None, suppress={"work_mem_spills": "batch server"})

    assert split.new_failures == 0 and split.suppressed == [{"setting": "work_mem"}]


def test_without_a_baseline_there_is_no_status_to_mark():
    split = partition("config", [config()], None)

    assert split.status({"setting": "work_mem"}) is None
    assert split.mark({}, {"setting": "work_mem"}, None) == {}
    assert split.new_failures == 1

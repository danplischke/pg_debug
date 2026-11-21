"""Plans, slow statements and connection events, read from logs PostgreSQL 16 wrote with RDS's prefix.

``rds-pg16.log`` and ``rds-pg16.csv`` are one throwaway server's output, not
edited: ``log_destination = 'stderr,csvlog'``, ``log_line_prefix =
'%t:%r:%u@%d:[%p]:'`` (RDS's default), ``auto_explain`` with
``log_format = json``, ``log_analyze`` and ``log_buffers`` on, and
``log_min_duration_statement = 50``. Against it: queries tagged the way
sqlcommenter tags them, two with a plan worth a finding (correlated columns, a
sort past ``work_mem`` under an OFFSET); wrong passwords, an unknown role,
``max_connections`` reached, an idle-in-transaction timeout,
``pg_terminate_backend``, a client gone mid-transaction, and a backend
killed with signal 9.
"""

import json
from pathlib import Path

import pytest

from pg_debug.logs import analyze_log_lines
from pg_debug.logs import analyze_logs
from pg_debug.plans import PlanIssue

FIXTURES = Path(__file__).parent / "fixtures"
BOTH = ["rds-pg16.log", "rds-pg16.csv"]


def read(name, **options):
    return analyze_logs([FIXTURES / name], **options)


def plan_for(report, text):
    [logged] = [p for p in report.plans if text in p.query]
    return logged


@pytest.mark.parametrize("name", BOTH)
def test_the_rds_prefix_is_read_without_being_given(name):
    report = read(name)
    assert report.unparsed == 0
    [denied] = [c for c in report.connections if c.kind == "too_many_clients"]
    assert denied.by == {"postgres@postgres": 1}


@pytest.mark.parametrize("name", BOTH)
def test_a_logged_plan_is_diagnosed_without_a_database(name):
    lookup = plan_for(read(name), "city = 7 AND zip = 7")

    assert lookup.plan_format == "json"
    assert lookup.analyzed
    assert lookup.not_diagnosed is None
    issues = {finding.issue for finding in lookup.findings}
    assert PlanIssue.ROW_ESTIMATE in issues
    assert lookup.callers == [{"action": "lookup", "controller": "geo"}]


@pytest.mark.parametrize("name", BOTH)
def test_a_spill_under_an_offset_is_found_in_the_slow_run(name):
    page = plan_for(read(name), "OFFSET 500000")
    summaries = " ".join(finding.summary for finding in page.findings)
    assert "spilled to disk" in summaries
    assert "OFFSET" in summaries


@pytest.mark.parametrize("name", BOTH)
def test_runs_of_one_query_are_one_entry_with_each_caller(name):
    report = read(name)
    sleeps = plan_for(report, "pg_sleep")
    assert sleeps.runs == 2
    assert sleeps.slowest_ms == pytest.approx(201.32)
    assert "pg_sleep(0.2)" in sleeps.query, "the slowest run's text"
    assert sleeps.callers == [{"action": "report", "controller": "reports"}, {"action": "export", "controller": "reports"}]

    [slow] = [s for s in report.slow_statements if "pg_sleep" in s.query]
    assert slow.runs == 2
    assert len(slow.callers) == 2


@pytest.mark.parametrize("name", BOTH)
def test_refused_and_lost_connections_are_counted_by_kind(name):
    by_kind = {events.kind: events for events in read(name).connections}

    assert by_kind["authentication_failed"].count == 3
    assert by_kind["authentication_failed"].by == {"postgres@postgres": 2, "nosuchdb@nosuchdb": 1}
    assert by_kind["unknown_role"].count == 1
    for kind in ("too_many_clients", "idle_in_transaction_timeout", "terminated_by_administrator", "client_vanished_in_transaction"):
        assert by_kind[kind].count == 1, kind
    assert by_kind["backend_crashed"].details == {"signal 9": 1}


def test_a_clean_shutdown_is_not_a_crash():
    """The launcher exits with 1 on every fast shutdown, and the fixture holds one."""
    report = read("rds-pg16.log")
    [crash] = [c for c in report.connections if c.kind == "backend_crashed"]
    assert crash.count == 1
    assert "logical replication launcher" not in crash.example


def test_the_json_output_carries_the_findings():
    data = read("rds-pg16.log").to_dict()
    json.dumps(data)
    [lookup] = [p for p in data["plans"] if "city = 7" in p["query"]]
    assert lookup["findings"][0]["severity"] == "high"
    assert {c["kind"] for c in data["connections"]} >= {"authentication_failed", "backend_crashed"}


PREFIX = "2026-11-21 10:00:00 UTC:10.0.0.5(5000):app@shop:[42]:"


def test_a_plan_logged_without_analyze_says_why_it_was_not_diagnosed():
    plan = {"Query Text": "SELECT 1", "Plan": {"Node Type": "Result", "Startup Cost": 0.0, "Total Cost": 0.01, "Plan Rows": 1, "Plan Width": 4}}
    lines = [f"{PREFIX}LOG:  duration: 120.000 ms  plan:"] + [f"\t{line}" for line in json.dumps(plan, indent=2).splitlines()]
    [logged] = analyze_log_lines(lines).plans

    assert not logged.analyzed
    assert logged.findings == []
    assert "log_analyze is off" in (logged.not_diagnosed or "")


def test_a_plan_logged_as_text_is_kept_and_not_diagnosed():
    lines = [
        f"{PREFIX}LOG:  duration: 300.000 ms  plan:",
        "\tQuery Text: SELECT * FROM t WHERE a = 1",
        "\tSeq Scan on t  (cost=0.00..35.50 rows=10 width=8) (actual time=0.010..290.000 rows=1000 loops=1)",
    ]
    [logged] = analyze_log_lines(lines).plans

    assert logged.plan_format == "text"
    assert logged.query == "SELECT * FROM t WHERE a = 1"
    assert "log_format = 'json'" in (logged.not_diagnosed or "")


def test_constants_do_not_split_a_query_and_the_slowest_run_is_kept():
    lines = [
        f"{PREFIX}LOG:  duration: 60.000 ms  statement: SELECT * FROM t WHERE a = 1",
        f"{PREFIX}LOG:  duration: 90.000 ms  execute <unnamed>: SELECT * FROM t WHERE a = 2",
        f"{PREFIX}LOG:  duration: 75.000 ms  statement: SELECT * FROM u",
    ]
    report = analyze_log_lines(lines)

    assert [(s.runs, s.query) for s in report.slow_statements] == [(2, "SELECT * FROM t WHERE a = 2"), (1, "SELECT * FROM u")]
    assert report.slow_statements[0].total_ms == 150.0


def test_rds_s_other_prefix_is_read_when_given():
    """RDS offers one alternative to its default prefix; given it, every field is read."""
    prefix = "%m:%r:%u@%d:[%p]:%l:%e:%s:%v:%x:%c:%q%a:"
    line = (
        "2026-11-21 10:00:00.123 UTC:10.0.0.5(5000):app@shop:[42]:7:28P01:2026-11-21 09:59:00 UTC:3/14:0:65a1b2c3.2a:psql:"
        'FATAL:  password authentication failed for user "app"'
    )
    [events] = analyze_log_lines([line], prefix=prefix).connections
    assert events.kind == "authentication_failed"
    assert events.by == {"app@shop": 1}
    assert events.first_time == "2026-11-21 10:00:00.123 UTC"


def test_with_nothing_logged_the_report_says_how_to_turn_auto_explain_on():
    report = analyze_log_lines([f"{PREFIX}LOG:  checkpoint starting: time"])
    assert any("auto_explain" in note and "parameter group" in note for note in report.notes)

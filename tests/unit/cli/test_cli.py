import argparse
import json
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from pg_debug import cli
from pg_debug.db.privileges import HIDDEN_QUERY_TEXT
from pg_debug.db.privileges import StatisticsAccess
from pg_debug.errors import ExtensionRequiredError
from pg_debug.errors import IndexTuningError
from pg_debug.health import HealthCheckResult
from pg_debug.model import Confidence
from pg_debug.model import ModelFinding
from pg_debug.model import RuleId
from pg_debug.model import Severity
from pg_debug.plans.tree import ExplainPlanArtifact
from pg_debug.plans.tree import PlanNode
from pg_debug.top_queries import QuerySample

#: A role shown every role's activity and statements, as a superuser is.
MONITOR = StatisticsAccess(role="monitor", reads_all_stats=True)
#: A role without pg_read_all_stats.
PLAIN = StatisticsAccess(role="app_user", reads_all_stats=False, remedy="GRANT pg_monitor TO app_user")


@pytest.fixture
def pg_client():
    """Patch PgDebug so commands run against a mock instead of a database.

    The connecting role is shown everything, unless a test says otherwise.
    """
    with patch("pg_debug.cli.PgDebug") as client_cls:
        client = client_cls.return_value
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.statistics_access = AsyncMock(return_value=MONITOR)
        client.withheld_backends = AsyncMock(return_value=[])
        client.counting_windows = AsyncMock(return_value={"tables": None, "checkpoints": None, "statements": None})
        yield client


def run(argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


# -- Argument parsing -------------------------------------------------------


def test_parse_index_spec_with_defaults():
    assert cli.parse_index_spec("users(email)") == {"table": "users", "columns": ["email"], "using": "btree"}


def test_parse_index_spec_with_multiple_columns_and_method():
    assert cli.parse_index_spec(" orders( user_id , created_at ) : hash ") == {
        "table": "orders",
        "columns": ["user_id", "created_at"],
        "using": "hash",
    }


@pytest.mark.parametrize("spec", ["users", "users()", "(email)", "users(email"])
def test_parse_index_spec_rejects_malformed_specs(spec):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_index_spec(spec)


def test_command_is_required():
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args([])
    assert excinfo.value.code == 2


def test_defaults():
    args = cli.build_parser().parse_args(["schemas"])
    assert args.access_mode == "unrestricted"
    assert args.json is False
    assert args.timeout is None
    assert args.search_path is None


# -- Commands ---------------------------------------------------------------


def test_schemas_renders_a_table(pg_client):
    pg_client.list_schemas = AsyncMock(return_value=[{"schema_name": "public", "schema_type": "User Schema"}])

    code, out, _ = run(["schemas"])

    assert code == 0
    assert "schema_name" in out
    assert "public" in out
    assert "(1 row)" in out


def test_windows_runs_on_a_loop_psycopg_can_connect_on(pg_client, monkeypatch):
    """psycopg's async connections refuse the proactor loop Windows makes by default."""
    import asyncio
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    loops = []

    async def list_schemas():
        loops.append(asyncio.get_running_loop())
        return []

    pg_client.list_schemas = list_schemas

    assert run(["schemas"])[0] == 0
    assert isinstance(loops[0], asyncio.SelectorEventLoop)


def test_json_output(pg_client):
    pg_client.list_schemas = AsyncMock(return_value=[{"schema_name": "public"}])

    code, out, _ = run(["--json", "schemas"])

    assert code == 0
    assert json.loads(out) == [{"schema_name": "public"}]


def test_objects_passes_type(pg_client):
    pg_client.list_objects = AsyncMock(return_value=[])

    assert run(["objects", "public", "--type", "view"])[0] == 0
    pg_client.list_objects.assert_awaited_once_with("public", "view")


def test_describe_splits_qualified_name(pg_client):
    pg_client.get_object_details = AsyncMock(return_value={})

    assert run(["describe", "reporting.orders"])[0] == 0
    pg_client.get_object_details.assert_awaited_once_with("reporting", "orders", "table")


def test_describe_uses_schema_option(pg_client):
    pg_client.get_object_details = AsyncMock(return_value={})

    assert run(["describe", "orders", "--schema", "reporting"])[0] == 0
    pg_client.get_object_details.assert_awaited_once_with("reporting", "orders", "table")


def test_execute_reads_stdin(pg_client, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("SELECT 1"))
    pg_client.execute_sql = AsyncMock(return_value=[{"?column?": 1}])

    assert run(["execute", "-"])[0] == 0
    pg_client.execute_sql.assert_awaited_once_with("SELECT 1")


def test_explain_renders_plan_text(pg_client):
    node = PlanNode(node_type="Seq Scan", total_cost=10.0, startup_cost=0.0, plan_rows=1, plan_width=4)
    pg_client.explain_query = AsyncMock(return_value=ExplainPlanArtifact(value='{"Plan": {}}', plan_tree=node))

    code, out, _ = run(["explain", "SELECT 1"])

    assert code == 0
    assert "Seq Scan" in out


def test_windows_writes_utf8_to_a_file_or_pipe(pg_client, monkeypatch):
    """Windows encodes redirected output in cp1252 or similar, which has no arrow."""
    import io
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="backslashreplace")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    node = PlanNode(node_type="Seq Scan", total_cost=10.0, startup_cost=0.0, plan_rows=1, plan_width=4)
    pg_client.explain_query = AsyncMock(return_value=ExplainPlanArtifact(value='{"Plan": {}}', plan_tree=node))

    assert cli.main(["explain", "SELECT 1"]) == 0

    out.flush()
    assert "→ Seq Scan" in out.buffer.getvalue().decode("utf-8")
    assert (err.encoding, err.errors) == ("utf-8", "backslashreplace"), "stderr keeps its error handler"


def test_explain_json_prints_the_plan_document(pg_client):
    node = PlanNode(node_type="Seq Scan", total_cost=10.0, startup_cost=0.0, plan_rows=1, plan_width=4)
    pg_client.explain_query = AsyncMock(return_value=ExplainPlanArtifact(value='{"Plan": {"Total Cost": 10.0}}', plan_tree=node))

    code, out, _ = run(["--json", "explain", "SELECT 1"])

    assert code == 0
    assert json.loads(out) == {"Plan": {"Total Cost": 10.0}}


def test_explain_json_falls_back_for_non_json_plans(pg_client):
    node = PlanNode(node_type="Seq Scan", total_cost=10.0, startup_cost=0.0, plan_rows=1, plan_width=4)
    pg_client.explain_query = AsyncMock(return_value=ExplainPlanArtifact(value="Seq Scan on orders", plan_tree=node))

    code, out, _ = run(["--json", "explain", "SELECT 1"])

    assert code == 0
    assert json.loads(out) == "Seq Scan on orders"


def test_explain_passes_hypothetical_indexes(pg_client):
    node = PlanNode(node_type="Index Scan", total_cost=1.0, startup_cost=0.0, plan_rows=1, plan_width=4)
    pg_client.explain_query = AsyncMock(return_value=ExplainPlanArtifact(value="{}", plan_tree=node))

    assert run(["explain", "SELECT 1", "-i", "users(email)", "-i", "orders(user_id):hash"])[0] == 0
    _, kwargs = pg_client.explain_query.call_args
    assert kwargs["hypothetical_indexes"] == [
        {"table": "users", "columns": ["email"], "using": "btree"},
        {"table": "orders", "columns": ["user_id"], "using": "hash"},
    ]


def health_results(*checks):
    """Build the mapping analyze_db_health returns."""
    return {name: HealthCheckResult(name=name, healthy=healthy, summary=summary, **extra) for name, healthy, summary, extra in checks}


def test_health_renders_one_line_per_check(pg_client):
    pg_client.analyze_db_health = AsyncMock(
        return_value=health_results(
            ("Connection health", True, "ok", {}),
            ("Vacuum health", True, "ok", {}),
        )
    )

    code, out, _ = run(["health", "--type", "connection,vacuum"])

    assert code == 0
    assert out.splitlines()[:2] == ["Connection health: ok", "Vacuum health: ok"]
    pg_client.analyze_db_health.assert_awaited_once_with("connection,vacuum", min_window_days=7.0)


def test_health_json_includes_findings_and_metrics(pg_client):
    pg_client.analyze_db_health = AsyncMock(
        return_value=health_results(
            (
                "Unused index check",
                False,
                "Rarely used indexes found:",
                {"findings": [{"index": "orders_status_idx", "index_scans": 0}], "metrics": {"max_scans": 50}},
            ),
        )
    )

    code, out, _ = run(["--json", "health", "--type", "index"])

    assert code == 0
    assert json.loads(out) == {
        "Unused index check": {
            "name": "Unused index check",
            "healthy": False,
            "summary": "Rarely used indexes found:",
            "findings": [{"index": "orders_status_idx", "index_scans": 0}],
            "metrics": {"max_scans": 50},
        }
    }


def test_health_exit_code_flags_problems(pg_client):
    pg_client.analyze_db_health = AsyncMock(
        return_value=health_results(
            ("Connection health", True, "ok", {}),
            ("Vacuum health", False, "Tables approaching transaction ID wraparound:", {}),
        )
    )

    assert run(["health", "--exit-code"])[0] == cli.UNHEALTHY_EXIT_CODE


def test_health_exit_code_is_zero_when_healthy(pg_client):
    pg_client.analyze_db_health = AsyncMock(return_value=health_results(("Connection health", True, "ok", {})))

    assert run(["health", "--exit-code"])[0] == 0


def test_health_without_exit_code_succeeds_when_a_check_finds_a_problem(pg_client):
    pg_client.analyze_db_health = AsyncMock(return_value=health_results(("Connection health", False, "too many connections", {})))

    assert run(["health"])[0] == 0


def could_not_run(name, error="canceling statement due to statement timeout"):
    return (name, False, f"Could not run: {error}", {"error": error})


def test_health_prints_every_check_and_exits_1_when_one_could_not_run(pg_client):
    pg_client.analyze_db_health = AsyncMock(
        return_value=health_results(
            could_not_run("Invalid index check"),
            ("Vacuum health", True, "ok", {}),
        )
    )

    code, out, err = run(["health"])

    assert code == 1
    assert out.splitlines() == ["Invalid index check: Could not run: canceling statement due to statement timeout", "Vacuum health: ok"]
    assert "1 of 2 health checks could not run, so this run is incomplete: Invalid index check." in err


def test_health_exit_code_is_1_when_the_only_failure_is_a_check_that_could_not_run(pg_client):
    """3 says the database has a problem; a check that did not run has not found one."""
    pg_client.analyze_db_health = AsyncMock(return_value=health_results(could_not_run("Invalid index check"), ("Vacuum health", True, "ok", {})))

    assert run(["health", "--exit-code"])[0] == 1


def test_health_exit_code_puts_a_problem_found_before_an_incomplete_run(pg_client):
    pg_client.analyze_db_health = AsyncMock(
        return_value=health_results(
            could_not_run("Invalid index check"),
            ("Vacuum health", False, "Tables approaching transaction ID wraparound:", {}),
        )
    )

    code, _, err = run(["health", "--exit-code"])

    assert code == cli.UNHEALTHY_EXIT_CODE
    assert "could not run" in err


def test_health_json_carries_the_reason_a_check_could_not_run(pg_client):
    pg_client.analyze_db_health = AsyncMock(
        return_value=health_results(could_not_run("Replication health", "permission denied for function pg_last_wal_replay_lsn"))
    )

    code, out, _ = run(["--json", "health", "--type", "replication"])

    assert code == 1
    assert json.loads(out)["Replication health"]["error"] == "permission denied for function pg_last_wal_replay_lsn"


def model_findings(*findings):
    """Build the list review_data_model returns."""
    return [
        ModelFinding(
            rule=rule,
            confidence=confidence,
            severity=severity,
            schema="public",
            table=table,
            columns=[],
            summary=f"{table} summary",
            detail="detail",
            remediation=["ALTER TABLE public.x ADD PRIMARY KEY (id);"],
        )
        for rule, confidence, severity, table in findings
    ]


def test_model_passes_its_options(pg_client):
    pg_client.review_data_model = AsyncMock(return_value=[])

    assert run(["model", "--schema", "reporting", "--rule", "naive_timestamp", "--proven-only", "--min-rows", "50"])[0] == 0
    pg_client.review_data_model.assert_awaited_once_with(
        rules="naive_timestamp",
        schema="reporting",
        min_rows=50,
        proven_only=True,
    )


def test_model_reports_a_clean_schema_plainly(pg_client):
    pg_client.review_data_model = AsyncMock(return_value=[])

    code, out, _ = run(["model"])

    assert code == 0
    assert out.strip() == "No data model findings."


def test_model_groups_findings_by_table_and_shows_the_fix(pg_client):
    pg_client.review_data_model = AsyncMock(
        return_value=model_findings(
            (RuleId.MISSING_PRIMARY_KEY, Confidence.PROVEN, Severity.HIGH, "events"),
            (RuleId.NAIVE_TIMESTAMP, Confidence.CANDIDATE, Severity.LOW, "orders"),
        )
    )

    code, out, _ = run(["model"])

    assert code == 0
    assert "public.events" in out
    assert "[high] missing_primary_key" in out
    assert "(candidate)" in out
    assert "ALTER TABLE public.x ADD PRIMARY KEY (id);" in out
    assert "2 findings (1 high, 1 low)" in out


def test_model_json_serializes_each_finding(pg_client):
    pg_client.review_data_model = AsyncMock(return_value=model_findings((RuleId.MISSING_PRIMARY_KEY, Confidence.PROVEN, Severity.HIGH, "events")))

    code, out, _ = run(["--json", "model"])

    assert code == 0
    payload = json.loads(out)
    assert payload[0]["rule"] == "missing_primary_key"
    assert payload[0]["severity"] == "high"
    assert payload[0]["remediation"] == ["ALTER TABLE public.x ADD PRIMARY KEY (id);"]


def test_model_exit_code_fires_only_on_high_severity(pg_client):
    """A gate that fires on advice is a gate somebody turns off."""
    pg_client.review_data_model = AsyncMock(return_value=model_findings((RuleId.NAIVE_TIMESTAMP, Confidence.PROVEN, Severity.MEDIUM, "orders")))
    assert run(["model", "--exit-code"])[0] == 0

    pg_client.review_data_model = AsyncMock(return_value=model_findings((RuleId.MISSING_PRIMARY_KEY, Confidence.PROVEN, Severity.HIGH, "events")))
    assert run(["model", "--exit-code"])[0] == cli.UNHEALTHY_EXIT_CODE


def test_model_without_exit_code_flag_always_succeeds(pg_client):
    pg_client.review_data_model = AsyncMock(return_value=model_findings((RuleId.MISSING_PRIMARY_KEY, Confidence.PROVEN, Severity.HIGH, "events")))

    assert run(["model"])[0] == 0


def test_config_renders_findings(pg_client):
    from pg_debug.configuration import ConfigFinding

    pg_client.review_configuration = AsyncMock(
        return_value=[
            ConfigFinding(
                setting="work_mem",
                current="4.0 MB",
                severity=Severity.MEDIUM,
                summary="Queries have spilled to disk.",
                detail="detail",
                recommended="8.0 MB",
            )
        ]
    )

    code, out, _ = run(["config"])

    assert code == 0
    assert "[medium] work_mem = 4.0 MB" in out
    assert "suggested: 8.0 MB" in out


def test_config_reports_a_clean_server_plainly(pg_client):
    pg_client.review_configuration = AsyncMock(return_value=[])

    code, out, _ = run(["config"])

    assert code == 0
    assert out.strip() == "Nothing measurable is wrong with the configuration."


def test_config_exit_code_fires_only_on_high_severity(pg_client):
    from pg_debug.configuration import ConfigFinding

    def finding(severity):
        return ConfigFinding(setting="fsync", current="off", severity=severity, summary="s", detail="d")

    pg_client.review_configuration = AsyncMock(return_value=[finding(Severity.MEDIUM)])
    assert run(["config", "--exit-code"])[0] == 0

    pg_client.review_configuration = AsyncMock(return_value=[finding(Severity.HIGH)])
    assert run(["config", "--exit-code"])[0] == cli.UNHEALTHY_EXIT_CODE


# -- upgrade-check and security ------------------------------------------------


def audit(*findings, unknown=False, command="security"):
    from pg_debug.audit import AuditReport
    from pg_debug.audit import CheckOutcome
    from pg_debug.audit import CheckStatus

    checks = [CheckOutcome(check="ssl", status=CheckStatus.FOUND if findings else CheckStatus.CLEAR, examined="ssl = off")]
    if unknown:
        checks.append(CheckOutcome(check="hba_rules", status=CheckStatus.UNKNOWN, examined="nothing", reason="only a superuser may read them"))
    return AuditReport(
        command=command, server_version="16.4", findings=list(findings), checks=checks, target_version=18 if command == "upgrade-check" else None
    )


def audit_finding(severity=Severity.MEDIUM, subject="ssl", check="ssl"):
    from pg_debug.audit import AuditFinding

    return AuditFinding(
        check=check,
        subject=subject,
        severity=severity,
        confidence=Confidence.PROVEN,
        summary="ssl is off.",
        detail="detail",
        remediation=["ALTER SYSTEM SET ssl = on;"],
    )


def test_upgrade_check_passes_its_target(pg_client):
    pg_client.check_upgrade = AsyncMock(return_value=audit(command="upgrade-check"))

    code, out, _ = run(["upgrade-check", "--to", "17"])

    assert code == 0
    pg_client.check_upgrade.assert_awaited_once_with(to=17)
    assert out.startswith("Upgrade from PostgreSQL 16.4 to 18")


def test_security_renders_findings_and_every_check(pg_client):
    pg_client.audit_security = AsyncMock(return_value=audit(audit_finding()))

    code, out, _ = run(["security"])

    assert code == 0
    assert "[medium] ssl: ssl" in out
    assert "ALTER SYSTEM SET ssl = on;" in out
    assert "found    ssl: ssl = off" in out
    assert out.strip().endswith("1 finding (1 medium).")


def test_a_check_that_could_not_tell_is_never_read_as_a_pass(pg_client):
    pg_client.audit_security = AsyncMock(return_value=audit(unknown=True))

    code, out, _ = run(["security", "--exit-code"])

    assert code == 0
    assert "unknown  hba_rules: only a superuser may read them" in out
    assert "No findings; 1 check could not tell (hba_rules), so this is not a clean bill." in out


def test_security_exit_code_fires_only_on_high_severity(pg_client):
    pg_client.audit_security = AsyncMock(return_value=audit(audit_finding(Severity.MEDIUM)))
    assert run(["security", "--exit-code"])[0] == 0

    pg_client.audit_security = AsyncMock(return_value=audit(audit_finding(Severity.HIGH)))
    assert run(["security", "--exit-code"])[0] == cli.UNHEALTHY_EXIT_CODE


def test_an_audit_baseline_silences_what_was_accepted(pg_client, tmp_path):
    path = tmp_path / "security.json"
    pg_client.audit_security = AsyncMock(return_value=audit(audit_finding(Severity.HIGH)))
    run(["security", "--save-baseline", str(path)])

    code, out, _ = run(["security", "--exit-code", "--baseline", str(path)])
    assert code == 0
    assert "1 known finding(s), nothing new" in out

    pg_client.audit_security = AsyncMock(
        return_value=audit(audit_finding(Severity.HIGH), audit_finding(Severity.HIGH, subject="role app", check="privileged_roles"))
    )
    code, out, _ = run(["security", "--exit-code", "--baseline", str(path)])
    assert code == cli.UNHEALTHY_EXIT_CODE
    assert "privileged_roles: role app" in out


def test_audit_json_marks_findings_against_the_baseline(pg_client, tmp_path):
    path = tmp_path / "upgrade.json"
    pg_client.check_upgrade = AsyncMock(return_value=audit(audit_finding(), command="upgrade-check"))
    run(["upgrade-check", "--save-baseline", str(path)])
    pg_client.check_upgrade = AsyncMock(return_value=audit(audit_finding(), audit_finding(subject="other"), unknown=True, command="upgrade-check"))

    code, out, _ = run(["--json", "upgrade-check", "--baseline", str(path)])

    payload = json.loads(out)
    assert code == 0
    assert payload["target_version"] == 18
    assert [(f["subject"], f["baseline"]) for f in payload["findings"]] == [("ssl", "known"), ("other", "new")]
    assert payload["checks"][1] == {"check": "hba_rules", "status": "unknown", "examined": "nothing", "reason": "only a superuser may read them"}


def test_check_migration_reads_a_file(pg_client, tmp_path):
    pg_client.check_migration = AsyncMock(return_value=[])
    migration = tmp_path / "001_add_column.sql"
    migration.write_text("ALTER TABLE orders ADD COLUMN n integer;")

    assert run(["check-migration", str(migration)])[0] == 0
    pg_client.check_migration.assert_awaited_once_with("ALTER TABLE orders ADD COLUMN n integer;")


def test_a_file_is_read_as_utf8_whatever_the_code_page(pg_client, tmp_path):
    """On Windows the code page is cp1252 or similar, which garbles UTF-8."""
    sql = "COMMENT ON TABLE orders IS 'Größe → 表';"
    pg_client.check_migration = AsyncMock(return_value=[])
    migration = tmp_path / "002_comment.sql"
    # with the byte order mark Notepad starts a UTF-8 file with
    migration.write_bytes(b"\xef\xbb\xbf" + sql.encode())

    assert run(["check-migration", str(migration)])[0] == 0
    pg_client.check_migration.assert_awaited_once_with(sql)


def test_a_file_that_is_not_utf8_is_refused(pg_client, tmp_path):
    pg_client.check_migration = AsyncMock(return_value=[])
    migration = tmp_path / "003_comment.sql"
    migration.write_bytes("COMMENT ON TABLE orders IS 'Größe';".encode("cp1252"))

    code, _, err = run(["check-migration", str(migration)])

    assert code == 1
    assert f"{migration} is not UTF-8 text" in err
    pg_client.check_migration.assert_not_awaited()


def test_stdin_is_read_as_utf8_whatever_the_code_page(pg_client, monkeypatch):
    """A pipe on Windows decodes as cp1252 or similar, which garbles UTF-8."""
    import io

    sql = "SELECT 'Größe → 表'"
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(sql.encode()), encoding="cp1252"))
    pg_client.execute_sql = AsyncMock(return_value=[])

    assert run(["execute", "-"])[0] == 0
    pg_client.execute_sql.assert_awaited_once_with(sql)


def test_tune_queries_reads_stdin_as_utf8_too(pg_client, monkeypatch):
    import io

    queries = "SELECT * FROM orders WHERE note = 'Größe'\nSELECT * FROM orders WHERE note = '表'\n"
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(queries.encode()), encoding="cp1252"))
    pg_client.analyze_query_indexes = AsyncMock(return_value={"recommendations": []})

    assert run(["tune-queries", "-"])[0] == 0
    pg_client.analyze_query_indexes.assert_awaited_once_with(
        ["SELECT * FROM orders WHERE note = 'Größe'", "SELECT * FROM orders WHERE note = '表'"], max_index_size_mb=10000, method="dta"
    )


def test_stdin_that_is_not_utf8_is_refused(pg_client, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO("SELECT 'Größe'".encode("cp1252")), encoding="cp1252"))
    pg_client.execute_sql = AsyncMock(return_value=[])

    code, _, err = run(["execute", "-"])

    assert code == 1
    assert "Standard input is not UTF-8 text" in err
    pg_client.execute_sql.assert_not_awaited()


def test_check_migration_accepts_sql_inline(pg_client):
    pg_client.check_migration = AsyncMock(return_value=[])

    assert run(["check-migration", "ALTER TABLE orders DROP COLUMN n"])[0] == 0
    pg_client.check_migration.assert_awaited_once_with("ALTER TABLE orders DROP COLUMN n")


def test_check_migration_offline_never_opens_a_connection(pg_client):
    """The point of --offline: CI has no production database to ask, and a
    staging one would soften every verdict."""
    code, out, _ = run(["check-migration", "--offline", "ALTER TABLE orders ALTER COLUMN id TYPE bigint"])

    assert code == 0
    assert "ACCESS EXCLUSIVE" in out
    pg_client.check_migration.assert_not_called()


def test_check_migration_offline_exit_code_fires_on_high_severity():
    assert run(["check-migration", "--offline", "--exit-code", "ALTER TABLE orders ALTER COLUMN id TYPE bigint"])[0] == cli.UNHEALTHY_EXIT_CODE
    assert run(["check-migration", "--offline", "--exit-code", "CREATE INDEX CONCURRENTLY i ON orders (a)"])[0] == 0


def test_check_migration_json_is_machine_readable():
    code, out, _ = run(["--json", "check-migration", "--offline", "CREATE INDEX i ON orders (a)"])

    assert code == 0
    payload = json.loads(out)
    assert payload[0]["lock"] == "SHARE"
    assert payload[0]["kind"] == "create_index"


def test_top_queries_passes_options(pg_client):
    pg_client.get_top_queries = AsyncMock(return_value=[])

    assert run(["top-queries", "--sort-by", "mean_time", "--limit", "3"])[0] == 0
    pg_client.get_top_queries.assert_awaited_once_with(sort_by="mean_time", limit=3)


def test_top_queries_sampling_interval(pg_client):
    pg_client.sample_top_queries = AsyncMock(return_value=QuerySample(queries=[]))

    assert run(["top-queries", "--interval", "5"])[0] == 0
    pg_client.sample_top_queries.assert_awaited_once_with(5.0, sort_by="resources", limit=10)


def test_activity_defaults(pg_client):
    pg_client.list_activity = AsyncMock(return_value=[])

    code, out, _ = run(["activity"])

    assert code == 0
    assert out.strip() == "(nothing running)"
    pg_client.list_activity.assert_awaited_once_with(include_idle=False, min_duration_seconds=None, blocked_only=False)


def test_activity_options(pg_client):
    pg_client.list_activity = AsyncMock(return_value=[])

    assert run(["activity", "--all", "--min-duration", "30", "--blocked"])[0] == 0
    pg_client.list_activity.assert_awaited_once_with(include_idle=True, min_duration_seconds=30.0, blocked_only=True)


def test_activity_renders_blocking(pg_client):
    pg_client.list_activity = AsyncMock(
        return_value=[
            {
                "pid": 42,
                "usename": "app",
                "state": "active",
                "backend_type": "client backend",
                "wait_event_type": "Lock",
                "wait_event": "transactionid",
                "state_seconds": 12.5,
                "blocked_by": [41],
                "query": "UPDATE orders SET status = 'paid' WHERE id = 1",
            }
        ]
    )

    code, out, _ = run(["activity"])

    assert code == 0
    assert "Lock:transactionid" in out
    assert "12.5s" in out
    assert "41" in out
    assert "UPDATE orders SET status = 'paid' WHERE id = 1" in out


def test_vacuum_progress_when_idle(pg_client):
    pg_client.vacuum_progress = AsyncMock(return_value=[])

    code, out, _ = run(["vacuum-progress"])

    assert code == 0
    assert out.strip() == "(no vacuum running)"


def test_vacuum_progress_renders_percentage(pg_client):
    pg_client.vacuum_progress = AsyncMock(
        return_value=[
            {
                "pid": 7,
                "schema": "public",
                "table": "orders",
                "phase": "scanning heap",
                "heap_blks_total": 100,
                "heap_blks_scanned": 79,
                "index_vacuum_count": 0,
                "percent_scanned": 79.0,
                "running_seconds": 65.0,
                "autovacuum": True,
            }
        ]
    )

    code, out, _ = run(["vacuum-progress"])

    assert code == 0
    assert "public.orders" in out
    assert "79.0%" in out
    assert "79/100" in out
    assert "1m05s" in out


# -- What PostgreSQL withheld from the role ---------------------------------


def withheld(pid, **fields):
    """A backend as list_activity returns it to a role without pg_read_all_stats."""
    return {"pid": pid, "datname": "shop", "usename": "postgres", "application_name": "worker", "state": None, "blocked_by": [], **fields}


#: What list_activity returned: the role's own waiter, and the other role's
#: backend in a transaction that it waits on.
LISTED = [
    {"pid": 42, "usename": "app_user", "state": "active", "blocked_by": [41], "in_transaction": True, "query": "SELECT 1"},
    withheld(41, in_transaction=True, query=HIDDEN_QUERY_TEXT),
]
#: What withheld_backends returned: that backend, and an idle one.
WITHHELD = [
    {**withheld(41), "in_transaction": True, "locks": ["AccessExclusiveLock on public.orders"], "blocking": [42]},
    {**withheld(43), "in_transaction": False, "locks": [], "blocking": []},
]


def test_activity_lists_a_withheld_backend_in_a_transaction_and_says_what_it_left_out(pg_client):
    """The reported failure: "(nothing running)" while another role's query held a lock."""
    pg_client.list_activity = AsyncMock(return_value=LISTED)
    pg_client.statistics_access = AsyncMock(return_value=PLAIN)
    pg_client.withheld_backends = AsyncMock(return_value=WITHHELD)

    code, out, err = run(["activity"])

    assert code == 0
    (row,) = [line for line in out.splitlines() if line.startswith("41")]
    assert "in a transaction" in row
    assert "holds AccessExclusiveLock on public.orders; blocking 42; database shop, application worker" in row
    assert err.startswith("pg-debug: 1 of these backends, of postgres, is in a transaction")
    assert "1 other backend, of postgres, is not listed: it shows no sign of a transaction" in err
    assert err.rstrip().endswith("GRANT pg_monitor TO app_user grants it.")


def test_activity_json_prints_the_rows_as_returned_and_the_note_on_standard_error(pg_client):
    """Scripts read the array with `jq '.[]'`."""
    pg_client.list_activity = AsyncMock(return_value=LISTED)
    pg_client.statistics_access = AsyncMock(return_value=PLAIN)
    pg_client.withheld_backends = AsyncMock(return_value=WITHHELD)

    code, out, err = run(["--json", "activity"])

    assert code == 0
    assert json.loads(out) == LISTED
    assert err.startswith("pg-debug: 1 of these backends, of postgres, is in a transaction")


def test_activity_asks_a_role_shown_everything_nothing_more(pg_client):
    pg_client.list_activity = AsyncMock(return_value=[LISTED[0]])

    code, out, err = run(["activity"])

    assert code == 0
    assert "SELECT 1" in out
    assert err == ""
    pg_client.statistics_access.assert_awaited_once()
    pg_client.withheld_backends.assert_not_awaited()


@pytest.mark.parametrize("argv", [["activity", "--all"], ["activity", "--blocked"]])
def test_activity_asks_nothing_when_the_rows_show_nothing_withheld(pg_client, argv):
    """With --all every backend is listed; with --blocked and nothing blocked, nothing is withheld that matters."""
    pg_client.list_activity = AsyncMock(return_value=[] if "--blocked" in argv else [LISTED[0]])

    assert run(argv)[0] == 0
    pg_client.statistics_access.assert_not_awaited()


def test_activity_blocked_describes_a_withheld_blocker(pg_client):
    pg_client.list_activity = AsyncMock(return_value=[LISTED[0]])
    pg_client.statistics_access = AsyncMock(return_value=PLAIN)
    pg_client.withheld_backends = AsyncMock(return_value=WITHHELD)

    code, _, err = run(["activity", "--blocked"])

    assert code == 0
    assert "Pid 41 (postgres@shop worker), which blocks 42, is in a transaction and holds AccessExclusiveLock on public.orders" in err


def test_locks_say_which_backends_in_the_trees_are_withheld(pg_client):
    pg_client.lock_tree = AsyncMock(return_value=[{**withheld(41, in_transaction=True, query=HIDDEN_QUERY_TEXT), "blocked_total": 0, "blocking": []}])
    pg_client.statistics_access = AsyncMock(return_value=PLAIN)

    code, out, err = run(["locks"])

    assert code == 0
    assert out.startswith("pid 41: in a transaction; state, query and wait withheld (postgres@shop worker)")
    assert err.startswith("pg-debug: 1 of the backends in these trees, of postgres, is shown without state")


def test_vacuum_progress_says_which_vacuums_are_withheld(pg_client):
    pg_client.vacuum_progress = AsyncMock(return_value=[{"pid": 7, "heap_blks_total": None, "heap_blks_scanned": None, "query": HIDDEN_QUERY_TEXT}])
    pg_client.statistics_access = AsyncMock(return_value=PLAIN)

    code, out, err = run(["vacuum-progress"])

    assert code == 0
    assert "None" not in out
    assert err.startswith("pg-debug: 1 of these vacuums is shown without table, phase or progress")


def test_top_queries_say_how_many_texts_were_withheld(pg_client):
    queries = [{"query": HIDDEN_QUERY_TEXT, "calls": 3}, {"query": "SELECT 1", "calls": 1}]
    pg_client.get_top_queries = AsyncMock(return_value=queries)
    pg_client.statistics_access = AsyncMock(return_value=PLAIN)

    code, out, err = run(["--json", "top-queries"])

    assert code == 0
    assert json.loads(out) == queries
    assert err.startswith("pg-debug: 1 of these queries is another role's: pg_stat_statements shows app_user its counters but not its text.")


def test_a_sample_says_how_many_statements_it_could_not_rank(pg_client):
    ranked = [{"queryid": 1, "query": "SELECT 1", "calls": 3}]
    pg_client.sample_top_queries = AsyncMock(return_value=QuerySample(queries=ranked, withheld=4))
    pg_client.statistics_access = AsyncMock(return_value=PLAIN)

    code, out, err = run(["--json", "top-queries", "--interval", "2", "--sort-by", "total_time"])

    assert code == 0
    assert json.loads(out) == ranked
    assert err.startswith("pg-debug: 4 statements of other roles ran during the interval and are not ranked")


def test_a_sample_with_nothing_withheld_asks_nothing_more(pg_client):
    pg_client.sample_top_queries = AsyncMock(return_value=QuerySample(queries=[{"queryid": 1, "query": "SELECT 1"}]))

    code, _, err = run(["top-queries", "--interval", "2"])

    assert (code, err) == (0, "")
    pg_client.statistics_access.assert_not_awaited()


def test_column_stats_renders_both_sections(pg_client):
    pg_client.column_stats = AsyncMock(return_value=[{"column": "user_id", "n_distinct": 500.0, "correlation": 0.01}])
    pg_client.extended_statistics = AsyncMock(return_value=[{"name": "orders_user_status", "columns": ["user_id", "status"], "kinds": ["d", "f"]}])

    code, out, _ = run(["column-stats", "orders"])

    assert code == 0
    assert "Column statistics" in out
    assert "Extended statistics" in out
    assert "user_id, status" in out


def test_column_stats_without_statistics(pg_client):
    pg_client.column_stats = AsyncMock(return_value=[])
    pg_client.extended_statistics = AsyncMock(return_value=[])

    code, out, _ = run(["column-stats", "orders"])

    assert code == 0
    assert "never been analyzed" in out


def test_tune_queries_collects_queries(pg_client):
    pg_client.analyze_query_indexes = AsyncMock(return_value={"recommendations": []})

    assert run(["tune-queries", "SELECT 1", "SELECT 2", "--max-index-size-mb", "50"])[0] == 0
    pg_client.analyze_query_indexes.assert_awaited_once_with(["SELECT 1", "SELECT 2"], max_index_size_mb=50, method="dta")


def test_tune_workload_renders_recommendations(pg_client):
    pg_client.analyze_workload_indexes = AsyncMock(
        return_value={
            "summary": {
                "total_recommendations": 1,
                "base_cost": "100.0",
                "new_cost": "10.0",
                "total_size_bytes": "8.0 MB",
                "improvement_multiple": "10.0",
            },
            "recommendations": [
                {
                    "index_apply_order": 1,
                    "index_definition": "CREATE INDEX ON public.orders USING btree (user_id)",
                    "index_estimated_size": "8.0 MB",
                    "benefit_of_this_index_only": {"improvement_multiple": "10.0"},
                    "benefit_after_previous_indexes": {"improvement_multiple": "10.0"},
                }
            ],
            "query_impact": [],
        }
    )

    code, out, _ = run(["tune-workload"])

    assert code == 0
    assert "CREATE INDEX ON public.orders USING btree (user_id)" in out
    assert "10.0x improvement" in out


# -- Error handling ---------------------------------------------------------


def test_library_errors_are_reported_without_a_traceback(pg_client):
    pg_client.get_top_queries = AsyncMock(side_effect=ExtensionRequiredError("pg_stat_statements", "Run CREATE EXTENSION pg_stat_statements;"))

    code, out, err = run(["top-queries"])

    assert code == 1
    assert out == ""
    assert "CREATE EXTENSION pg_stat_statements" in err


def test_a_tuning_failure_exits_non_zero_with_the_reason(pg_client):
    pg_client.analyze_workload_indexes = AsyncMock(side_effect=IndexTuningError("None of the 3 queries in the workload could be analyzed"))

    code, out, err = run(["tune-workload"])

    assert code == 1
    assert out == ""
    assert err == "pg-debug: None of the 3 queries in the workload could be analyzed\n"


def test_unexpected_errors_hide_the_password(pg_client):
    pg_client.list_schemas = AsyncMock(side_effect=RuntimeError("failed on postgresql://user:hunter2@localhost/db"))

    code, _, err = run(["schemas"])

    assert code == 1
    assert "hunter2" not in err
    assert "****" in err


def test_llm_model_flag_reaches_the_client(pg_client):
    pg_client.analyze_query_indexes = AsyncMock(return_value={"recommendations": []})

    with patch("pg_debug.cli.PgDebug") as client_cls:
        client_cls.return_value = pg_client
        assert run(["--llm-model", "o3-mini", "tune-queries", "--method", "llm", "SELECT 1"])[0] == 0

    assert client_cls.call_args.kwargs["llm_model"] == "o3-mini"


def test_llm_model_defaults_to_unset(pg_client):
    pg_client.list_schemas = AsyncMock(return_value=[])

    with patch("pg_debug.cli.PgDebug") as client_cls:
        client_cls.return_value = pg_client
        assert run(["schemas"])[0] == 0

    assert client_cls.call_args.kwargs["llm_model"] is None


# -- Expression index specs -------------------------------------------------


@pytest.mark.parametrize(
    "spec,columns",
    [
        ("users(lower(email))", ["lower(email)"]),
        ("users( lower(email) )", ["lower(email)"]),
        ("users(substr(email, 1, 3))", ["substr(email, 1, 3)"]),
        ("orders(date_trunc('day', created_at))", ["date_trunc('day', created_at)"]),
        ("users((first_name || ' ' || last_name))", ["(first_name || ' ' || last_name)"]),
        ("users(coalesce(nickname, email), status)", ["coalesce(nickname, email)", "status"]),
        ("users(status, lower(email))", ["status", "lower(email)"]),
        ("users(upper(lower(trim(email))))", ["upper(lower(trim(email)))"]),
    ],
)
def test_parse_index_spec_accepts_expressions(spec, columns):
    assert cli.parse_index_spec(spec)["columns"] == columns


def test_parse_index_spec_keeps_commas_inside_quotes():
    assert cli.parse_index_spec("t(replace(name, ',', ';'))")["columns"] == ["replace(name, ',', ';')"]


def test_parse_index_spec_handles_doubled_quotes():
    assert cli.parse_index_spec("t(replace(name, '''', ''))")["columns"] == ["replace(name, '''', '')"]


def test_parse_index_spec_accepts_a_quoted_table():
    parsed = cli.parse_index_spec('"My Table"(col)')
    assert parsed["table"] == '"My Table"'


def test_parse_index_spec_accepts_a_quoted_column():
    assert cli.parse_index_spec('t("Mixed Case")')["columns"] == ['"Mixed Case"']


def test_parse_index_spec_method_after_an_expression():
    parsed = cli.parse_index_spec("users(lower(email)):hash")
    assert parsed == {"table": "users", "columns": ["lower(email)"], "using": "hash"}


@pytest.mark.parametrize(
    "spec,reason",
    [
        ("users(lower(email)", "Unbalanced"),
        ("(email)", "No table name"),
        ("users()", "At least one column"),
        ("users(email) extra", "Unexpected text"),
        ("users(email):not a method", "Invalid index method"),
    ],
)
def test_parse_index_spec_rejects_malformed_expressions(spec, reason):
    with pytest.raises(argparse.ArgumentTypeError, match=reason):
        cli.parse_index_spec(spec)


def test_split_index_columns_splits_only_at_the_top_level():
    assert cli.split_index_columns("a, f(b, c), (d || ',' || e)") == ["a", "f(b, c)", "(d || ',' || e)"]


def test_split_index_columns_ignores_blank_entries():
    assert cli.split_index_columns(" a , , b ") == ["a", "b"]


# -- Partitioning -----------------------------------------------------------


def partition_candidate(**overrides):
    from pg_debug.partition import PartitionCandidate
    from pg_debug.partition import Strategy

    base = dict(
        schema="public",
        table="events",
        table_bytes=400 * 1024**3,
        row_count=2_000_000_000,
        column="created_at",
        column_type="timestamp with time zone",
        strategy=Strategy.RANGE,
        pruning_share=0.9,
        recommended=True,
        reasons=["Queries restricting on created_at would read one partition."],
        blockers=[],
        ddl=["CREATE TABLE public.events_partitioned (LIKE public.events INCLUDING ALL) PARTITION BY RANGE (created_at);"],
        evidence={"alternatives": []},
    )
    return PartitionCandidate(**{**base, **overrides})


def test_partitions_renders_a_recommendation(pg_client):
    pg_client.suggest_partitions = AsyncMock(return_value=[partition_candidate()])

    code, out, _ = run(["partitions"])

    assert code == 0
    assert "[partition] public.events" in out
    assert "RANGE (created_at)" in out
    assert "90%" in out
    assert "PARTITION BY RANGE" in out


def test_partitions_reports_a_refusal_under_its_own_heading(pg_client):
    """A rejection skimmed off the top of the output would read as advice."""
    pg_client.suggest_partitions = AsyncMock(return_value=[partition_candidate(recommended=False, pruning_share=0.2, ddl=[])])

    code, out, _ = run(["partitions"])

    assert code == 0
    assert "Examined and not recommended" in out
    assert "[leave as it is] public.events" in out
    assert "PARTITION BY" not in out


def test_partitions_says_so_when_nothing_qualifies(pg_client):
    pg_client.suggest_partitions = AsyncMock(return_value=[])

    code, out, _ = run(["partitions"])

    assert code == 0
    assert out.strip() == "No table is both large enough and busy enough to be worth partitioning."


def test_partitions_json_carries_the_verdict_and_its_evidence(pg_client):
    pg_client.suggest_partitions = AsyncMock(return_value=[partition_candidate()])

    code, out, _ = run(["--json", "partitions"])

    payload = json.loads(out)
    assert code == 0
    assert payload[0]["recommended"] is True
    assert payload[0]["strategy"] == "range"
    assert payload[0]["pruning_share"] == 0.9


def plan_finding(**overrides):
    from pg_debug.plans import PlanFinding
    from pg_debug.plans import PlanIssue
    from pg_debug.severity import Severity

    base = dict(
        issue=PlanIssue.ROW_ESTIMATE,
        severity=Severity.HIGH,
        node_type="Seq Scan",
        relation="public.visits",
        summary="The Seq Scan on public.visits expected 17 rows and found 4,000 — an underestimate of 235x.",
        detail="Every estimate above this node inherited the error.",
        cost_ms=940.0,
        share_of_time=0.87,
        evidence={"estimated_rows": 17, "actual_rows": 4000},
        remediation=[],
    )
    return PlanFinding(**{**base, **overrides})


def test_diagnose_renders_a_finding_with_the_share_behind_it(pg_client):
    pg_client.diagnose_query = AsyncMock(return_value=[plan_finding()])

    code, out, _ = run(["diagnose", "SELECT 1"])

    assert code == 0
    assert "[high] row_estimate — Seq Scan on public.visits" in out
    assert "87% of the query's work" in out


def test_diagnose_shows_the_remediation_a_finding_carries(pg_client):
    pg_client.diagnose_query = AsyncMock(return_value=[plan_finding(remediation=["ANALYZE public.visits;"])])

    code, out, _ = run(["diagnose", "SELECT 1"])

    assert "ANALYZE public.visits;" in out


def test_diagnose_says_the_plan_is_fine_when_nothing_is_wrong(pg_client):
    """An empty result has to read as an answer rather than as a failure."""
    pg_client.diagnose_query = AsyncMock(return_value=[])

    code, out, _ = run(["diagnose", "SELECT 1"])

    assert code == 0
    assert "Nothing wrong with this plan" in out


def test_diagnose_warns_that_the_shares_are_not_a_breakdown(pg_client):
    """Two findings can be about the same node, so the percentages overlap.
    A column of them otherwise reads as adding up to the whole."""
    pg_client.diagnose_query = AsyncMock(return_value=[plan_finding(), plan_finding()])

    _, out, _ = run(["diagnose", "SELECT 1"])

    assert "not a breakdown of the total" in out


def test_diagnose_json_carries_the_evidence(pg_client):
    pg_client.diagnose_query = AsyncMock(return_value=[plan_finding()])

    code, out, _ = run(["--json", "diagnose", "SELECT 1"])

    payload = json.loads(out)
    assert code == 0
    assert payload[0]["issue"] == "row_estimate"
    assert payload[0]["evidence"]["actual_rows"] == 4000
    assert payload[0]["share_of_time"] == 0.87


def test_diagnose_passes_the_threshold_through(pg_client):
    pg_client.diagnose_query = AsyncMock(return_value=[])

    run(["diagnose", "SELECT 1", "--min-ratio", "25"])

    pg_client.diagnose_query.assert_awaited_once_with("SELECT 1", min_ratio=25.0)


def test_partitions_passes_its_thresholds_through(pg_client):
    pg_client.suggest_partitions = AsyncMock(return_value=[])

    run(["partitions", "--min-size", "500MB", "--min-pruning-share", "0.7", "--limit", "50"])

    pg_client.suggest_partitions.assert_awaited_once_with(
        min_table_bytes=500 * 1024**2,
        min_pruning_share=0.7,
        limit=50,
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1024", 1024),
        ("1KB", 1024),
        ("500MB", 500 * 1024**2),
        ("2GB", 2 * 1024**3),
        ("1.5gb", int(1.5 * 1024**3)),
        (" 4 TB ", 4 * 1024**4),
    ],
)
def test_parse_size_accepts_the_units_postgres_uses(text, expected):
    assert cli.parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "big", "10PB", "GB"])
def test_parse_size_rejects_what_is_not_a_size(text):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_size(text)


# -- diagnose --plan ----------------------------------------------------------

MISESTIMATE_PLAN = [
    {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "visits",
            "Schema": "public",
            "Total Cost": 100.0,
            "Startup Cost": 0.0,
            "Plan Rows": 50,
            "Plan Width": 8,
            "Actual Total Time": 25.0,
            "Actual Startup Time": 0.0,
            "Actual Rows": 1000,
            "Actual Loops": 1,
        }
    }
]


@pytest.fixture
def no_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URI", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_diagnose_plan_without_a_database_url_never_opens_a_connection(pg_client, no_database_url, tmp_path):
    """A plan in hand and no database: the point of --plan."""
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(MISESTIMATE_PLAN))

    code, out, _ = run(["diagnose", "--plan", str(plan)])

    assert code == 0
    assert "expected 50 rows and found 1,000" in out
    assert "needs this database's catalog" in out
    pg_client.diagnose_plan.assert_not_called()
    pg_client.diagnose_query.assert_not_called()


def test_diagnose_plan_asks_the_database_when_there_is_one(pg_client, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URI", "postgresql://db")
    pg_client.diagnose_plan = AsyncMock(return_value=[])
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(MISESTIMATE_PLAN))

    code, _, _ = run(["diagnose", "--plan", str(plan)])

    assert code == 0
    pg_client.diagnose_plan.assert_awaited_once()
    assert json.loads(pg_client.diagnose_plan.await_args_list[0].args[0]) == MISESTIMATE_PLAN


def test_diagnose_plan_offline_ignores_the_database_url(pg_client, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URI", "postgresql://db")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(MISESTIMATE_PLAN))

    code, out, _ = run(["--json", "diagnose", "--plan", "--offline", str(plan)])

    assert code == 0
    assert json.loads(out)[0]["issue"] == "row_estimate"
    pg_client.diagnose_plan.assert_not_called()


def test_diagnose_plan_reads_stdin(no_database_url, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(MISESTIMATE_PLAN)))

    code, out, _ = run(["diagnose", "--plan", "-"])

    assert code == 0
    assert "expected 50 rows" in out


def test_diagnose_plan_refuses_the_text_format_with_a_clear_message(no_database_url):
    code, _, err = run(["diagnose", "--plan", "Seq Scan on orders  (cost=0.00..1.00 rows=1 width=8)"])

    assert code == 1
    assert "FORMAT JSON" in err


def test_diagnose_reads_the_query_from_a_file(pg_client, tmp_path):
    """The README promises `pg-debug diagnose ./slow.sql`."""
    pg_client.diagnose_query = AsyncMock(return_value=[])
    sql = tmp_path / "slow.sql"
    sql.write_text("SELECT 1")

    assert run(["diagnose", str(sql)])[0] == 0
    assert pg_client.diagnose_query.await_args_list[0].args[0] == "SELECT 1"


# -- index-use --------------------------------------------------------------


def index_use_verdict(used=False):
    from pg_debug.index_use import IndexUse
    from pg_debug.index_use import IndexUseFinding
    from pg_debug.index_use import IndexUseReason
    from pg_debug.model import Confidence

    verdict = IndexUse(
        index="orders_status",
        table="public.orders",
        definition="CREATE INDEX orders_status ON public.orders USING btree (status)",
        method="btree",
        columns=["status"],
        key_columns=1,
        predicate=None,
        valid=True,
        size_bytes=8192,
        used=used,
        node_type="Index Scan" if used else None,
    )
    if not used:
        verdict.findings = [
            IndexUseFinding(
                reason=IndexUseReason.NO_PREDICATE,
                confidence=Confidence.PROVEN,
                summary="The query does not restrict status, the index's first column.",
                detail="The query restricts region on public.orders.",
                evidence={"index_columns": ["status"]},
                remediation=["CREATE INDEX ON public.orders (region);"],
            )
        ]
    return verdict


def test_index_use_renders_the_verdict_and_the_reason(pg_client):
    pg_client.explain_index_use = AsyncMock(return_value=[index_use_verdict()])

    code, out, _ = run(["index-use", "SELECT * FROM orders WHERE region = 'eu'"])

    assert code == 0
    assert "public.orders: orders_status  btree (status)  8.0 KiB  -- NOT used" in out
    assert "[proven] no predicate: The query does not restrict status" in out
    assert "CREATE INDEX ON public.orders (region);" in out


def test_index_use_passes_the_index_filter_and_emits_json(pg_client):
    pg_client.explain_index_use = AsyncMock(return_value=[index_use_verdict(used=True)])

    code, out, _ = run(["--json", "index-use", "-i", "orders_status", "SELECT 1 FROM orders"])

    assert code == 0
    pg_client.explain_index_use.assert_awaited_once_with("SELECT 1 FROM orders", index="orders_status")
    assert json.loads(out)[0]["used"] is True


# -- Baselines --------------------------------------------------------------


def unhealthy_index_results(*indexes):
    return health_results(
        ("Connection health", True, "ok", {}),
        (
            "Index bloat",
            False,
            "bloated",
            {"findings": [{"schema": "public", "table": "orders", "index": name, "bloat_percent": 30.0} for name in indexes]},
        ),
    )


def test_save_baseline_writes_what_the_run_found(pg_client, tmp_path):
    pg_client.analyze_db_health = AsyncMock(return_value=unhealthy_index_results("orders_a"))
    path = tmp_path / "health.json"

    code, _, err = run(["health", "--save-baseline", str(path)])

    assert code == 0
    assert "Saved a baseline of 1 finding(s)" in err
    assert json.loads(path.read_text())["findings"] == [{"check": "Index bloat", "index": "orders_a", "schema": "public", "table": "orders"}]


def test_a_known_finding_does_not_fail_the_gate_but_a_new_one_does(pg_client, tmp_path):
    path = tmp_path / "health.json"
    pg_client.analyze_db_health = AsyncMock(return_value=unhealthy_index_results("orders_a"))
    run(["health", "--save-baseline", str(path)])

    code, out, _ = run(["health", "--exit-code", "--baseline", str(path)])
    assert code == 0
    assert f"Baseline {path}: 1 known finding(s), nothing new." in out

    pg_client.analyze_db_health = AsyncMock(return_value=unhealthy_index_results("orders_a", "orders_b"))
    code, out, _ = run(["health", "--exit-code", "--baseline", str(path)])
    assert code == cli.UNHEALTHY_EXIT_CODE
    assert "1 known finding(s) ignored, 1 new:" in out
    assert "  Index bloat: public.orders orders_b" in out


def test_json_output_marks_each_finding_against_the_baseline(pg_client, tmp_path):
    path = tmp_path / "health.json"
    pg_client.analyze_db_health = AsyncMock(return_value=unhealthy_index_results("orders_a"))
    run(["health", "--save-baseline", str(path)])
    pg_client.analyze_db_health = AsyncMock(return_value=unhealthy_index_results("orders_a", "orders_b"))

    code, out, _ = run(["--json", "health", "--baseline", str(path)])

    assert code == 0
    findings = json.loads(out)["Index bloat"]["findings"]
    assert [(f["index"], f["baseline"]) for f in findings] == [("orders_a", "known"), ("orders_b", "new")]


def test_a_run_in_which_a_check_could_not_run_saves_no_baseline(pg_client, tmp_path):
    """A baseline of it would hold nothing for that check, so what it finds
    once it runs would fail as new; saved over the accepted baseline, a lock
    during one run threw away everything the team had accepted."""
    path = tmp_path / "health.json"
    pg_client.analyze_db_health = AsyncMock(return_value=unhealthy_index_results("orders_a"))
    run(["health", "--save-baseline", str(path)])
    accepted = path.read_text()
    pg_client.analyze_db_health = AsyncMock(
        return_value={
            **health_results(could_not_run("Index bloat"), could_not_run("Unused index check")),
            **health_results(("Vacuum health", False, "Tables approaching transaction ID wraparound:", {})),
        }
    )

    code, out, err = run(["health", "--exit-code", "--save-baseline", str(path)])

    assert code == 1
    assert path.read_text() == accepted
    assert "Index bloat: Could not run" in out
    assert "2 of 3 health checks could not run, so this run is incomplete: Index bloat, Unused index check." in err
    assert f"Nothing was written to {path}" in err
    assert "Saved a baseline" not in err


def test_against_a_baseline_an_incomplete_run_is_not_reported_as_a_pass(pg_client, tmp_path):
    """Nothing new, said of a run in which nothing ran, reads as a pass."""
    path = tmp_path / "health.json"
    pg_client.analyze_db_health = AsyncMock(return_value=unhealthy_index_results("orders_a"))
    run(["health", "--save-baseline", str(path)])
    pg_client.analyze_db_health = AsyncMock(return_value=health_results(could_not_run("Index bloat"), could_not_run("Unused index check")))

    code, out, _ = run(["health", "--exit-code", "--baseline", str(path)])

    assert code == 1
    assert out.splitlines()[-1] == (
        f"Baseline {path}: 0 known finding(s), nothing new among the checks that ran. "
        "2 checks could not run and were not compared, so this is not a pass."
    )

    pg_client.analyze_db_health = AsyncMock(
        return_value={**unhealthy_index_results("orders_a", "orders_b"), **health_results(could_not_run("Replication health"))}
    )
    code, out, _ = run(["health", "--exit-code", "--baseline", str(path)])

    assert code == cli.UNHEALTHY_EXIT_CODE
    assert out.splitlines()[-3:] == [
        f"Baseline {path}: 1 known finding(s) ignored, 1 new among the checks that ran:",
        "  Index bloat: public.orders orders_b",
        "1 check could not run and was not compared.",
    ]


def test_against_a_baseline_an_incomplete_run_still_fails(pg_client, tmp_path):
    """Nothing new was found, but only among the checks that ran."""
    path = tmp_path / "health.json"
    pg_client.analyze_db_health = AsyncMock(return_value=unhealthy_index_results("orders_a"))
    run(["health", "--save-baseline", str(path)])
    pg_client.analyze_db_health = AsyncMock(
        return_value={**unhealthy_index_results("orders_a"), **health_results(could_not_run("Replication health"))}
    )

    code, out, err = run(["--json", "health", "--exit-code", "--baseline", str(path)])

    assert code == 1
    assert "baseline" not in json.loads(out)["Replication health"]
    assert "1 of 3 health checks could not run" in err


def test_model_baseline_gates_only_new_high_severity_findings(pg_client, tmp_path):
    path = tmp_path / "model.json"
    pg_client.review_data_model = AsyncMock(return_value=model_findings((RuleId.MISSING_PRIMARY_KEY, Confidence.PROVEN, Severity.HIGH, "events")))
    run(["model", "--save-baseline", str(path)])

    assert run(["model", "--exit-code", "--baseline", str(path)])[0] == 0

    pg_client.review_data_model = AsyncMock(
        return_value=model_findings(
            (RuleId.MISSING_PRIMARY_KEY, Confidence.PROVEN, Severity.HIGH, "events"),
            (RuleId.NAIVE_TIMESTAMP, Confidence.PROVEN, Severity.MEDIUM, "orders"),
        )
    )
    assert run(["model", "--exit-code", "--baseline", str(path)])[0] == 0, "new but not high severity"

    pg_client.review_data_model = AsyncMock(return_value=model_findings((RuleId.MISSING_PRIMARY_KEY, Confidence.PROVEN, Severity.HIGH, "fresh")))
    code, out, _ = run(["--json", "model", "--exit-code", "--baseline", str(path)])
    assert code == cli.UNHEALTHY_EXIT_CODE
    assert json.loads(out)[0]["baseline"] == "new"


def test_a_baseline_from_another_command_is_an_error(pg_client, tmp_path):
    path = tmp_path / "health.json"
    pg_client.analyze_db_health = AsyncMock(return_value=unhealthy_index_results("orders_a"))
    run(["health", "--save-baseline", str(path)])
    pg_client.review_data_model = AsyncMock(return_value=[])

    code, _, err = run(["model", "--baseline", str(path)])

    assert code == 1
    assert "baseline for `pg-debug health`, not `pg-debug model`" in err


def test_the_window_an_unused_index_needs_is_passed_through(pg_client):
    """A crash, a failover or pg_stat_reset() starts the counters again."""
    pg_client.analyze_db_health = AsyncMock(return_value=health_results(("Unused index check", True, "ok", {})))

    run(["health", "--type", "index", "--min-window-days", "30"])

    pg_client.analyze_db_health.assert_awaited_once_with("index", min_window_days=30.0)


def test_cumulative_top_queries_say_since_when_they_counted(pg_client):
    """A total since a migration months ago and one since a reset this
    morning look alike; the ranking says neither."""
    pg_client.get_top_queries = AsyncMock(return_value=[{"query": "SELECT 1", "calls": 3}])
    pg_client.counting_windows = AsyncMock(
        return_value={
            "tables": None,
            "checkpoints": None,
            "statements": {
                "counting_since": "2026-10-14T09:00:00+00:00",
                "counting_seconds": 3 * 3600,
                "counting_began_with": "reset",
                "statements_evicted": 12,
            },
        }
    )

    code, out, err = run(["--json", "top-queries"])

    assert code == 0
    assert json.loads(out) == [{"query": "SELECT 1", "calls": 3}]
    assert "These totals are counts since 2026-10-14 09:00 UTC (3 hours), when pg_stat_statements was last reset" in err
    assert "evicted statements 12 times since" in err


def test_top_queries_before_pg_stat_statements_1_9_say_their_span_is_not_dated(pg_client):
    pg_client.get_top_queries = AsyncMock(return_value=[])
    pg_client.counting_windows = AsyncMock(
        return_value={
            "tables": None,
            "checkpoints": None,
            "statements": {"counting_since": None, "counting_seconds": None, "counting_began_with": None},
        }
    )

    _, _, err = run(["top-queries"])

    assert "does not record when it was last reset" in err


def test_cumulative_top_queries_say_a_crash_lost_statements(pg_client):
    """A crash leaves pg_stat_statements' stats_reset alone and loses what it
    recorded since the last clean shutdown."""
    pg_client.get_top_queries = AsyncMock(return_value=[])
    pg_client.counting_windows = AsyncMock(
        return_value={
            "tables": {"counting_since": "2026-10-14T10:00:00+00:00", "counting_seconds": 7200, "counting_began_with": "restart"},
            "checkpoints": None,
            "statements": {"counting_since": "2026-10-01T00:00:00+00:00", "counting_seconds": 13 * 86400, "counting_began_with": "reset"},
        }
    )

    _, _, err = run(["top-queries"])

    assert "PostgreSQL discarded its statistics at 2026-10-14 10:00 UTC" in err
    assert "lost what it had recorded since its last clean shutdown" in err

import argparse
import json
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from pg_debug import cli
from pg_debug.artifacts import ExplainPlanArtifact
from pg_debug.artifacts import PlanNode
from pg_debug.errors import ExtensionRequiredError


@pytest.fixture
def pg_client():
    """Patch PgDebug so commands run against a mock instead of a database."""
    with patch("pg_debug.cli.PgDebug") as client_cls:
        client = client_cls.return_value
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
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
    assert args.timeout == 30


# -- Commands ---------------------------------------------------------------


def test_schemas_renders_a_table(pg_client):
    pg_client.list_schemas = AsyncMock(return_value=[{"schema_name": "public", "schema_type": "User Schema"}])

    code, out, _ = run(["schemas"])

    assert code == 0
    assert "schema_name" in out
    assert "public" in out
    assert "(1 row)" in out


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


def test_health_renders_one_line_per_check(pg_client):
    pg_client.analyze_db_health = AsyncMock(return_value={"Connection health": "ok", "Vacuum health": "ok"})

    code, out, _ = run(["health", "--type", "connection,vacuum"])

    assert code == 0
    assert out.splitlines()[:2] == ["Connection health: ok", "Vacuum health: ok"]
    pg_client.analyze_db_health.assert_awaited_once_with("connection,vacuum")


def test_top_queries_passes_options(pg_client):
    pg_client.get_top_queries = AsyncMock(return_value=[])

    assert run(["top-queries", "--sort-by", "mean_time", "--limit", "3"])[0] == 0
    pg_client.get_top_queries.assert_awaited_once_with(sort_by="mean_time", limit=3)


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


def test_unexpected_errors_hide_the_password(pg_client):
    pg_client.list_schemas = AsyncMock(side_effect=RuntimeError("failed on postgresql://user:hunter2@localhost/db"))

    code, _, err = run(["schemas"])

    assert code == 1
    assert "hunter2" not in err
    assert "****" in err

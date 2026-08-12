import json
from datetime import datetime
from decimal import Decimal

from pg_debug.formatting import render_index_recommendations
from pg_debug.formatting import render_mapping
from pg_debug.formatting import render_object_details
from pg_debug.formatting import render_rows
from pg_debug.formatting import to_json


def test_to_json_handles_database_types():
    value = {"total": Decimal("1.5"), "at": datetime(2026, 1, 1)}
    assert json.loads(to_json(value)) == {"total": "1.5", "at": "2026-01-01 00:00:00"}


def test_render_rows_aligns_columns():
    rows = [{"name": "orders", "kind": "table"}, {"name": "a", "kind": "view"}]

    lines = render_rows(rows).splitlines()

    assert lines[0] == "name    kind"
    assert lines[2] == "orders  table"
    assert lines[3] == "a       view"
    assert lines[-1] == "(2 rows)"


def test_render_rows_includes_columns_missing_from_some_rows():
    lines = render_rows([{"a": 1}, {"b": 2}]).splitlines()

    assert lines[0] == "a  b"
    assert lines[2] == "1"
    assert lines[3].startswith("   2")


def test_render_rows_collapses_whitespace_and_truncates():
    rows = [{"query": "SELECT\n  *\n  FROM orders WHERE id = 1"}]

    rendered = render_rows(rows, max_width=20)

    assert "\n  " not in rendered.split("\n")[2]
    assert "…" in rendered


def test_render_rows_when_empty():
    assert render_rows([]) == "(no rows)"
    assert render_rows([], empty="nothing here") == "nothing here"


def test_render_rows_singular_row_count():
    assert render_rows([{"a": 1}]).endswith("(1 row)")


def test_render_mapping():
    assert render_mapping({"Vacuum health": "ok"}) == "Vacuum health: ok"
    assert render_mapping({}) == "(nothing to report)"


def test_render_object_details():
    details = {
        "basic": {"schema": "public", "name": "orders", "type": "table"},
        "columns": [{"column": "id", "data_type": "integer", "is_nullable": "NO", "default": None}],
        "constraints": [{"name": "orders_pkey", "type": "PRIMARY KEY", "columns": ["id", "tenant_id"]}],
        "indexes": [{"name": "orders_pkey", "definition": "CREATE UNIQUE INDEX orders_pkey ON public.orders USING btree (id)"}],
    }

    rendered = render_object_details(details)

    assert rendered.startswith("Table public.orders")
    assert "id, tenant_id" in rendered
    # Index definitions are not truncated.
    assert "CREATE UNIQUE INDEX orders_pkey ON public.orders USING btree (id)" in rendered


def test_render_object_details_for_flat_objects():
    assert render_object_details({"name": "hypopg", "version": "1.4.1"}) == "name: hypopg\nversion: 1.4.1"


def test_render_object_details_when_missing():
    assert render_object_details({}) == "(object not found)"


def test_render_index_recommendations_reports_errors():
    assert render_index_recommendations({"error": "no queries found"}) == "Index tuning failed: no queries found"


def test_render_index_recommendations_when_none_found():
    assert render_index_recommendations({"recommendations": "No index recommendations found."}) == "No index recommendations found."


def test_render_index_recommendations_includes_warnings():
    result = {
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
                "benefit_after_previous_indexes": {"improvement_multiple": "4.0"},
                "warning": "includes a long text column",
            }
        ],
        "query_impact": [
            {
                "query": "SELECT * FROM orders WHERE user_id = 1",
                "base_cost": "100.0",
                "new_cost": "10.0",
                "improvement_multiple": "10.0",
            }
        ],
    }

    rendered = render_index_recommendations(result)

    assert "1 recommended index(es)" in rendered
    assert "warning: includes a long text column" in rendered
    assert "SELECT * FROM orders WHERE user_id = 1" in rendered

import json
from datetime import datetime
from decimal import Decimal

from pg_debug.cli.render import render_index_recommendations
from pg_debug.cli.render import render_lock_tree
from pg_debug.cli.render import render_mapping
from pg_debug.cli.render import render_object_details
from pg_debug.cli.render import render_rows
from pg_debug.cli.render import to_json


def test_to_json_handles_database_types():
    value = {"total": Decimal("1.5"), "at": datetime(2026, 1, 1)}
    assert json.loads(to_json(value)) == {"total": "1.5", "at": "2026-01-01 00:00:00"}


def test_a_numeric_ratio_is_rounded_rather_than_printed_raw():
    """pg_stat_statements returns these fractions as numeric, and Python prints
    a Decimal at full precision — 0.09286047596826878208 in a column of a
    table nobody can read."""
    rendered = render_rows([{"frac": Decimal("0.09286047596826878208")}])

    assert "0.093" in rendered
    assert "0.09286047596826878208" not in rendered


def test_a_numeric_zero_is_not_printed_in_scientific_notation():
    """A numeric division that came out zero carries its scale, so str() gives
    0E-20 — which reads as anything but zero. It is whole, so it prints whole."""
    rendered = render_rows([{"frac": Decimal("0E-20")}])

    assert "0E-20" not in rendered
    assert rendered.splitlines()[2].strip() == "0"


def test_a_whole_numeric_keeps_its_whole_number():
    """wal_bytes is a numeric because it can exceed a bigint, not because it
    has a fractional part. Printing it as 4198.000 would be worse than the raw
    value."""
    assert "4198" in render_rows([{"wal_bytes": Decimal("4198")}])
    assert "4198.000" not in render_rows([{"wal_bytes": Decimal("4198")}])


def test_a_boolean_stays_a_boolean():
    """A bool is an int in Python, so a numeric branch that catches it first
    turns True into 1."""
    rendered = render_rows([{"autovacuum": True, "healthy": False}])

    assert "True" in rendered
    assert "False" in rendered


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


def test_the_lock_tree_renders_the_root_first_and_indents_the_waiters():
    tree = [
        {
            "pid": 41,
            "usename": "app",
            "datname": "shop",
            "application_name": "worker",
            "state": "idle in transaction",
            "state_seconds": 125.0,
            "transaction_seconds": 130.0,
            "query": "UPDATE   orders\n   SET status = 'paid'",
            "waits_for": None,
            "blocked_total": 1,
            "blocking": [
                {
                    "pid": 42,
                    "usename": "app",
                    "datname": "shop",
                    "application_name": "",
                    "state": "active",
                    "state_seconds": 90.0,
                    "transaction_seconds": 90.0,
                    "query": "SELECT count(*) FROM orders",
                    "waits_for": {"description": "AccessShareLock on public.orders (41 holds AccessExclusiveLock)"},
                    "blocked_total": 0,
                    "blocking": [],
                }
            ],
        }
    ]

    text = render_lock_tree(tree)

    lines = text.splitlines()
    assert lines[0] == "pid 41: idle in transaction for 2m05s in a transaction open 2m10s (app@shop worker) blocking 1"
    assert lines[1] == "    UPDATE orders SET status = 'paid'"
    assert lines[2] == "  pid 42: waits for AccessShareLock on public.orders (41 holds AccessExclusiveLock); active for 1m30s (app@shop)"
    assert lines[3] == "      SELECT count(*) FROM orders"


def test_an_empty_lock_tree_says_so():
    assert render_lock_tree([]) == "(nothing is blocked)"

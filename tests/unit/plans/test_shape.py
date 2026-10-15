"""A plan's shape is what it does, not what it costs."""

import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.errors import ExplainPlanError
from pg_debug.errors import InvalidArgumentError
from pg_debug.plans.shape import PlanShape
from pg_debug.plans.shape import read_plan_shapes
from pg_debug.plans.shape import shape_fingerprint
from pg_debug.plans.shape import shape_lines
from pg_debug.plans.shape import split_queries


def plan(index="orders_user_idx", cost=14.8, rows=3):
    return {
        "Plan": {
            "Node Type": "Nested Loop",
            "Join Type": "Inner",
            "Total Cost": cost * 10,
            "Plan Rows": rows,
            "Plans": [
                {"Node Type": "Seq Scan", "Parent Relationship": "Outer", "Relation Name": "users", "Alias": "u", "Total Cost": cost, "Plan Rows": 1},
                {
                    "Node Type": "Index Scan",
                    "Parent Relationship": "Inner",
                    "Scan Direction": "Forward",
                    "Relation Name": "orders",
                    "Index Name": index,
                    "Index Cond": "(user_id = u.id)",
                    "Total Cost": cost,
                    "Plan Rows": rows,
                },
            ],
        }
    }


def test_the_shape_names_nodes_strategies_relations_and_indexes():
    assert shape_lines(plan()) == ["Nested Loop Inner", "  Seq Scan on users", "  Index Scan on orders using orders_user_idx"]


def test_costs_and_row_estimates_do_not_change_the_shape():
    assert shape_fingerprint(shape_lines(plan(cost=14.8, rows=3))) == shape_fingerprint(shape_lines(plan(cost=9000, rows=40_000)))


def test_another_index_is_another_shape():
    assert shape_fingerprint(shape_lines(plan())) != shape_fingerprint(shape_lines(plan(index="orders_pkey")))


def test_parallel_backward_and_subplans_are_part_of_the_shape():
    node = {
        "Node Type": "Gather",
        "Plans": [
            {"Node Type": "Seq Scan", "Parallel Aware": True, "Relation Name": "t"},
            {
                "Node Type": "Index Only Scan",
                "Parent Relationship": "SubPlan",
                "Scan Direction": "Backward",
                "Relation Name": "u",
                "Index Name": "u_pkey",
            },
        ],
    }
    assert shape_lines(node) == ["Gather", "  Parallel Seq Scan on t", "  [SubPlan] Index Only Scan Backward on u using u_pkey"]


def test_queries_are_known_by_their_name_or_fingerprint():
    named = split_queries("-- name: orders_by_user\nSELECT * FROM orders WHERE user_id = 1;\nSELECT 2")

    assert named[0] == ("orders_by_user", "-- name: orders_by_user\nSELECT * FROM orders WHERE user_id = 1")
    assert named[1][1] == "SELECT 2" and len(named[1][0]) == 16


def test_two_queries_that_differ_only_in_a_constant_need_names():
    with pytest.raises(InvalidArgumentError, match="name"):
        split_queries("SELECT * FROM t WHERE id = 1; SELECT * FROM t WHERE id = 2")
    assert len(split_queries("-- name: a\nSELECT * FROM t WHERE id = 1;\n-- name: b\nSELECT * FROM t WHERE id = 2")) == 2


def test_an_empty_file_is_refused():
    with pytest.raises(InvalidArgumentError, match="no statement"):
        split_queries("  -- nothing\n")


def test_the_baseline_key_is_the_name_and_the_rest_annotates_it():
    shape = PlanShape(query="SELECT 1", name="one", shape=["Result"], fingerprint="f")

    assert shape.key() == {"query": "one", "about": "SELECT 1", "fingerprint": "f", "shape": ["Result"]}


@pytest.mark.asyncio
async def test_a_query_that_cannot_be_planned_is_reported_and_the_others_are_planned():
    tool = MagicMock()
    artifact = MagicMock()
    artifact.value = json.dumps(plan())
    tool.explain = AsyncMock(side_effect=[ExplainPlanError('relation "nope" does not exist'), artifact])

    shapes = await read_plan_shapes(tool, ["SELECT * FROM nope", "SELECT * FROM orders"])

    assert shapes[0].error == 'relation "nope" does not exist' and shapes[0].fingerprint is None
    assert shapes[1].fingerprint == shape_fingerprint(shape_lines(plan()))

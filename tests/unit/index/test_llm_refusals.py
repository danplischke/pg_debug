"""The llm method reports the alternatives it was offered and did not choose, with their costs and why.

The model is stubbed: there is no model to call here.
"""

from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from pglast import parse_sql

pytest.importorskip("pydantic")

from pg_debug.db import SqlDriver  # noqa: E402
from pg_debug.index.llm import Index  # noqa: E402
from pg_debug.index.llm import IndexingAlternative  # noqa: E402
from pg_debug.index.llm import LLMOptimizerTool  # noqa: E402
from pg_debug.plans.tree import ExplainPlanArtifact  # noqa: E402
from pg_debug.plans.tree import PlanNode  # noqa: E402

QUERY = "select * from t where a = 1 and b = 2"

ON_A = {Index(table_name="t", columns=("a",))}
ON_AB = {Index(table_name="t", columns=("a", "b"))}
BAD = {Index(table_name="t", columns=("nope",))}

#: Cost of the query under each set of indexes, by their columns.
COSTS = {frozenset(): 1000.0, frozenset({("a",)}): 100.0, frozenset({("a", "b")}): 10.0}


@pytest.mark.asyncio
async def test_the_alternatives_not_chosen_are_reported_with_their_costs_and_why():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[])
    tool = LLMOptimizerTool(driver, max_no_progress_attempts=1)

    async def evaluate(_weights: Any, indexes: Any) -> float:
        key = frozenset(idx.columns for idx in indexes)
        if key not in COSTS:
            raise RuntimeError('column "nope" does not exist')
        return COSTS[key]

    client = MagicMock()
    # Round one offers (a), then (a, b), then one that cannot be planned; round
    # two offers (a) again, which does not beat (a, b), and the search stops.
    client.chat.completions.create.side_effect = [
        IndexingAlternative(alternatives=[ON_A, ON_AB, BAD]),
        IndexingAlternative(alternatives=[ON_A]),
    ]
    plan = ExplainPlanArtifact(
        value='{"Plan": {}}', plan_tree=PlanNode(node_type="Seq Scan", total_cost=1000.0, startup_cost=0.0, plan_rows=1, plan_width=4)
    )

    with (
        patch("pg_debug.index.llm._make_llm_client", return_value=client),
        patch("pg_debug.index.llm.ExplainPlanTool") as explain_cls,
        patch.object(tool, "_evaluate_configuration_cost", side_effect=evaluate),
        patch.object(tool, "_get_table_size", AsyncMock(return_value=10_000_000)),
        patch.object(tool, "_estimate_index_size_2", AsyncMock(return_value=1_000_000.0)),
    ):
        explain_cls.return_value.explain = AsyncMock(return_value=plan)
        queries: list[tuple[str, Any, float]] = [(QUERY, parse_sql(QUERY)[0].stmt, 1.0)]
        chosen, cost = await tool._generate_recommendations(queries)

    assert {idx.columns for idx in chosen} == {("a", "b")}
    assert cost == 10.0
    refused = {tuple(r["indexes"]): r for r in tool._refused_candidates}
    on_a = refused[(next(iter(ON_A)).to_index_definition().definition,)]
    assert on_a["execution_cost"] == 100.0
    assert on_a["baseline_cost"] == 1000.0
    assert on_a["index_size_bytes"] == 1_000_000
    assert "superseded" in on_a["reason"]
    bad = refused[(next(iter(BAD)).to_index_definition().definition,)]
    assert bad["reason"] == 'the planner could not cost it: column "nope" does not exist'
    assert len(tool._refused_candidates) == 2

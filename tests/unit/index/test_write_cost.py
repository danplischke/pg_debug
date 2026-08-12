"""The advisor weighs what an index costs to maintain, not only what it saves.

The weight matters more than the idea. Set too high the term vetoes indexes
that are plainly worth having; set to zero it is the old behaviour. These pin
down that it breaks ties without overruling a real improvement.
"""

import math
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.index.dta_calc import DatabaseTuningAdvisor
from pg_debug.index.index_opt_base import DEFAULT_WRITE_WEIGHT
from pg_debug.sql import SqlDriver


def driver():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[])
    return driver


def write_row(**overrides):
    cells = {"inserts": 1000, "updates": 200, "deletes": 50, "hot_updates": 150}
    return [SqlDriver.RowResult(cells={**cells, **overrides})]


@pytest.mark.asyncio
async def test_hot_updates_are_not_counted_against_an_index():
    """A HOT update rewrites the row without touching any index. Counting it
    would overstate the cost on exactly the tables PostgreSQL is already
    keeping cheap."""
    advisor = DatabaseTuningAdvisor(driver())
    advisor.sql_driver.execute_query = AsyncMock(return_value=write_row(updates=200, hot_updates=150))

    load = await advisor.write_load("orders")

    # 1000 inserts + 50 deletes + (200 - 150) non-HOT updates
    assert load["index_writes"] == 1100
    assert load["hot_ratio"] == 0.75


@pytest.mark.asyncio
async def test_a_table_of_pure_hot_updates_costs_an_index_nothing():
    advisor = DatabaseTuningAdvisor(driver())
    advisor.sql_driver.execute_query = AsyncMock(return_value=write_row(inserts=0, deletes=0, updates=500, hot_updates=500))

    assert (await advisor.write_load("orders"))["index_writes"] == 0


@pytest.mark.asyncio
async def test_the_write_load_is_read_once_per_table():
    advisor = DatabaseTuningAdvisor(driver())
    advisor.sql_driver.execute_query = AsyncMock(return_value=write_row())

    await advisor.write_load("orders")
    await advisor.write_load("orders")

    assert advisor.sql_driver.execute_query.await_count == 1


@pytest.mark.asyncio
async def test_a_server_that_will_not_report_writes_does_not_stop_the_search():
    advisor = DatabaseTuningAdvisor(driver())
    advisor.sql_driver.execute_query = AsyncMock(side_effect=RuntimeError("permission denied"))

    assert (await advisor.write_load("orders"))["index_writes"] == 0


def test_the_weight_leaves_a_real_improvement_intact():
    """The term moves the objective by log(2) when the first index is added to
    a table, whatever the write volume. Weighted at 1.0 that is 0.69, which
    outweighs the 0.22 a 20% time improvement is worth and vetoes the index —
    which is what happened when this was first tried."""
    first_index_penalty = math.log(2)
    twenty_percent_faster = math.log(100.0) - math.log(80.0)

    assert first_index_penalty > twenty_percent_faster, "at weight 1.0 the term would veto it"
    assert DEFAULT_WRITE_WEIGHT * first_index_penalty < twenty_percent_faster, "at the default it must not"


def test_the_term_prefers_the_quieter_table_between_equal_candidates():
    """Two candidates of identical benefit: the one on the table with fewer
    index-touching writes should win."""
    base = 1_000 + 1_000_000  # a quiet table and a busy one
    on_quiet = math.log(base + 1_000)
    on_busy = math.log(base + 1_000_000)

    assert DEFAULT_WRITE_WEIGHT * on_quiet < DEFAULT_WRITE_WEIGHT * on_busy


def test_the_term_can_be_switched_off_entirely():
    advisor = DatabaseTuningAdvisor(driver(), write_weight=0.0)

    assert advisor.write_weight == 0.0


# -- Reporting --------------------------------------------------------------


class FakeRecommendation:
    def __init__(self, write_load):
        self.table = "orders"
        self.write_load = write_load


def test_a_table_with_no_recorded_writes_reports_no_cost():
    """Silence beats a confident zero when the counters were reset."""
    from pg_debug.index.presentation import TextPresentation

    assert TextPresentation._describe_write_cost(FakeRecommendation({"index_writes": 0})) is None
    assert TextPresentation._describe_write_cost(FakeRecommendation({})) is None


def test_the_reported_cost_carries_the_counters_it_came_from():
    from pg_debug.index.presentation import TextPresentation

    cost = TextPresentation._describe_write_cost(
        FakeRecommendation({"index_writes": 5100, "inserts": 5000, "updates": 0, "deletes": 100, "hot_ratio": 0.0})
    )

    assert cost is not None
    assert cost["index_touching_writes"] == 5100
    assert "5,100 writes recorded on orders" in cost["note"]
    assert "hot_update_warning" not in cost


def test_a_mostly_hot_table_is_warned_about_losing_that():
    """Adding an index over a column those updates change gives up HOT, which
    costs more than the maintenance itself."""
    from pg_debug.index.presentation import TextPresentation

    cost = TextPresentation._describe_write_cost(
        FakeRecommendation({"index_writes": 1000, "inserts": 0, "updates": 4000, "deletes": 0, "hot_ratio": 0.75})
    )

    assert cost is not None
    assert "75% of updates to orders are currently HOT" in cost["hot_update_warning"]

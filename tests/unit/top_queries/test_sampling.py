"""Delta sampling of pg_stat_statements counters."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.errors import InvalidArgumentError
from pg_debug.sql import QUERY_MARKER
from pg_debug.sql import SqlDriver
from pg_debug.top_queries import TopQueriesCalc


def snapshot(*entries):
    """Build a snapshot keyed the way _counter_snapshot keys it."""
    defaults = {
        "calls": 0,
        "total_exec_time": 0.0,
        "rows": 0,
        "shared_blks_hit": 0,
        "shared_blks_read": 0,
        "shared_blks_dirtied": 0,
        "wal_bytes": 0,
    }
    return {(entry["queryid"], 10, 20): {**defaults, **entry} for entry in entries}


def entry(queryid, query, **counters):
    return {"queryid": queryid, "query": query, **counters}


# -- Diffing ----------------------------------------------------------------


def test_counters_are_reported_as_the_difference():
    before = snapshot(entry(1, "SELECT 1", calls=100, total_exec_time=500.0, rows=100))
    after = snapshot(entry(1, "SELECT 1", calls=130, total_exec_time=650.0, rows=130))

    (delta,) = TopQueriesCalc._diff_snapshots(before, after)

    assert delta["calls"] == 30
    assert delta["total_exec_time"] == 150.0
    assert delta["rows"] == 30


def test_mean_is_recomputed_over_the_interval():
    before = snapshot(entry(1, "SELECT 1", calls=100, total_exec_time=1000.0))
    after = snapshot(entry(1, "SELECT 1", calls=110, total_exec_time=1050.0))

    (delta,) = TopQueriesCalc._diff_snapshots(before, after)

    # 50ms over 10 calls, not the cumulative 9.5ms average.
    assert delta["mean_exec_time"] == pytest.approx(5.0)


def test_queries_that_did_not_run_are_dropped():
    before = snapshot(entry(1, "SELECT 1", calls=100), entry(2, "SELECT 2", calls=5))
    after = snapshot(entry(1, "SELECT 1", calls=101), entry(2, "SELECT 2", calls=5))

    assert [d["queryid"] for d in TopQueriesCalc._diff_snapshots(before, after)] == [1]


def test_a_query_new_in_the_second_snapshot_counts_from_zero():
    before = snapshot()
    after = snapshot(entry(1, "SELECT 1", calls=7, total_exec_time=70.0))

    (delta,) = TopQueriesCalc._diff_snapshots(before, after)

    assert delta["calls"] == 7
    assert delta["total_exec_time"] == 70.0


def test_a_reset_between_snapshots_is_measured_from_zero():
    """Counters going backwards mean the view was reset or the entry reused."""
    before = snapshot(entry(1, "SELECT 1", calls=100, total_exec_time=500.0))
    after = snapshot(entry(1, "SELECT 1", calls=3, total_exec_time=9.0))

    (delta,) = TopQueriesCalc._diff_snapshots(before, after)

    assert delta["calls"] == 3
    assert delta["total_exec_time"] == 9.0


def test_null_counters_are_treated_as_zero():
    before = snapshot(entry(1, "SELECT 1", calls=1, wal_bytes=None))
    after = snapshot(entry(1, "SELECT 1", calls=2, wal_bytes=None))

    (delta,) = TopQueriesCalc._diff_snapshots(before, after)

    assert delta["wal_bytes"] == 0


# -- Filtering our own footprint --------------------------------------------


def test_the_snapshot_query_is_not_reported():
    after = snapshot(entry(1, f"{QUERY_MARKER} SELECT * FROM pg_stat_statements", calls=2))

    assert TopQueriesCalc._diff_snapshots(snapshot(), after) == []


@pytest.mark.parametrize("statement", ["BEGIN", "commit", "ROLLBACK;", "BEGIN TRANSACTION READ ONLY", "SAVEPOINT s1"])
def test_transaction_control_is_not_reported(statement):
    after = snapshot(entry(1, statement, calls=2))

    assert TopQueriesCalc._diff_snapshots(snapshot(), after) == []


def test_ordinary_queries_survive_filtering():
    after = snapshot(entry(1, "SELECT * FROM pg_stat_statements", calls=2))

    assert len(TopQueriesCalc._diff_snapshots(snapshot(), after)) == 1


# -- Ranking ----------------------------------------------------------------


def deltas(*rows):
    defaults = {"shared_blks_hit": 0, "shared_blks_read": 0, "shared_blks_dirtied": 0, "wal_bytes": 0}
    return [{**defaults, **row} for row in rows]


def test_ranking_by_total_time():
    rows = deltas(
        {"query": "a", "total_exec_time": 10.0, "mean_exec_time": 1.0},
        {"query": "b", "total_exec_time": 90.0, "mean_exec_time": 0.1},
    )

    assert [r["query"] for r in TopQueriesCalc._rank(rows, "total", 10)] == ["b", "a"]


def test_ranking_by_mean_time():
    rows = deltas(
        {"query": "a", "total_exec_time": 10.0, "mean_exec_time": 1.0},
        {"query": "b", "total_exec_time": 90.0, "mean_exec_time": 0.1},
    )

    assert [r["query"] for r in TopQueriesCalc._rank(rows, "mean", 10)] == ["a", "b"]


def test_ranking_by_resources_surfaces_an_io_heavy_query():
    """A query can dominate I/O without dominating time."""
    rows = deltas(
        {"query": "cpu", "total_exec_time": 100.0, "mean_exec_time": 1.0, "shared_blks_read": 1},
        {"query": "io", "total_exec_time": 1.0, "mean_exec_time": 0.1, "shared_blks_read": 10_000},
    )

    ranked = TopQueriesCalc._rank(rows, "resources", 10)

    assert [r["query"] for r in ranked] == ["io", "cpu"]
    assert ranked[0]["resource_fraction"] == pytest.approx(1.0, abs=0.01)


def test_ranking_applies_the_limit():
    rows = deltas(*({"query": str(i), "total_exec_time": float(i), "mean_exec_time": 1.0} for i in range(10)))

    assert len(TopQueriesCalc._rank(rows, "total", 3)) == 3


def test_ranking_an_empty_sample():
    assert TopQueriesCalc._rank([], "resources", 10) == []


# -- Sampling flow ----------------------------------------------------------


@pytest.mark.asyncio
async def test_sample_rejects_a_non_positive_interval():
    calc = TopQueriesCalc(MagicMock(spec=SqlDriver))

    with pytest.raises(InvalidArgumentError, match="greater than zero"):
        await calc.sample(0)


@pytest.mark.asyncio
async def test_sample_waits_between_snapshots():
    calc = TopQueriesCalc(MagicMock(spec=SqlDriver))
    snapshots = [
        snapshot(entry(1, "SELECT 1", calls=1, total_exec_time=1.0)),
        snapshot(entry(1, "SELECT 1", calls=5, total_exec_time=9.0)),
    ]

    with (
        patch.object(TopQueriesCalc, "_require_pg_stat_statements", AsyncMock()),
        patch.object(TopQueriesCalc, "_counter_snapshot", AsyncMock(side_effect=snapshots)),
        patch("pg_debug.top_queries.top_queries_calc.asyncio.sleep", AsyncMock()) as sleep,
    ):
        (result,) = await calc.sample(2.5)

    sleep.assert_awaited_once_with(2.5)
    assert result["calls"] == 4
    assert result["total_exec_time"] == 8.0

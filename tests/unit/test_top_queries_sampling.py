"""Delta sampling of pg_stat_statements counters."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import QUERY_MARKER
from pg_debug.db import SqlDriver
from pg_debug.db.privileges import HIDDEN_QUERY_TEXT
from pg_debug.db.window import CountingWindow
from pg_debug.errors import InvalidArgumentError
from pg_debug.top_queries import ChangeRule
from pg_debug.top_queries import QuerySample
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
    return {(entry["queryid"], 10, 20): {"userid": 10, "dbid": 20, **defaults, **entry} for entry in entries}


def rows(*entries):
    """Build a snapshot as _counter_snapshot reads it: one row per pg_stat_statements entry."""
    return list(snapshot(*entries).values())


def entry(queryid, query, **counters):
    return {"queryid": queryid, "query": query, **counters}


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def window(days=None, seconds=None, evicted=None):
    """pg_stat_statements' counting window at the second snapshot; unknown with neither."""
    if days is None and seconds is None:
        return CountingWindow(since=None, now=NOW)
    span = timedelta(days=days or 0, seconds=seconds or 0)
    return CountingWindow(since=NOW - span, now=NOW, began_with="reset", evicted=evicted)


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
        rows(entry(1, "SELECT 1", calls=1, total_exec_time=1.0)),
        rows(entry(1, "SELECT 1", calls=5, total_exec_time=9.0)),
    ]

    with (
        patch.object(TopQueriesCalc, "_require_pg_stat_statements", AsyncMock()),
        patch.object(TopQueriesCalc, "_counter_snapshot", AsyncMock(side_effect=snapshots)),
        patch.object(TopQueriesCalc, "_statement_window", AsyncMock(return_value=window(days=30))),
        patch("pg_debug.top_queries.asyncio.sleep", AsyncMock()) as sleep,
    ):
        (result,) = await calc.sample(2.5)

    sleep.assert_awaited_once_with(2.5)
    assert result["calls"] == 4
    assert result["total_exec_time"] == 8.0


# -- Statements whose queryid is withheld -------------------------------------


def withheld(userid=10, **counters):
    """Another role's statement, as pg_stat_statements shows it to a role without pg_read_all_stats."""
    return {**snapshot(entry(None, HIDDEN_QUERY_TEXT, **counters))[(None, 10, 20)], "userid": userid}


async def measure(before, after, counted=None, **kwargs) -> QuerySample:
    calc = TopQueriesCalc(MagicMock(spec=SqlDriver))
    with (
        patch.object(TopQueriesCalc, "_require_pg_stat_statements", AsyncMock()),
        patch.object(TopQueriesCalc, "_counter_snapshot", AsyncMock(side_effect=[before, after])),
        patch.object(TopQueriesCalc, "_statement_window", AsyncMock(return_value=counted or window(days=30))),
        patch("pg_debug.top_queries.asyncio.sleep", AsyncMock()),
    ):
        return await calc.measure(1, **kwargs)


@pytest.mark.asyncio
async def test_statements_whose_queryid_is_withheld_are_counted_rather_than_ranked_as_one():
    """The reported failure: keyed by (queryid, userid, dbid), every withheld
    statement of a role in a database was one key, so the ranking showed one
    row with one statement's counters, and took a cumulative total for
    activity during the interval."""
    before = [withheld(calls=100, total_exec_time=500.0), withheld(calls=7, total_exec_time=2.0), *rows(entry(1, "SELECT 1", calls=1))]
    after = [withheld(calls=130, total_exec_time=650.0), withheld(calls=9, total_exec_time=3.0), *rows(entry(1, "SELECT 1", calls=3))]

    sample = await measure(before, after, sort_by="total")

    assert [row["queryid"] for row in sample.queries] == [1]
    assert sample.withheld == 2


@pytest.mark.asyncio
async def test_withheld_statements_that_did_not_run_are_not_counted():
    """One that did not run is in both snapshots with the same counters."""
    idle = [withheld(calls=5, total_exec_time=1.5), withheld(userid=11, calls=5, total_exec_time=1.5)]

    assert (await measure(idle, list(idle))).withheld == 0


@pytest.mark.asyncio
async def test_a_withheld_statement_new_in_the_interval_is_counted_once_it_has_run():
    """An entry is made when a statement is parsed, before it has run."""
    sample = await measure([], [withheld(calls=1, total_exec_time=0.3), withheld(calls=0)])

    assert sample.withheld == 1


@pytest.mark.asyncio
async def test_a_withheld_statement_evicted_during_the_interval_is_not_counted():
    sample = await measure([withheld(calls=4), withheld(calls=9)], [withheld(calls=9)])

    assert sample.withheld == 0


@pytest.mark.asyncio
async def test_a_role_shown_everything_has_nothing_left_out():
    sample = await measure(rows(entry(1, "SELECT 1", calls=1)), rows(entry(1, "SELECT 1", calls=4)))

    assert sample == QuerySample(queries=sample.queries, withheld=0, statement_window=window(days=30).to_dict())
    assert sample.queries[0]["calls"] == 3


# -- New and changed statements -------------------------------------------------


@pytest.mark.asyncio
async def test_a_statement_new_in_the_sample_is_flagged_as_a_candidate():
    before = rows(entry(1, "SELECT 1", calls=10, total_exec_time=10.0))
    after = rows(entry(1, "SELECT 1", calls=11, total_exec_time=11.0), entry(2, "SELECT 2", calls=3, total_exec_time=3.0))

    sample = await measure(before, after)

    changes = {row["queryid"]: row["change"] for row in sample.queries}
    assert changes[1] is None
    assert changes[2]["kind"] == "new"
    assert changes[2]["confidence"] == "candidate"
    assert sample.changes_refused is None


@pytest.mark.asyncio
async def test_a_new_statement_says_evictions_may_explain_it():
    sample = await measure([], rows(entry(2, "SELECT 2", calls=3, total_exec_time=3.0)), counted=window(days=30, evicted=12))

    assert "evicted statements 12 times" in sample.queries[0]["change"]["reason"]


@pytest.mark.asyncio
async def test_a_statement_whose_mean_moved_sharply_is_flagged_slower():
    """Mean 1ms over 1000 calls before the sample, 5ms a call in it."""
    before = rows(entry(1, "SELECT 1", calls=1000, total_exec_time=1000.0))
    after = rows(entry(1, "SELECT 1", calls=1010, total_exec_time=1050.0))

    (row,) = (await measure(before, after)).queries

    assert row["change"]["kind"] == "slower"
    assert row["change"]["ratio"] == pytest.approx(5.0)
    assert row["change"]["previous_mean_exec_time"] == pytest.approx(1.0)
    assert row["change"]["sample_mean_exec_time"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_a_statement_whose_mean_fell_sharply_is_flagged_faster():
    before = rows(entry(1, "SELECT 1", calls=100, total_exec_time=1000.0))
    after = rows(entry(1, "SELECT 1", calls=110, total_exec_time=1010.0))

    (row,) = (await measure(before, after)).queries

    assert row["change"]["kind"] == "faster"


@pytest.mark.asyncio
async def test_a_large_statement_that_runs_as_it_always_has_is_not_changed():
    """Large is not changed: the most expensive statement may always have been."""
    before = rows(entry(1, "SELECT 1", calls=1000, total_exec_time=100_000.0))
    after = rows(entry(1, "SELECT 1", calls=1100, total_exec_time=115_000.0))

    (row,) = (await measure(before, after)).queries

    assert row["change"] is None


@pytest.mark.asyncio
async def test_nothing_is_judged_when_the_record_is_not_much_longer_than_the_sample():
    """Reset seconds ago: every statement is new, and no mean is a history."""
    sample = await measure([], rows(entry(2, "SELECT 2", calls=3, total_exec_time=3.0)), counted=window(seconds=5))

    assert sample.queries[0]["change"] is None
    assert sample.changes_refused is not None
    assert "counted for 4s before the sample" in sample.changes_refused
    assert "(10s) is needed" in sample.changes_refused


@pytest.mark.asyncio
async def test_nothing_is_judged_when_the_record_cannot_be_dated():
    """pg_stat_statements before 1.9 records no reset."""
    sample = await measure([], rows(entry(2, "SELECT 2", calls=3, total_exec_time=3.0)), counted=window())

    assert sample.queries[0]["change"] is None
    assert "before 1.9" in (sample.changes_refused or "")


def test_a_statement_whose_counters_went_backwards_is_not_judged():
    rule = ChangeRule(sample_seconds=1, window=window(days=30))
    before = snapshot(entry(1, "SELECT 1", calls=100, total_exec_time=100.0))
    after = snapshot(entry(1, "SELECT 1", calls=4, total_exec_time=40.0))

    (delta,) = TopQueriesCalc._diff_snapshots(before, after, rule=rule)

    assert delta["change"]["kind"] is None
    assert "started again" in delta["change"]["refused"]


def test_a_statement_recorded_again_recently_is_not_judged_by_its_short_record():
    """pg_stat_statements 1.11 dates each entry: one evicted and recorded again seconds ago has seconds of history."""
    rule = ChangeRule(sample_seconds=1, window=window(days=30))
    since = NOW - timedelta(seconds=6)
    before = snapshot(entry(1, "SELECT 1", calls=10, total_exec_time=10.0, stats_since=since))
    after = snapshot(entry(1, "SELECT 1", calls=20, total_exec_time=100.0, stats_since=since))

    (delta,) = TopQueriesCalc._diff_snapshots(before, after, rule=rule)

    assert delta["change"]["kind"] is None
    assert "5s long" in delta["change"]["refused"]

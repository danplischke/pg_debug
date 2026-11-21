"""top-queries --save and --compare: a snapshot kept in a file, and what ran since it."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from pg_debug.errors import InvalidArgumentError
from pg_debug.top_queries import QuerySnapshot
from pg_debug.top_queries import TopQueriesCalc

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def row(queryid, query, calls, total, **counters):
    return {
        "queryid": queryid,
        "userid": 10,
        "dbid": 5,
        "toplevel": True,
        "stats_since": None,
        "query": query,
        "calls": calls,
        "total_exec_time": total,
        "rows": calls,
        "shared_blks_hit": 0,
        "shared_blks_read": 0,
        "shared_blks_dirtied": 0,
        "wal_bytes": 0,
        **counters,
    }


def snap(at, rows, reset=T0 - timedelta(days=2), started=T0 - timedelta(days=30), database="app"):
    return QuerySnapshot(taken_at=at, database=database, server_version=16, statements_since=reset, server_started=started, rows=rows)


async def compare(before, after, **options):
    calc = TopQueriesCalc(sql_driver=None)  # type: ignore[arg-type]
    with patch.object(TopQueriesCalc, "snapshot", AsyncMock(return_value=after)):
        return await calc.compare(before, **options)


def test_a_snapshot_survives_its_file(tmp_path):
    original = snap(T0, [row(1, "SELECT 1 /*route='%2Fa'*/", 3, 1.5, wal_bytes=Decimal("4096"), stats_since=T0)])
    original.save(tmp_path / "s.json")
    loaded = QuerySnapshot.load(tmp_path / "s.json")

    assert loaded == original
    assert loaded.rows[0]["wal_bytes"] == 4096, "a numeric counter is a number in the file, not a string"


def test_a_file_that_is_not_a_snapshot_is_refused(tmp_path):
    path = tmp_path / "x.json"
    path.write_text('{"plans": []}')
    with pytest.raises(InvalidArgumentError, match="not a top-queries snapshot"):
        QuerySnapshot.load(path)
    with pytest.raises(InvalidArgumentError, match="Could not read"):
        QuerySnapshot.load(tmp_path / "missing.json")


@pytest.mark.asyncio
async def test_the_counters_since_the_snapshot_are_reported_with_what_changed():
    before = snap(T0, [row(1, "SELECT a", 1000, 1000.0), row(2, "SELECT b", 10, 10.0)])
    after = snap(
        T0 + timedelta(hours=1),
        [row(1, "SELECT a", 1100, 1600.0), row(2, "SELECT b", 10, 10.0), row(3, "SELECT c /*controller='new'*/", 5, 50.0)],
    )
    sample = await compare(before, after, sort_by="total")

    by_id = {q["queryid"]: q for q in sample.queries}
    assert set(by_id) == {1, 3}, "b did not run since"
    assert (by_id[1]["calls"], by_id[1]["total_exec_time"]) == (100, 600.0)
    assert by_id[1]["change"]["kind"] == "slower"
    assert by_id[1]["change"]["ratio"] == pytest.approx(6.0)
    assert by_id[3]["change"]["kind"] == "new"
    assert by_id[3]["tags"] == {"controller": "new"}
    assert sample.since == T0.isoformat()
    assert sample.changes_refused is None
    assert sample.notes == []


@pytest.mark.asyncio
async def test_a_reset_after_the_snapshot_measures_from_the_reset_and_judges_nothing():
    """1000 calls before, a reset, 1100 since: subtracting would report 100."""
    before = snap(T0, [row(1, "SELECT a", 1000, 1000.0)])
    after = snap(T0 + timedelta(hours=1), [row(1, "SELECT a", 1100, 1100.0)], reset=T0 + timedelta(minutes=5))
    sample = await compare(before, after)

    [query] = sample.queries
    assert query["calls"] == 1100
    assert query["change"] is None
    assert "reset" in sample.notes[0]


@pytest.mark.asyncio
async def test_a_record_before_the_snapshot_shorter_than_the_time_since_is_not_judged():
    before = snap(T0, [row(1, "SELECT a", 10, 10.0)], reset=T0 - timedelta(minutes=10))
    after = snap(T0 + timedelta(hours=1), [row(1, "SELECT a", 20, 200.0)], reset=T0 - timedelta(minutes=10))
    sample = await compare(before, after)

    assert sample.queries[0]["change"] is None
    assert "not judged" in (sample.changes_refused or "")


@pytest.mark.asyncio
async def test_a_restart_since_the_snapshot_is_said():
    before = snap(T0, [row(1, "SELECT a", 10, 10.0)])
    after = snap(T0 + timedelta(hours=1), [row(1, "SELECT a", 20, 20.0)], started=T0 + timedelta(minutes=30))
    sample = await compare(before, after)
    assert any("restarted" in note for note in sample.notes)


@pytest.mark.asyncio
async def test_another_database_or_a_later_snapshot_is_refused():
    with pytest.raises(InvalidArgumentError, match="taken in database 'other'"):
        await compare(snap(T0, [], database="other"), snap(T0 + timedelta(hours=1), []))
    with pytest.raises(InvalidArgumentError, match="not before now"):
        await compare(snap(T0, []), snap(T0 - timedelta(hours=1), []))

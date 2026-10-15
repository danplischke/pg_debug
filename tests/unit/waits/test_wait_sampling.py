"""Sampling pg_stat_activity into a wait profile, without an extension."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.db.privileges import HIDDEN_QUERY_TEXT
from pg_debug.errors import InvalidArgumentError
from pg_debug.waits import WaitSampler
from pg_debug.waits.sampling import AZURE_COLUMNS_SQL
from pg_debug.waits.sampling import AZURE_LAST_INTERVAL_SQL
from pg_debug.waits.sampling import CPU
from pg_debug.waits.sampling import SETUP_SQL
from pg_debug.waits.sampling import _wait_sampling_profile_sql
from pg_debug.waits.sampling import build_profile
from pg_debug.waits.sampling import check_sampling
from pg_debug.waits.sampling import measure_timing
from pg_debug.waits.sampling import normalize_query
from pg_debug.waits.sampling import sample_sql
from pg_debug.waits.sampling import wait_sampling_delta

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def at(*seconds: float) -> list[datetime]:
    return [T0 + timedelta(seconds=s) for s in seconds]


def backend(pid, wait_type=None, wait=None, query="SELECT 1", query_id=None, backend_type="client backend"):
    return {
        "pid": pid,
        "datname": "app",
        "usename": "app",
        "backend_type": backend_type,
        "state": "active" if backend_type == "client backend" else None,
        "wait_event_type": wait_type,
        "wait_event": wait,
        "query_id": query_id,
        "query": query,
    }


# -- How many samples ---------------------------------------------------------


@pytest.mark.parametrize(("seconds", "interval", "count"), [(10, 0.5, 20), (1, 0.3, 4), (2, 1, 2), (0.05, 0.01, 5)])
def test_samples_are_taken_from_the_start_up_to_the_duration(seconds, interval, count):
    assert check_sampling(seconds, interval) == count


@pytest.mark.parametrize(
    ("seconds", "interval", "says"),
    [
        (0, 0.5, "greater than zero"),
        (-1, 0.5, "greater than zero"),
        (5, 0.001, "at least 0.01"),
        (1, 1, "not shorter than the duration"),
        (1, 2, "not shorter than the duration"),
        (86400 * 30, 0.01, "at most"),
    ],
)
def test_a_sampling_that_could_not_be_done_is_refused_with_why(seconds, interval, says):
    with pytest.raises(InvalidArgumentError, match=says):
        check_sampling(seconds, interval)


# -- The interval achieved ---------------------------------------------------


def test_the_interval_is_measured_from_the_server_clock_not_the_request():
    timing = measure_timing(at(0, 0.6, 1.1, 1.8), seconds=2, interval=0.5)

    assert timing.samples == 4
    assert timing.requested_interval_seconds == 0.5
    assert timing.achieved_interval_seconds == pytest.approx(0.6)
    assert timing.shortest_gap_seconds == pytest.approx(0.5)
    assert timing.longest_gap_seconds == pytest.approx(0.7)
    assert timing.span_seconds == pytest.approx(1.8)


# -- The profile -------------------------------------------------------------


def test_average_active_sessions_split_by_wait_and_by_query():
    samples = [
        [backend(1, "Lock", "transactionid", "UPDATE t SET n = n + 1 WHERE id = 1", 11), backend(2, None, None, "SELECT 2", 22)],
        [backend(1, "Lock", "transactionid", "UPDATE t SET n = n + 1 WHERE id = 1", 11)],
        [],
        [backend(1, "Lock", "transactionid", "UPDATE t SET n = n + 1 WHERE id = 1", 11), backend(3, "IO", "DataFileRead", "SELECT 3", 33)],
    ]

    profile = build_profile(samples, at(0, 1, 2, 3), 4, 1, has_query_id=True)

    assert profile.active_sessions == 1.25  # 5 session-samples over 4 samples
    assert profile.active_sessions_max == 2
    assert profile.active_client_sessions == 1.25
    waits = {wait["wait"]: wait for wait in profile.waits}
    assert waits["Lock:transactionid"]["active_sessions"] == 0.75
    assert waits["Lock:transactionid"]["share"] == 0.6
    assert waits[CPU]["samples"] == 1
    assert all(wait["confidence"] == "candidate" for wait in profile.waits)
    top = profile.queries[0]
    assert top["query_id"] == 11
    assert top["active_sessions"] == 0.75
    assert top["waits"][0] == {"wait": "Lock:transactionid", "samples": 3, "share_of_query": 1.0}
    assert top["confidence"] == "candidate"
    assert profile.query_grouping == "query_id"
    assert profile.timing.samples == 4
    assert any("CPU is an active session reporting no wait event" in note for note in profile.notes)
    assert "each sample is exact; the profile is a sample" in profile.to_dict()["exactness"]


def test_without_a_query_id_queries_are_grouped_by_their_text_without_constants():
    samples = [
        [backend(1, query="SELECT * FROM t WHERE id = 1"), backend(2, query="SELECT * FROM t WHERE id = 22")],
        [backend(1, query="SELECT * FROM t WHERE name = 'x'")],
    ]

    profile = build_profile(samples, at(0, 1), 2, 1, has_query_id=False)

    assert profile.query_grouping == "normalized_text"
    assert profile.queries[0]["query"] == "SELECT * FROM t WHERE id = ?"
    assert profile.queries[0]["samples"] == 2
    assert profile.queries[0]["grouped_by"] == "query"


def test_some_queries_with_an_id_and_some_without_is_mixed():
    samples = [[backend(1, query="SELECT 1", query_id=5), backend(2, "Lock", "relation", query="SELECT * FROM t")]]

    profile = build_profile(samples, at(0), 1, 0.5, has_query_id=True)

    assert profile.query_grouping == "mixed"


def test_server_processes_are_listed_by_their_type():
    samples = [[backend(9, "IO", "DataFileWrite", query="", backend_type="checkpointer")]]

    profile = build_profile(samples, at(0), 1, 0.5, has_query_id=True)

    assert profile.queries[0]["grouped_by"] == "backend_type"
    assert profile.queries[0]["backend_type"] == "checkpointer"
    assert profile.active_client_sessions == 0
    assert profile.active_sessions == 1


def test_withheld_backends_are_counted_apart_and_in_no_figure():
    samples = [
        [backend(1, query=HIDDEN_QUERY_TEXT), backend(2, query=HIDDEN_QUERY_TEXT), backend(3)],
        [backend(1, query=HIDDEN_QUERY_TEXT)],
    ]

    profile = build_profile(samples, at(0, 1), 2, 1, has_query_id=True)

    assert profile.withheld_per_sample == 2
    assert profile.active_sessions == 0.5
    assert [query["samples"] for query in profile.queries] == [1]


def test_a_gap_much_longer_than_asked_for_is_said():
    profile = build_profile([[], [], []], at(0, 0.5, 3), 3, 0.5, has_query_id=True)

    assert any("did not look at the server during it" in note for note in profile.notes)


@pytest.mark.parametrize(
    ("text", "normalized"),
    [
        ("SELECT * FROM t WHERE id = 42", "SELECT * FROM t WHERE id = ?"),
        ("SELECT * FROM t WHERE name = 'it''s'  AND x = -1.5", "SELECT * FROM t WHERE name = ? AND x = ?"),
        ("SELECT * FROM t WHERE id IN (1, 2, 3)", "SELECT * FROM t WHERE id IN (?)"),
        ("SELECT * FROM t2 WHERE id = $1", "SELECT * FROM t2 WHERE id = $1"),
        ("SELECT\n  a\nFROM t", "SELECT a FROM t"),
        (None, ""),
    ],
)
def test_query_text_is_normalized(text, normalized):
    assert normalize_query(text) == normalized


# -- Sampling ----------------------------------------------------------------


def sampling_driver(samples: list[list[dict[str, Any]]], setup: dict[str, Any] | None = None, profiles: list[Any] | None = None) -> MagicMock:
    """A driver that answers the setup query, then each sample in turn with the server's time."""
    driver = MagicMock(spec=SqlDriver)
    remaining = list(samples)
    readings = list(profiles or [])
    clock = iter(at(*[i * 0.25 for i in range(100)]))

    async def answer(query, *args, **kwargs):
        if query == SETUP_SQL:
            return [SqlDriver.RowResult(cells={"compute_query_id": "auto", "track_activity_query_size": "1kB", **(setup or {})})]
        if "pg_wait_sampling_profile" in query:
            return [SqlDriver.RowResult(cells=row) for row in readings.pop(0)]
        if "pg_stat_activity" in query:
            rows = remaining.pop(0)
            when = next(clock)
            if not rows:
                return [SqlDriver.RowResult(cells={"sampled_at": when, "pid": None})]
            return [SqlDriver.RowResult(cells={"sampled_at": when, **row}) for row in rows]
        raise AssertionError(f"unexpected query: {query}")

    driver.execute_query = AsyncMock(side_effect=answer)
    return driver


@pytest.mark.asyncio
async def test_each_sample_is_one_statement_and_leaves_out_our_own_backend():
    driver = sampling_driver([[backend(1, "Lock", "relation")], [], [backend(1, "Lock", "relation")]])

    with patch("pg_debug.waits.sampling.get_postgres_version", AsyncMock(return_value=16)):
        profile = await WaitSampler(driver).sample(0.03, interval=0.01)

    sample_calls = [call for call in driver.execute_query.call_args_list if "pg_stat_activity" in call.args[0]]
    assert len(sample_calls) == 3
    assert "pg_backend_pid()" in sample_calls[0].args[0]
    assert "a.query_id" in sample_calls[0].args[0]
    assert profile.timing.samples == 3
    # Measured from the times the server reported, not the 0.01s asked for.
    assert profile.timing.achieved_interval_seconds == pytest.approx(0.25)
    assert profile.active_sessions == pytest.approx(0.667, abs=0.001)
    assert profile.extension is None


@pytest.mark.asyncio
async def test_before_14_there_is_no_query_id_and_the_profile_says_why():
    driver = sampling_driver([[backend(1, query="SELECT 1")], [backend(1, query="SELECT 2")]])

    with patch("pg_debug.waits.sampling.get_postgres_version", AsyncMock(return_value=13)):
        profile = await WaitSampler(driver).sample(0.02, interval=0.01)

    first = next(call.args[0] for call in driver.execute_query.call_args_list if "pg_stat_activity" in call.args[0])
    assert "NULL::bigint AS query_id" in first
    assert profile.query_grouping == "normalized_text"
    assert profile.query_grouping_reason is not None
    assert "PostgreSQL 13 does not report query_id" in profile.query_grouping_reason
    assert profile.queries[0]["samples"] == 2


@pytest.mark.asyncio
async def test_pg_wait_sampling_is_read_too_over_the_same_window():
    before = [{"read_at": T0, "event_type": "Lock", "event": "transactionid", "queryid": 7, "count": 100}]
    after = [
        {"read_at": T0 + timedelta(seconds=1), "event_type": "Lock", "event": "transactionid", "queryid": 7, "count": 150},
        {"read_at": T0 + timedelta(seconds=1), "event_type": "Client", "event": "ClientRead", "queryid": 0, "count": 100},
    ]
    driver = sampling_driver(
        [[backend(1, "Lock", "transactionid")], [backend(1, "Lock", "transactionid")]],
        setup={"pg_wait_sampling_schema": "public", "profile_period": "10"},
        profiles=[before, after],
    )

    with patch("pg_debug.waits.sampling.get_postgres_version", AsyncMock(return_value=16)):
        profile = await WaitSampler(driver).sample(0.02, interval=0.01)

    assert profile.source == "pg_stat_activity"
    extension = profile.extension
    assert extension is not None
    assert extension["source"] == "pg_wait_sampling_profile"
    assert extension["read"] is True
    # 1 second at one sample every 10ms is 100 samples per backend; 50 in a lock.
    assert extension["active_sessions"] == 0.5
    idle = next(wait for wait in extension["waits"] if wait["wait"] == "Client:ClientRead")
    assert idle["idle"] is True


def test_a_pg_wait_sampling_profile_reset_in_the_window_is_refused():
    before = [{"read_at": T0, "event_type": "IO", "event": "DataFileRead", "queryid": 1, "count": 500}]
    after = [{"read_at": T0 + timedelta(seconds=1), "event_type": "IO", "event": "DataFileRead", "queryid": 1, "count": 20}]

    result = wait_sampling_delta(before, after, 10, "on")

    assert result["read"] is False
    assert "reset" in result["reason"]


def test_a_pg_wait_sampling_that_could_not_be_read_says_why():
    result = wait_sampling_delta("reading public.pg_wait_sampling_profile failed: permission denied", None, 10, "on")

    assert result == {
        "source": "pg_wait_sampling_profile",
        "read": False,
        "reason": "reading public.pg_wait_sampling_profile failed: permission denied",
    }


@pytest.mark.asyncio
async def test_azure_query_store_of_another_shape_is_reported_not_read():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells={"columns": ["event", "event_type", "calls"]})])

    result = await WaitSampler(driver)._azure_last_interval()

    assert result["read"] is False
    assert "end_time, start_time" in result["reason"]
    assert driver.execute_query.await_count == 1


# -- Restricted mode ---------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [sample_sql(True), sample_sql(False), SETUP_SQL, AZURE_COLUMNS_SQL, AZURE_LAST_INTERVAL_SQL, _wait_sampling_profile_sql('"public"')],
)
def test_every_statement_the_sampler_runs_passes_restricted_mode(query):
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(query)

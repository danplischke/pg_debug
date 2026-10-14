"""Configuration findings, and the measurements that have to back them.

The point of this reviewer is that it argues from counters rather than from a
table of recommended values, so most of these tests are about it staying quiet
when the measurement does not support a complaint.
"""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.configuration import GOOD_CACHE_HIT_RATE
from pg_debug.configuration import MIN_BLOCK_READS
from pg_debug.configuration import MIN_TEMP_BYTES
from pg_debug.configuration import ConfigurationReviewer
from pg_debug.configuration import _checkpoints
from pg_debug.configuration import _durability
from pg_debug.configuration import _effective_cache_size
from pg_debug.configuration import _idle_transaction_timeout
from pg_debug.configuration import _random_page_cost
from pg_debug.configuration import _shared_buffers
from pg_debug.configuration import _slow_query_log
from pg_debug.configuration import _work_mem
from pg_debug.db import SqlDriver
from pg_debug.db import reset_postgres_version_cache
from pg_debug.severity import Severity


def setting(name, value, unit=None, source="default"):
    return {name: {"name": name, "setting": value, "unit": unit, "source": source, "boot_val": value}}


def healthy_measurements(**overrides):
    """A server with nothing wrong with it."""
    base = {
        "blks_read": 1_000,
        "blks_hit": 10_000_000,
        "temp_files": 0,
        "temp_bytes": 0,
        "database_bytes": 100 * 1024**2,
        "checkpoints_timed": 50,
        "checkpoints_requested": 2,
    }
    return {**base, **overrides}


# -- Durability -------------------------------------------------------------


def test_fsync_off_is_the_most_serious_thing_here():
    finding = _durability(setting("fsync", "off"), healthy_measurements())

    assert finding is not None
    assert finding.severity is Severity.HIGH
    assert finding.recommended == "on"


def test_full_page_writes_off_is_reported_too():
    assert _durability(setting("full_page_writes", "off"), healthy_measurements()) is not None


def test_durability_left_alone_when_it_is_on():
    assert _durability(setting("fsync", "on"), healthy_measurements()) is None


# -- work_mem ---------------------------------------------------------------


def test_work_mem_is_only_raised_when_queries_actually_spill():
    """A small work_mem that nothing has spilled against is not a problem."""
    assert _work_mem(setting("work_mem", "4096", "kB"), healthy_measurements()) is None


def test_work_mem_is_reported_with_what_was_spilled():
    finding = _work_mem(
        setting("work_mem", "4096", "kB"),
        healthy_measurements(temp_files=137, temp_bytes=380 * 1024**2),
    )

    assert finding is not None
    assert finding.severity is Severity.MEDIUM
    assert finding.evidence["temp_files"] == 137
    assert "per operation, not per query" in finding.detail, "raising it blindly is how a server runs out of memory"


def test_a_trivial_spill_is_not_worth_reconfiguring_for():
    measurements = healthy_measurements(temp_files=1, temp_bytes=MIN_TEMP_BYTES - 1)

    assert _work_mem(setting("work_mem", "4096", "kB"), measurements) is None


# -- Checkpoints ------------------------------------------------------------


def test_checkpoints_are_reported_when_requests_outnumber_the_timer():
    finding = _checkpoints(
        setting("max_wal_size", "1024", "MB"),
        healthy_measurements(checkpoints_timed=3, checkpoints_requested=16),
    )

    assert finding is not None
    assert finding.recommended == "2.0 GB"
    assert "an explicit CHECKPOINT command" in finding.detail, "a manual checkpoint counts as requested too"


def test_a_handful_of_checkpoints_says_nothing_either_way():
    measurements = healthy_measurements(checkpoints_timed=1, checkpoints_requested=3)

    assert _checkpoints(setting("max_wal_size", "1024", "MB"), measurements) is None


def test_checkpoints_on_the_timer_are_not_reported():
    measurements = healthy_measurements(checkpoints_timed=90, checkpoints_requested=4)

    assert _checkpoints(setting("max_wal_size", "1024", "MB"), measurements) is None


# -- shared_buffers ---------------------------------------------------------


def test_a_small_cache_that_is_working_is_left_alone():
    """The whole point: shared_buffers below the database size is only a
    problem when reads are actually missing."""
    measurements = healthy_measurements(blks_read=200_000, blks_hit=100_000_000, database_bytes=10 * 1024**3)

    assert _shared_buffers(setting("shared_buffers", "16384", "8kB"), measurements) is None


def test_a_cache_that_is_missing_is_reported_without_inventing_a_size():
    measurements = healthy_measurements(blks_read=5_000_000, blks_hit=5_000_000, database_bytes=10 * 1024**3)

    finding = _shared_buffers(setting("shared_buffers", "16384", "8kB"), measurements)

    assert finding is not None
    assert finding.recommended is None, "the machine's memory is not visible from here, so no number is offered"
    assert "cannot see how much memory" in finding.detail
    assert finding.evidence["cache_hit_rate"] == 0.5


def test_a_cold_server_is_not_judged():
    measurements = healthy_measurements(blks_read=MIN_BLOCK_READS - 1, blks_hit=0)

    assert _shared_buffers(setting("shared_buffers", "16384", "8kB"), measurements) is None


# -- Planner hints ----------------------------------------------------------


def test_effective_cache_size_is_only_mentioned_when_the_database_outgrew_it():
    small_database = healthy_measurements(database_bytes=1024**2)
    big_database = healthy_measurements(database_bytes=100 * 1024**3)

    assert _effective_cache_size(setting("effective_cache_size", "524288", "8kB"), small_database) is None
    assert _effective_cache_size(setting("effective_cache_size", "524288", "8kB"), big_database) is not None


def test_a_configured_effective_cache_size_is_left_alone():
    configured = setting("effective_cache_size", "524288", "8kB", source="configuration file")

    assert _effective_cache_size(configured, healthy_measurements(database_bytes=100 * 1024**3)) is None


def test_random_page_cost_is_only_questioned_when_reads_come_from_memory():
    cached = healthy_measurements(blks_read=1_000, blks_hit=10_000_000)
    uncached = healthy_measurements(blks_read=5_000_000, blks_hit=5_000_000)

    assert _random_page_cost(setting("random_page_cost", "4"), cached) is not None
    assert _random_page_cost(setting("random_page_cost", "4"), uncached) is None


def test_random_page_cost_admits_what_it_cannot_see():
    finding = _random_page_cost(setting("random_page_cost", "4"), healthy_measurements())

    assert finding is not None
    assert finding.recommended is None
    assert "cannot see what your storage is" in finding.detail


def test_a_tuned_random_page_cost_is_left_alone():
    tuned = setting("random_page_cost", "1.1", source="configuration file")

    assert _random_page_cost(tuned, healthy_measurements()) is None


# -- Observability ----------------------------------------------------------


def test_no_slow_query_log_is_reported():
    finding = _slow_query_log(setting("log_min_duration_statement", "-1", "ms"), healthy_measurements())

    assert finding is not None
    assert finding.recommended == "1000"


def test_a_configured_slow_query_log_is_left_alone():
    assert _slow_query_log(setting("log_min_duration_statement", "1000", "ms"), healthy_measurements()) is None


def test_no_idle_transaction_timeout_points_at_the_horizon_check():
    finding = _idle_transaction_timeout(setting("idle_in_transaction_session_timeout", "0", "ms"), healthy_measurements())

    assert finding is not None
    assert "Vacuum horizon" in finding.detail, "the two findings are about the same failure"


# -- The reviewer -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_server_with_nothing_measurably_wrong_reports_nothing():
    reset_postgres_version_cache()
    driver = MagicMock(spec=SqlDriver)

    async def answer(query, *args, **kwargs):
        if "pg_settings" in query:
            return [
                SqlDriver.RowResult(cells={"name": name, "setting": value, "unit": unit, "source": "configuration file", "boot_val": value})
                for name, value, unit in (
                    ("fsync", "on", None),
                    ("autovacuum", "on", None),
                    ("work_mem", "16384", "kB"),
                    ("log_min_duration_statement", "1000", "ms"),
                    ("idle_in_transaction_session_timeout", "60000", "ms"),
                    ("random_page_cost", "1.1", None),
                )
            ]
        if query.strip() == "SHOW server_version_num":
            return [SqlDriver.RowResult(cells={"server_version_num": "160004"})]
        return [SqlDriver.RowResult(cells=healthy_measurements())]

    driver.execute_query = AsyncMock(side_effect=answer)

    assert await ConfigurationReviewer(driver).review() == []


def test_the_hit_rate_threshold_is_a_share_not_a_percentage():
    """A rule reading 99 where it means 0.99 would fire on every server."""
    assert 0 < GOOD_CACHE_HIT_RATE < 1

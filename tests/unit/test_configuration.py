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
from pg_debug.platform import Basis
from pg_debug.platform import Platform
from pg_debug.platform import PlatformDetection
from pg_debug.platform import detect_platform
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


NEON = PlatformDetection(Platform.NEON, "the server has the setting neon.timeline_id", Basis.SERVER)


def test_neons_fsync_off_is_a_note_not_a_failure():
    """Neon runs every compute with fsync off; a commit is durable once its
    safekeepers hold the WAL, which a HIGH finding on every Neon database hid."""
    finding = _durability(setting("fsync", "off"), healthy_measurements(), NEON)

    assert finding is not None
    assert finding.severity is Severity.LOW, "low enough not to fail --exit-code"
    assert finding.recommended is None, "there is nothing to change"
    assert "quorum of its safekeepers" in finding.detail
    assert "neon.timeline_id" in finding.detail, "it says what it took the platform from"
    assert finding.evidence["platform"] == "neon"
    assert finding.evidence["platform_signal"] == NEON.signal


@pytest.mark.parametrize(
    "platform",
    [None, PlatformDetection(Platform.UNKNOWN, "nothing matched", Basis.NONE), PlatformDetection(Platform.SUPABASE, "a setting", Basis.SERVER)],
)
def test_fsync_off_anywhere_else_is_still_high(platform):
    finding = _durability(setting("fsync", "off"), healthy_measurements(), platform)

    assert finding is not None
    assert finding.severity is Severity.HIGH


def test_a_neon_host_excuses_fsync_as_the_setting_does():
    host = PlatformDetection(Platform.NEON, "the host ep-x.aws.neon.tech is under neon.tech", Basis.URL)

    finding = _durability(setting("fsync", "off"), healthy_measurements(), host)

    assert finding is not None
    assert finding.severity is Severity.LOW


def test_a_neon_role_alone_does_not_excuse_fsync():
    """A role can be on any server: a dump of a Neon project's roles restored on
    one's own cluster carries neon_superuser, and that must not turn the most
    serious finding there is into a note that passes --exit-code."""
    role = PlatformDetection(Platform.NEON, "the role neon_superuser exists", Basis.ROLE)

    finding = _durability(setting("fsync", "off"), healthy_measurements(), role)

    assert finding is not None
    assert finding.severity is Severity.HIGH
    assert finding.recommended == "on"
    assert "because the role neon_superuser exists" in finding.detail, "it says what it examined"
    assert "That is not enough to call fsync = off safe" in finding.detail, "and why it refused"
    assert finding.evidence["platform_signal"] == "the role neon_superuser exists"


@pytest.mark.asyncio
async def test_a_server_with_only_a_neon_role_keeps_fsync_high_end_to_end():
    """The case review found: no setting, a host that names nothing, and the role."""
    driver = MagicMock(spec=SqlDriver)
    probe = {
        "neon_timeline_id": False,
        "supautils_privileged_role": False,
        "aurora_version": False,
        "rds_extensions": False,
        "azure_extensions": False,
        "platform_roles": ["neon_superuser"],
    }
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=probe)])

    detection = await detect_platform(driver, "postgresql://postgres@db.internal.example.com/app")
    finding = _durability(setting("fsync", "off"), healthy_measurements(), detection)

    assert detection.platform is Platform.NEON
    assert finding is not None
    assert finding.severity is Severity.HIGH


def test_full_page_writes_off_on_neon_is_still_reported_high():
    """The note about fsync must not take the place of a finding that is not about it."""
    settings = {**setting("fsync", "off"), **setting("full_page_writes", "off")}

    finding = _durability(settings, healthy_measurements(), NEON)

    assert finding is not None
    assert finding.setting == "full_page_writes"
    assert finding.severity is Severity.HIGH


@pytest.mark.asyncio
async def test_the_reviewer_judges_fsync_by_the_platform_it_is_given():
    reset_postgres_version_cache()
    driver = MagicMock(spec=SqlDriver)

    async def answer(query, *args, **kwargs):
        if "pg_settings" in query:
            return [SqlDriver.RowResult(cells={"name": "fsync", "setting": "off", "unit": None, "source": "configuration file", "boot_val": "on"})]
        if query.strip() == "SHOW server_version":
            return [SqlDriver.RowResult(cells={"server_version": "16.4"})]
        return [SqlDriver.RowResult(cells=healthy_measurements())]

    driver.execute_query = AsyncMock(side_effect=answer)

    on_neon = await ConfigurationReviewer(driver, platform=NEON).review()
    unknown = await ConfigurationReviewer(driver).review()

    assert [(f.setting, f.severity) for f in on_neon] == [("fsync", Severity.LOW)]
    assert [(f.setting, f.severity) for f in unknown] == [("fsync", Severity.HIGH)]


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

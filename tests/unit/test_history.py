"""Reading the history a platform or an extension keeps.

None of the platforms is reachable from the tests, and neither extension is
installed where they run, so every reader is driven by a stubbed server that
answers with the documented columns. What is pinned: each source says how far
back it goes, says when capture is off and by which setting, never reads a
view the catalog says is missing, and survives a sibling's failure. Every
statement passes restricted mode.
"""

from datetime import UTC
from datetime import datetime
from typing import Any

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.errors import QueryError
from pg_debug.history import PRESENCE_SQL
from pg_debug.history import UNTRIED
from pg_debug.history import HistoryReader
from pg_debug.platform import Basis
from pg_debug.platform import Platform
from pg_debug.platform import PlatformDetection

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)

NOTHING: dict[str, Any] = {
    "database": "app",
    "query_store": False,
    "query_store_waits": False,
    "pg_qs_query_capture_mode": None,
    "pg_qs_retention_period_in_days": None,
    "pg_qs_interval_length_minutes": None,
    "pgms_wait_sampling_query_capture_mode": None,
    "aurora_stat_statements": None,
    "aurora_stat_plans": None,
    "aurora_compute_plan_id": None,
    "google_db_advisor": None,
    "pg_qualstats": None,
    "pg_wait_sampling": None,
    "neon": None,
    "pg_qualstats_available": False,
    "pg_wait_sampling_available": False,
    "pg_qualstats_enabled": None,
    "pg_qualstats_sample_rate": None,
    "pg_qualstats_max": None,
    "pg_wait_sampling_profile_period": None,
    "pg_wait_sampling_profile_queries": None,
    "pg_wait_sampling_sample_cpu": None,
}


class Server:
    """A driver that answers each statement by the first fragment it contains."""

    def __init__(self, presence: dict[str, Any], answers: dict[str, Any] | None = None):
        self.presence = {**NOTHING, **presence}
        self.answers = answers or {}
        self.queries: list[str] = []

    async def execute_query(self, query, params=None, force_readonly=False, *, in_context=False, cleanup=None):
        self.queries.append(query)
        if "pg_qs.query_capture_mode" in query and "aurora_compute_plan_id" in query:
            return [SqlDriver.RowResult(cells=self.presence)]
        for fragment, answer in self.answers.items():
            if fragment in query:
                if isinstance(answer, Exception):
                    raise answer
                return [SqlDriver.RowResult(cells=row) for row in answer]
        return []

    def ran(self, fragment: str) -> bool:
        return any(fragment in query for query in self.queries)


def on(platform: Platform) -> PlatformDetection:
    return PlatformDetection(platform, "a test said so", Basis.SERVER)


def source(report, name):
    return next(s for s in report.sources if s.name == name)


async def read(server: Server, platform: Platform = Platform.UNKNOWN, restricted: bool = False):
    driver: Any = SafeSqlDriver(sql_driver=server, timeout=None) if restricted else server  # type: ignore[arg-type]
    return await HistoryReader(driver, on(platform)).read(limit=5)


# -- Which sources are examined -------------------------------------------------


@pytest.mark.asyncio
async def test_an_unrecognised_server_is_asked_only_about_the_extensions():
    report = await read(Server({}))

    assert [s.name for s in report.sources] == ["pg_qualstats", "pg_wait_sampling"]
    assert report.note == UNTRIED


@pytest.mark.asyncio
async def test_a_platforms_object_in_the_catalog_is_read_even_when_the_platform_was_not_recognised():
    server = Server({"aurora_stat_statements": "pg_catalog"}, {"aurora_stat_statements(true)": [{"queryid": 1, "calls": 3}]})

    report = await read(server)

    assert source(report, "aurora_stat_statements").rows == [{"queryid": 1, "calls": 3}]


@pytest.mark.asyncio
async def test_presence_is_read_from_the_catalog_not_from_errors():
    """One statement reads where everything is; nothing is tried to see whether it fails."""
    for fragment in ("to_regclass('query_store.qs_view')", "pg_proc", "pg_extension", "pg_available_extensions", "pg_class"):
        assert fragment in PRESENCE_SQL


# -- Azure ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_azure_with_capture_off_says_which_setting_and_where_the_data_lives():
    server = Server({"pg_qs_query_capture_mode": "none", "pgms_wait_sampling_query_capture_mode": "none"})

    report = await read(server, Platform.AZURE)
    store = source(report, "azure_query_store")

    assert store.capturing is False
    assert "pg_qs.query_capture_mode = none" in store.status
    assert "azure_sys" in store.status and "app" in store.status
    assert "7 (the default) days" in store.retention
    assert not server.ran("FROM query_store.qs_view")
    assert source(report, "azure_wait_sampling").capturing is False


@pytest.mark.asyncio
async def test_azure_in_azure_sys_reads_the_windows_it_holds():
    server = Server(
        {
            "database": "azure_sys",
            "query_store": True,
            "query_store_waits": True,
            "pg_qs_query_capture_mode": "top",
            "pg_qs_retention_period_in_days": "14",
            "pg_qs_interval_length_minutes": "15",
            "pgms_wait_sampling_query_capture_mode": "all",
        },
        {
            "count(DISTINCT query_id)": [{"oldest_window": NOW, "newest_window": NOW, "queries": 4}],
            "FROM query_store.qs_view": [{"query_id": 1, "query": "SELECT 1", "calls": 10, "total_time_ms": 5.0}],
            "pgms_wait_sampling_view": [{"event_type": "Lock", "event": "tuple", "samples": 7}],
        },
    )

    report = await read(server, Platform.AZURE, restricted=True)
    store = source(report, "azure_query_store")

    assert store.capturing is True
    assert "14 days" in store.retention
    assert store.rows[0]["query_id"] == 1
    assert "holds windows from" in store.status
    assert source(report, "azure_wait_sampling").rows[0]["event"] == "tuple"


# -- Aurora --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aurora_plans_with_plan_ids_off_say_so_and_are_not_read():
    server = Server({"aurora_stat_statements": "pg_catalog", "aurora_stat_plans": "pg_catalog", "aurora_compute_plan_id": "off"})

    report = await read(server, Platform.AURORA)
    plans = source(report, "aurora_stat_plans")

    assert plans.capturing is False
    assert "aurora_compute_plan_id = off" in plans.status
    assert not server.ran("aurora_stat_plans(true)")


@pytest.mark.asyncio
async def test_aurora_reads_both_functions_in_restricted_mode_and_states_the_statements_window():
    server = Server(
        {"aurora_stat_statements": "pg_catalog", "aurora_stat_plans": "pg_catalog", "aurora_compute_plan_id": "on"},
        {
            "FROM pg_extension e": [{"schema": "public", "version": "1.10"}],
            "pg_stat_statements_info": [{"stats_reset": NOW, "dealloc": 0, "now": NOW}],
            "aurora_stat_statements(true)": [{"queryid": 1, "storage_blks_read": 5, "orcache_blks_hit": 2}],
            "aurora_stat_plans(true)": [{"queryid": 1, "planid": 9, "plan_type": "estimate"}],
        },
    )

    report = await read(server, Platform.AURORA, restricted=True)

    assert source(report, "aurora_stat_statements").rows[0]["orcache_blks_hit"] == 2
    assert "since 2026-10-14" in source(report, "aurora_stat_statements").retention
    assert source(report, "aurora_stat_plans").rows[0]["planid"] == 9


@pytest.mark.asyncio
async def test_aurora_without_the_functions_names_the_versions_that_have_them():
    report = await read(Server({}), Platform.AURORA)

    assert "14.9" in source(report, "aurora_stat_statements").status
    assert "14.10" in source(report, "aurora_stat_plans").status


# -- Cloud SQL and AlloyDB -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_index_advisors_recommendations_are_read_and_attributed_to_the_platform():
    server = Server(
        {"google_db_advisor": "public"},
        {"google_db_advisor_recommended_indexes": [{"index": "CREATE INDEX ON t(a)", "estimated_storage_size_in_mb": 3}]},
    )

    report = await read(server, Platform.ALLOYDB, restricted=True)
    advisor = source(report, "google_db_advisor")

    assert advisor.rows[0]["index"] == "CREATE INDEX ON t(a)"
    assert "hypothetical-index" in advisor.status
    assert "alloydb" in advisor.doc
    assert "no interval" in advisor.retention


@pytest.mark.asyncio
async def test_cloud_sql_without_the_advisor_says_how_to_enable_it():
    report = await read(Server({}), Platform.CLOUD_SQL)

    assert "Enterprise Plus" in source(report, "google_db_advisor").status


# -- Neon ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_neon_without_the_extension_does_not_create_it():
    server = Server({})

    report = await read(server, Platform.NEON)

    assert "CREATE EXTENSION neon" in source(report, "neon_file_cache").status
    assert not server.ran("CREATE EXTENSION")


@pytest.mark.asyncio
async def test_neon_sets_the_file_cache_beside_the_buffer_ratio_that_misreads_it():
    server = Server(
        {"neon": "public"},
        {
            "neon_stat_file_cache": [{"file_cache_hits": 990, "file_cache_misses": 10, "file_cache_hit_ratio": 99.0}],
            "FROM pg_stat_database": [{"blks_hit": 1000, "blks_read": 1000}],
        },
    )

    report = await read(server, Platform.NEON, restricted=True)
    cache = source(report, "neon_file_cache")

    assert cache.capturing is True
    assert "990 of 1,000" in cache.status
    assert "50.0%" in cache.status, "the buffer ratio, which counts the file cache's hits as reads"


@pytest.mark.asyncio
async def test_a_neon_compute_with_the_file_cache_off_says_so():
    server = Server({"neon": "public"}, {"neon_stat_file_cache": [{"file_cache_hits": 0, "file_cache_misses": 0}]})

    report = await read(server, Platform.NEON)

    assert source(report, "neon_file_cache").capturing is False
    assert "18 CU" in source(report, "neon_file_cache").status


# -- The extensions ------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_extension_the_server_has_but_this_database_lacks_says_how_to_install_it():
    report = await read(Server({"pg_qualstats_available": True}))

    assert "CREATE EXTENSION pg_qualstats" in source(report, "pg_qualstats").status
    assert "does not have it" in source(report, "pg_wait_sampling").status


@pytest.mark.asyncio
async def test_an_installed_extension_whose_library_is_not_loaded_is_not_read():
    server = Server({"pg_qualstats": "public", "pg_wait_sampling": "public"})

    report = await read(server)

    assert "shared_preload_libraries" in source(report, "pg_qualstats").status
    assert "shared_preload_libraries" in source(report, "pg_wait_sampling").status
    assert not server.ran("FROM public.pg_qualstats_pretty") and not server.ran("FROM public.pg_wait_sampling_profile")


@pytest.mark.asyncio
async def test_both_extensions_are_read_in_restricted_mode_with_their_windows():
    server = Server(
        {
            "pg_qualstats": "public",
            "pg_qualstats_enabled": "on",
            "pg_qualstats_sample_rate": "0.1",
            "pg_wait_sampling": "public",
            "pg_wait_sampling_profile_period": "10",
        },
        {
            "pg_qualstats_pretty": [{"left_table": "orders", "left_column": "tenant_id", "execution_count": 40}],
            "pg_wait_sampling_profile": [{"event_type": "CPU", "event": "running", "samples": 120}],
        },
    )

    report = await read(server, restricted=True)

    qualstats, waits = source(report, "pg_qualstats"), source(report, "pg_wait_sampling")
    assert qualstats.capturing is True and qualstats.rows[0]["left_column"] == "tenant_id"
    assert "sample_rate = 0.1" in qualstats.retention
    assert waits.rows[0]["samples"] == 120 and "120 samples" in waits.status


@pytest.mark.asyncio
async def test_pg_qualstats_switched_off_says_so():
    server = Server({"pg_qualstats": "public", "pg_qualstats_enabled": "off"}, {"pg_qualstats_pretty": []})

    report = await read(server)

    assert source(report, "pg_qualstats").capturing is False
    assert "pg_qualstats.enabled = off" in source(report, "pg_qualstats").status


# -- Failure -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_refused_read_is_reported_on_its_source_and_the_others_still_run():
    server = Server(
        {"pg_qualstats": "public", "pg_qualstats_enabled": "on", "pg_wait_sampling": "public", "pg_wait_sampling_profile_period": "10"},
        {
            "pg_qualstats_pretty": QueryError("permission denied for view pg_qualstats_pretty"),
            "pg_wait_sampling_profile": [{"event_type": "IO", "event": "DataFileRead", "samples": 3}],
        },
    )

    report = await read(server)

    assert "permission denied" in (source(report, "pg_qualstats").error or "")
    assert source(report, "pg_wait_sampling").rows


@pytest.mark.asyncio
async def test_a_limit_below_one_is_refused():
    from pg_debug.errors import InvalidArgumentError

    with pytest.raises(InvalidArgumentError):
        await HistoryReader(Server({}), on(Platform.UNKNOWN)).read(limit=0)  # type: ignore[arg-type]


def test_the_report_is_plain_data():
    from pg_debug.history import HistoryReport
    from pg_debug.history import HistorySource

    report = HistoryReport(platform={"platform": "neon"}, sources=[HistorySource("x", "X", True, None, "forever", "ok")])

    assert report.to_dict()["sources"][0]["retention"] == "forever"

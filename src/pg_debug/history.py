"""The history a platform or an extension keeps, read where it is kept.

``pg_stat_statements`` counts from its last reset, and on a managed platform
that can be minutes ago. Some platforms keep more: Azure's Query Store keeps
days of 15-minute windows, Aurora keeps a plan per statement, Cloud SQL and
AlloyDB keep the index advisor's recommendations, Neon counts its local file
cache. Two extensions anyone can install keep evidence pg_stat_statements has
not: ``pg_qualstats`` the predicates queries ran with, ``pg_wait_sampling``
what backends waited on.

Each reader says how far back what it read goes, and says so when capture is
off rather than returning nothing. Whether an object is there is read from the
catalog in one statement before anything else runs, so a reader never relies
on an error to learn that its view is missing.

None of the platform readers has been run on its platform: each follows the
platform's documentation, cited beside it, and the column names it reads are
the documented ones. :data:`UNTRIED` says so in every report.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import LiteralString
from typing import cast

from .db import SafeSqlDriver
from .db import SqlDriver
from .db import workload_only
from .db.statements import THIS_DATABASE
from .db.statements import locate_pg_stat_statements
from .db.window import statement_window
from .errors import InvalidArgumentError
from .errors import PgDebugError
from .platform import Platform
from .platform import PlatformDetection

logger = logging.getLogger(__name__)

#: A source as examined so far, and the read that completes it.
Reading = tuple["HistorySource", Callable[["HistorySource"], Awaitable[None]]]

#: Said in every report, because it is true of every platform reader.
UNTRIED = (
    "The Azure, Aurora, Cloud SQL/AlloyDB and Neon readers follow each platform's documentation and have not been "
    "run on the platforms; pg_qualstats and pg_wait_sampling follow the extensions' documentation."
)

AZURE_QUERY_STORE_DOC = "https://learn.microsoft.com/en-us/azure/postgresql/monitor/concepts-query-store"
AURORA_STAT_STATEMENTS_DOC = "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora_stat_statements.html"
AURORA_STAT_PLANS_DOC = "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora_stat_plans.html"
CLOUD_SQL_ADVISOR_DOC = "https://docs.cloud.google.com/sql/docs/postgres/use-index-advisor"
ALLOYDB_ADVISOR_DOC = "https://docs.cloud.google.com/alloydb/docs/use-index-advisor"
NEON_FILE_CACHE_DOC = "https://neon.com/docs/extensions/neon"
PG_QUALSTATS_DOC = "https://github.com/powa-team/pg_qualstats"
PG_WAIT_SAMPLING_DOC = "https://github.com/postgrespro/pg_wait_sampling"

#: Where everything is, in one read of the catalog and the settings. A
#: function is looked up in pg_proc rather than with to_regproc, which returns
#: NULL for a name with more than one signature as well as for a missing one.
#: A view is looked up by name in pg_class, in whichever schema it was put.
#: current_setting with its second argument reads NULL for a setting that does
#: not exist, which is how a library that is not loaded shows.
PRESENCE_SQL = """
SELECT
    current_database() AS database,
    to_regclass('query_store.qs_view') IS NOT NULL AS query_store,
    to_regclass('query_store.pgms_wait_sampling_view') IS NOT NULL AS query_store_waits,
    current_setting('pg_qs.query_capture_mode', true) AS pg_qs_query_capture_mode,
    current_setting('pg_qs.retention_period_in_days', true) AS pg_qs_retention_period_in_days,
    current_setting('pg_qs.interval_length_minutes', true) AS pg_qs_interval_length_minutes,
    current_setting('pgms_wait_sampling.query_capture_mode', true) AS pgms_wait_sampling_query_capture_mode,
    (SELECT quote_ident(n.nspname) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
      WHERE p.proname = 'aurora_stat_statements' ORDER BY n.nspname <> 'pg_catalog' LIMIT 1) AS aurora_stat_statements,
    (SELECT quote_ident(n.nspname) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
      WHERE p.proname = 'aurora_stat_plans' ORDER BY n.nspname <> 'pg_catalog' LIMIT 1) AS aurora_stat_plans,
    current_setting('aurora_compute_plan_id', true) AS aurora_compute_plan_id,
    (SELECT quote_ident(n.nspname) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE c.relname = 'google_db_advisor_recommended_indexes' LIMIT 1) AS google_db_advisor,
    (SELECT quote_ident(n.nspname) FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
      WHERE e.extname = 'pg_qualstats') AS pg_qualstats,
    (SELECT quote_ident(n.nspname) FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
      WHERE e.extname = 'pg_wait_sampling') AS pg_wait_sampling,
    (SELECT quote_ident(n.nspname) FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
      WHERE e.extname = 'neon') AS neon,
    EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_qualstats') AS pg_qualstats_available,
    EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_wait_sampling') AS pg_wait_sampling_available,
    current_setting('pg_qualstats.enabled', true) AS pg_qualstats_enabled,
    current_setting('pg_qualstats.sample_rate', true) AS pg_qualstats_sample_rate,
    current_setting('pg_qualstats.max', true) AS pg_qualstats_max,
    current_setting('pg_wait_sampling.profile_period', true) AS pg_wait_sampling_profile_period,
    current_setting('pg_wait_sampling.profile_queries', true) AS pg_wait_sampling_profile_queries,
    current_setting('pg_wait_sampling.sample_cpu', true) AS pg_wait_sampling_sample_cpu
"""


@dataclass
class HistorySource:
    """One place history is kept, and what was read from it.

    Attributes:
        name: A stable identifier: ``azure_query_store``,
            ``azure_wait_sampling``, ``aurora_stat_statements``,
            ``aurora_stat_plans``, ``google_db_advisor``, ``pg_qualstats``,
            ``pg_wait_sampling`` or ``neon_file_cache``.
        label: What a sentence calls it.
        present: Whether its view or function is in this database.
        capturing: Whether the platform or extension is recording: False
            when a setting says capture is off, None when nothing says.
        retention: How far back what it keeps goes, and what clears it.
        status: What was read, or why nothing was, in a sentence.
        evidence: The settings and catalog reads the status rests on.
        rows: What was read, at most ``limit`` rows.
        doc: The documentation the reader follows.
        error: The server's refusal, when the read failed.
    """

    name: str
    label: str
    present: bool
    capturing: bool | None
    retention: str
    status: str
    evidence: dict[str, Any] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    doc: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "present": self.present,
            "capturing": self.capturing,
            "retention": self.retention,
            "status": self.status,
            "evidence": self.evidence,
            "rows": self.rows,
            "doc": self.doc,
            "error": self.error,
        }


@dataclass
class HistoryReport:
    """Every history source examined on this server.

    Attributes:
        platform: The platform detection the readers were chosen by:
            ``platform``, ``signal`` and ``basis``.
        sources: One per source examined, including the ones that are not
            there or not capturing.
        note: :data:`UNTRIED`.
    """

    platform: dict[str, Any]
    sources: list[HistorySource]
    note: str = UNTRIED

    def to_dict(self) -> dict[str, Any]:
        return {"platform": self.platform, "sources": [source.to_dict() for source in self.sources], "note": self.note}


class HistoryReader:
    """Reads the history the platform and the installed extensions keep."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver, platform: PlatformDetection):
        self.sql_driver = sql_driver
        self.platform = platform

    async def read(self, limit: int = 10) -> HistoryReport:
        """Examine every source this server could have, and read the ones it has.

        A platform's sources are examined when the platform was detected or
        its object is in this database; the two extensions' always are, since
        any server may have them.

        Args:
            limit: How many rows to read from each source.
        """
        if limit < 1:
            raise InvalidArgumentError("The limit must be at least 1.")
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, PRESENCE_SQL)
        found = dict(rows[0].cells) if rows else {}
        platform = self.platform.platform

        sources: list[HistorySource] = []
        if platform is Platform.AZURE or found.get("query_store") or found.get("pg_qs_query_capture_mode") is not None:
            sources.append(await self._guarded(self._azure_query_store(found, limit)))
            sources.append(await self._guarded(self._azure_wait_sampling(found, limit)))
        if platform is Platform.AURORA or found.get("aurora_stat_statements") or found.get("aurora_stat_plans"):
            sources.append(await self._guarded(self._aurora_statements(found, limit)))
            sources.append(await self._guarded(self._aurora_plans(found, limit)))
        if platform in (Platform.CLOUD_SQL, Platform.ALLOYDB) or found.get("google_db_advisor"):
            sources.append(await self._guarded(self._google_db_advisor(found, limit)))
        if platform in (Platform.NEON, Platform.LAKEBASE) or found.get("neon"):
            sources.append(await self._guarded(self._neon_file_cache(found)))
        sources.append(await self._guarded(self._pg_qualstats(found, limit)))
        sources.append(await self._guarded(self._pg_wait_sampling(found, limit)))

        return HistoryReport(
            platform={"platform": platform.value, "signal": self.platform.signal, "basis": self.platform.basis.value},
            sources=sources,
        )

    async def _guarded(self, reading: Reading) -> HistorySource:
        """Run one reader; a refusal is reported on its source rather than ending the report."""
        source, read = reading
        try:
            await read(source)
        except PgDebugError as e:
            logger.info("Could not read %s: %s", source.label, e)
            source.error = str(e)
            source.status = f"{source.status} Reading it failed: {e}"
        return source

    async def _query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, cast(LiteralString, sql), params)
        return [dict(row.cells) for row in rows or []]

    # -- Azure Query Store ----------------------------------------------------

    def _azure_query_store(self, found: dict[str, Any], limit: int) -> Reading:
        # Query Store: data in database azure_sys, schema query_store, 7 days
        # by default, opt-in through pg_qs.query_capture_mode (default none).
        # Column names as documented at AZURE_QUERY_STORE_DOC.
        mode = found.get("pg_qs_query_capture_mode")
        days = found.get("pg_qs_retention_period_in_days") or "7 (the default)"
        minutes = found.get("pg_qs_interval_length_minutes") or "15 (the default)"
        capturing = None if mode is None else mode.lower() in ("top", "all")
        source = HistorySource(
            name="azure_query_store",
            label="Azure Query Store",
            present=bool(found.get("query_store")),
            capturing=capturing,
            retention=(
                f"{days} days (pg_qs.retention_period_in_days), in windows of {minutes} minutes "
                "(pg_qs.interval_length_minutes); the window being recorded shows only once it closes"
            ),
            status="",
            evidence={
                "database": found.get("database"),
                "pg_qs.query_capture_mode": mode,
                "pg_qs.retention_period_in_days": found.get("pg_qs_retention_period_in_days"),
                "pg_qs.interval_length_minutes": found.get("pg_qs_interval_length_minutes"),
            },
            doc=AZURE_QUERY_STORE_DOC,
        )

        async def read(source: HistorySource) -> None:
            off = (
                f"Capture is off: pg_qs.query_capture_mode = {mode}. Set it to top or all in the server parameters; "
                "the first data takes up to 20 minutes to appear."
                if capturing is False
                else ""
            )
            if not source.present:
                source.status = " ".join(
                    part
                    for part in (
                        off,
                        f"Query Store keeps its data in the database azure_sys, and this connection is to "
                        f"{found.get('database')}: point the URL at azure_sys to read it.",
                    )
                    if part
                )
                return
            span = await self._query(
                "SELECT min(start_time) AS oldest_window, max(end_time) AS newest_window, "
                "count(DISTINCT query_id) AS queries FROM query_store.qs_view"
            )
            source.evidence.update(span[0] if span else {})
            source.rows = await self._query(
                """
SELECT query_id,
       db_id,
       max(query_sql_text) AS query,
       sum(calls) AS calls,
       sum(total_time) AS total_time_ms,
       sum(total_time) / nullif(sum(calls), 0) AS mean_time_ms,
       sum(shared_blks_read) AS shared_blks_read,
       sum(temp_blks_written) AS temp_blks_written,
       min(start_time) AS first_window,
       max(end_time) AS last_window
FROM query_store.qs_view
WHERE NOT is_system_query
GROUP BY query_id, db_id
ORDER BY sum(total_time) DESC
LIMIT {}
""",
                [limit],
            )
            oldest, newest = source.evidence.get("oldest_window"), source.evidence.get("newest_window")
            held = f"Query Store holds windows from {oldest} to {newest}." if oldest else "Query Store holds no windows yet."
            source.status = " ".join(part for part in (off, held) if part)

        return source, read

    def _azure_wait_sampling(self, found: dict[str, Any], limit: int) -> Reading:
        # pgms_wait_sampling.query_capture_mode defaults to none, and has no
        # effect while pg_qs.query_capture_mode is none (AZURE_QUERY_STORE_DOC).
        mode = found.get("pgms_wait_sampling_query_capture_mode")
        qs_mode = found.get("pg_qs_query_capture_mode")
        if mode is None:
            capturing = None
        else:
            capturing = mode.lower() == "all" and (qs_mode or "").lower() in ("top", "all")
        source = HistorySource(
            name="azure_wait_sampling",
            label="Azure Query Store wait sampling",
            present=bool(found.get("query_store_waits")),
            capturing=capturing,
            retention="the same as Query Store's: pg_qs.retention_period_in_days, in pg_qs.interval_length_minutes windows",
            status="",
            evidence={"pgms_wait_sampling.query_capture_mode": mode, "pg_qs.query_capture_mode": qs_mode},
            doc=AZURE_QUERY_STORE_DOC,
        )

        async def read(source: HistorySource) -> None:
            off = ""
            if capturing is False:
                off = (
                    f"Capture is off: pgms_wait_sampling.query_capture_mode = {mode} and pg_qs.query_capture_mode = {qs_mode}; "
                    "both have to be on for waits to be recorded."
                )
            if not source.present:
                source.status = " ".join(part for part in (off, "Its view is in the database azure_sys; point the URL at it.") if part)
                return
            source.rows = await self._query(
                """
SELECT event_type, event, sum(calls) AS samples, min(start_time) AS first_window, max(end_time) AS last_window
FROM query_store.pgms_wait_sampling_view
GROUP BY event_type, event
ORDER BY sum(calls) DESC
LIMIT {}
""",
                [limit],
            )
            source.status = " ".join(part for part in (off, f"{len(source.rows)} kinds of wait read.") if part)

        return source, read

    # -- Aurora ---------------------------------------------------------------

    def _aurora_statements(self, found: dict[str, Any], limit: int) -> Reading:
        # aurora_stat_statements(showtext): pg_stat_statements' columns plus
        # storage_blks_read, orcache_blks_hit, storage_blk_read_time and
        # orcache_blk_read_time; Aurora 14.9 and 15.4 on
        # (AURORA_STAT_STATEMENTS_DOC). Reset by pg_stat_statements_reset().
        schema = found.get("aurora_stat_statements")
        source = HistorySource(
            name="aurora_stat_statements",
            label="Aurora aurora_stat_statements()",
            present=bool(schema),
            capturing=None,
            retention="the same window as pg_stat_statements, which pg_stat_statements_reset() also clears",
            status="",
            doc=AURORA_STAT_STATEMENTS_DOC,
        )

        async def read(source: HistorySource) -> None:
            if not schema:
                source.status = "The function is not in this database: it comes with Aurora PostgreSQL 14.9, 15.4 and later."
                return
            await self._statement_window_into(source)
            source.rows = await self._query(
                f"""
SELECT queryid, left(query, 500) AS query, calls, total_exec_time, shared_blks_hit, shared_blks_read,
       storage_blks_read, orcache_blks_hit, storage_blk_read_time, orcache_blk_read_time
FROM {_function(schema, "aurora_stat_statements")}(true)
WHERE {THIS_DATABASE} AND {workload_only()}
ORDER BY total_exec_time DESC
LIMIT {{}}
""",
                [limit],
            )
            source.capturing = True
            source.status = (
                f"{len(source.rows)} statements read. storage_blks_read counts the blocks read from Aurora storage and "
                "orcache_blks_hit those served by the Optimized Reads cache; shared_blks_read counts both as misses."
            )

        return source, read

    def _aurora_plans(self, found: dict[str, Any], limit: int) -> Reading:
        # aurora_stat_plans(showtext): one row per tracked plan, with planid,
        # explain_plan, plan_type and plan_captured_time; plans are tracked
        # only with aurora_compute_plan_id on (AURORA_STAT_PLANS_DOC).
        schema = found.get("aurora_stat_plans")
        setting = found.get("aurora_compute_plan_id")
        capturing = None if setting is None else setting.lower() in ("on", "true", "1")
        source = HistorySource(
            name="aurora_stat_plans",
            label="Aurora aurora_stat_plans()",
            present=bool(schema),
            capturing=capturing,
            retention="the same window as pg_stat_statements; as many plans as pg_stat_statements.max allows",
            status="",
            evidence={"aurora_compute_plan_id": setting},
            doc=AURORA_STAT_PLANS_DOC,
        )

        async def read(source: HistorySource) -> None:
            if not schema:
                source.status = "The function is not in this database: it comes with Aurora PostgreSQL 14.10, 15.5 and later."
                return
            if capturing is False:
                source.status = f"Capture is off: aurora_compute_plan_id = {setting}. Turn it on in the parameter group to track plans."
                return
            await self._statement_window_into(source)
            source.rows = await self._query(
                f"""
SELECT queryid, planid, plan_type, plan_captured_time, calls, total_exec_time, left(explain_plan, 2000) AS explain_plan
FROM {_function(schema, "aurora_stat_plans")}(true)
WHERE {THIS_DATABASE} AND {workload_only()}
ORDER BY total_exec_time DESC
LIMIT {{}}
""",
                [limit],
            )
            source.status = f"{len(source.rows)} plans read."

        return source, read

    async def _statement_window_into(self, source: HistorySource) -> None:
        stats = await locate_pg_stat_statements(self.sql_driver)
        if stats is None:
            return
        window = await statement_window(self.sql_driver, stats)
        source.retention = f"{source.retention}: {window.describe()}"
        source.evidence.update(window.to_dict())

    # -- Cloud SQL and AlloyDB ------------------------------------------------

    def _google_db_advisor(self, found: dict[str, Any], limit: int) -> Reading:
        # google_db_advisor_recommended_indexes: index (a CREATE INDEX
        # statement) and estimated_storage_size_in_mb are the documented
        # columns; the rest are read as they are (CLOUD_SQL_ADVISOR_DOC,
        # ALLOYDB_ADVISOR_DOC). Cloud SQL needs Enterprise Plus.
        schema = found.get("google_db_advisor")
        source = HistorySource(
            name="google_db_advisor",
            label="Cloud SQL / AlloyDB index advisor",
            present=bool(schema),
            capturing=True if schema else None,
            retention=(
                "the platform runs the analysis periodically and its documentation gives no interval or retention; "
                "a role other than the platform's superuser sees only recommendations from its own queries"
            ),
            status="",
            doc=ALLOYDB_ADVISOR_DOC if self.platform.platform is Platform.ALLOYDB else CLOUD_SQL_ADVISOR_DOC,
        )

        async def read(source: HistorySource) -> None:
            if not schema:
                source.status = (
                    "The advisor's views are not in this database. Enable the index advisor under Query Insights "
                    "(on Cloud SQL, Enterprise Plus only)."
                )
                return
            source.rows = await self._query(f"SELECT * FROM {schema}.google_db_advisor_recommended_indexes LIMIT {{}}", [limit])
            source.status = (
                f"{len(source.rows)} recommendations read. They are the platform's, not pg-debug's: simulate one with "
                "pg-debug explain --hypothetical-index before creating it."
            )

        return source, read

    # -- Neon -----------------------------------------------------------------

    def _neon_file_cache(self, found: dict[str, Any]) -> Reading:
        # neon_stat_file_cache: file_cache_misses, file_cache_hits,
        # file_cache_used, file_cache_writes, file_cache_hit_ratio; needs
        # CREATE EXTENSION neon; covers the whole compute and resets when it
        # restarts; off on computes of 18 CU and above (NEON_FILE_CACHE_DOC).
        schema = found.get("neon")
        source = HistorySource(
            name="neon_file_cache",
            label="Neon local file cache",
            present=bool(schema),
            capturing=None,
            retention="since the compute last started; a suspend or restart resets it",
            status="",
            doc=NEON_FILE_CACHE_DOC,
        )

        async def read(source: HistorySource) -> None:
            if not schema:
                source.status = (
                    "The neon extension is not installed in this database. CREATE EXTENSION neon installs it; that is a "
                    "write, so pg-debug does not run it."
                )
                return
            cache = await self._query(
                f"SELECT file_cache_misses, file_cache_hits, file_cache_used, file_cache_writes, file_cache_hit_ratio "
                f"FROM {schema}.neon_stat_file_cache"
            )
            buffers = await self._query("SELECT sum(blks_hit) AS blks_hit, sum(blks_read) AS blks_read FROM pg_stat_database")
            cells = cache[0] if cache else {}
            source.rows = [cells] if cells else []
            source.evidence.update(buffers[0] if buffers else {})
            source.status = _neon_status(cells, buffers[0] if buffers else {})
            hits, misses = cells.get("file_cache_hits"), cells.get("file_cache_misses")
            source.capturing = bool((hits or 0) + (misses or 0)) if cells else None

        return source, read

    # -- Extensions -----------------------------------------------------------

    def _pg_qualstats(self, found: dict[str, Any], limit: int) -> Reading:
        # The pg_qualstats_pretty view: left_schema, left_table, left_column,
        # operator, right_*, occurences (sic), execution_count, nbfiltered.
        # Kept in memory, not across restarts; samples 1/max_connections of
        # queries by default (PG_QUALSTATS_DOC).
        schema = found.get("pg_qualstats")
        enabled = found.get("pg_qualstats_enabled")
        capturing = None if not schema else enabled is not None and enabled.lower() in ("on", "true", "1")
        source = HistorySource(
            name="pg_qualstats",
            label="pg_qualstats",
            present=bool(schema),
            capturing=capturing,
            retention=(
                "in memory since the last pg_qualstats_reset() or server start, not kept across a restart; samples "
                + (
                    f"a share of queries (pg_qualstats.sample_rate = {found.get('pg_qualstats_sample_rate')}, where -1 means 1/max_connections)"
                    if found.get("pg_qualstats_sample_rate") is not None
                    else "1/max_connections of queries by default (pg_qualstats.sample_rate)"
                )
            ),
            status="",
            evidence={
                "pg_qualstats.enabled": enabled,
                "pg_qualstats.sample_rate": found.get("pg_qualstats_sample_rate"),
                "pg_qualstats.max": found.get("pg_qualstats_max"),
                "available_on_server": found.get("pg_qualstats_available"),
            },
            doc=PG_QUALSTATS_DOC,
        )

        async def read(source: HistorySource) -> None:
            if not schema:
                source.status = _not_installed("pg_qualstats", found.get("pg_qualstats_available"))
                return
            if enabled is None:
                source.status = "Installed, but its library is not loaded: add pg_qualstats to shared_preload_libraries and restart."
                return
            if capturing is False:
                source.status = f"Capture is off: pg_qualstats.enabled = {enabled}."
            source.rows = await self._query(
                f"""
SELECT left_schema, left_table, left_column, operator, right_schema, right_table, right_column,
       occurences, execution_count, nbfiltered
FROM {schema}.pg_qualstats_pretty
ORDER BY execution_count DESC
LIMIT {{}}
""",
                [limit],
            )
            source.status = " ".join(part for part in (source.status, f"{len(source.rows)} predicates read.") if part)

        return source, read

    def _pg_wait_sampling(self, found: dict[str, Any], limit: int) -> Reading:
        # pg_wait_sampling_profile: pid, event_type, event, queryid, count;
        # sampled every pg_wait_sampling.profile_period ms (10 by default),
        # cumulative until pg_wait_sampling_reset_profile(); with sample_cpu a
        # backend not waiting is sampled with NULL event (PG_WAIT_SAMPLING_DOC).
        schema = found.get("pg_wait_sampling")
        period = found.get("pg_wait_sampling_profile_period")
        source = HistorySource(
            name="pg_wait_sampling",
            label="pg_wait_sampling",
            present=bool(schema),
            capturing=None if not schema else period is not None,
            retention=(
                f"in memory since pg_wait_sampling_reset_profile() or the server started, sampled every {period or 10} ms "
                "(pg_wait_sampling.profile_period)"
            ),
            status="",
            evidence={
                "pg_wait_sampling.profile_period": period,
                "pg_wait_sampling.profile_queries": found.get("pg_wait_sampling_profile_queries"),
                "pg_wait_sampling.sample_cpu": found.get("pg_wait_sampling_sample_cpu"),
                "available_on_server": found.get("pg_wait_sampling_available"),
            },
            doc=PG_WAIT_SAMPLING_DOC,
        )

        async def read(source: HistorySource) -> None:
            if not schema:
                source.status = _not_installed("pg_wait_sampling", found.get("pg_wait_sampling_available"))
                return
            if period is None:
                source.status = "Installed, but its library is not loaded: add pg_wait_sampling to shared_preload_libraries and restart."
                return
            source.rows = await self._query(
                f"""
SELECT coalesce(event_type, 'CPU') AS event_type, coalesce(event, 'running') AS event, sum(count) AS samples
FROM {schema}.pg_wait_sampling_profile
GROUP BY 1, 2
ORDER BY sum(count) DESC
LIMIT {{}}
""",
                [limit],
            )
            total = sum(int(row.get("samples") or 0) for row in source.rows)
            source.status = f"{len(source.rows)} kinds of wait read, {total:,} samples among them."

        return source, read


def _function(schema: str, name: str) -> str:
    """A function call's name: bare in pg_catalog, which restricted mode reads as the same function, qualified elsewhere."""
    return name if schema == "pg_catalog" else f"{schema}.{name}"


def _not_installed(extension: str, available: Any) -> str:
    if available:
        return f"Not installed in this database, though the server has it: CREATE EXTENSION {extension}, after adding it to shared_preload_libraries."
    return "Not installed, and the server does not have it available (pg_available_extensions)."


def _neon_status(cache: dict[str, Any], buffers: dict[str, Any]) -> str:
    """What the file cache did, beside the buffer cache hit ratio that counts its hits as misses."""
    hits, misses = cache.get("file_cache_hits"), cache.get("file_cache_misses")
    if not cache or (hits or 0) + (misses or 0) == 0:
        return (
            "The file cache recorded no requests: on computes of 18 CU and above Neon turns it off and sizes "
            "shared_buffers instead, and the buffer cache hit ratio means what it does anywhere."
        )
    ratio = float(cache.get("file_cache_hit_ratio") or 0)
    requests = int(hits or 0) + int(misses or 0)
    status = f"The local file cache served {int(hits or 0):,} of {requests:,} requests ({ratio:.1f}%); Neon's target for OLTP is 99%."
    blks_hit, blks_read = int(buffers.get("blks_hit") or 0), int(buffers.get("blks_read") or 0)
    if blks_hit + blks_read:
        buffer_ratio = blks_hit / (blks_hit + blks_read)
        status += (
            f" The buffer cache hit ratio is {buffer_ratio:.1%}, and it counts a block the file cache served as a read: "
            "only a file cache miss went to storage, so judge the cache by the file cache's ratio."
        )
    return status


__all__ = ["UNTRIED", "HistoryReader", "HistoryReport", "HistorySource"]

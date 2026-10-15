"""Partition health: each finding from the catalog rows that argue it, and silence where they do not."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.health import HealthType
from pg_debug.health.checks.partition import BOUNDS_SQL
from pg_debug.health.checks.partition import CONTEXT_PRUNING_SQL
from pg_debug.health.checks.partition import DEFAULTS_SQL
from pg_debug.health.checks.partition import LOCKS_SQL
from pg_debug.health.checks.partition import PRUNING_OVERRIDES_SQL
from pg_debug.health.checks.partition import ROOTS_SQL
from pg_debug.health.checks.partition import SETTINGS_SQL
from pg_debug.health.checks.partition import UNIQUE_PER_PARTITION_SQL
from pg_debug.health.checks.partition import PartitionHealthCalc
from pg_debug.health.checks.partition import _advance
from pg_debug.health.checks.partition import _next_name
from pg_debug.health.checks.partition import _point
from pg_debug.health.checks.partition import _range_bound
from pg_debug.health.checks.partition import fast_path_slots
from pg_debug.plans.diagnoser import PARTITIONS_SQL

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def root(**overrides: Any) -> dict[str, Any]:
    row = {
        "schema": "public",
        "table": "events",
        "table_ident": "public.events",
        "strategy": "r",
        "partition_key": "RANGE (created_at)",
        "key_columns": ["created_at"],
        "key_idents": ["created_at"],
        "key_types": ["timestamptz"],
        "key_count": 1,
        "leaves": 3,
        "depth": 1,
        "relations": 4,
        "indexes": 4,
        "one_partition_locks": 3,
        "default_partition": None,
    }
    row.update(overrides)
    return row


def bound(partition: str, lower: str, upper: str, table: str = "public.events") -> dict[str, Any]:
    return {
        "root": table,
        "partition": partition,
        "bound": f"FOR VALUES FROM ('{lower}') TO ('{upper}')",
        "now": NOW,
        "local_now": NOW.replace(tzinfo=None),
    }


class FakeDriver:
    """Answers each of the check's queries from the catalog it describes."""

    def __init__(
        self,
        roots: list[dict[str, Any]] | None = None,
        defaults: list[dict[str, Any]] | None = None,
        bounds: list[dict[str, Any]] | None = None,
        unique: list[dict[str, Any]] | None = None,
        overrides: list[dict[str, Any]] | None = None,
        pruning: str = "on",
        pruning_source: str = "default",
        version: int = 160004,
        max_locks: int = 64,
        statements: list[dict[str, Any]] | None = None,
    ):
        self.answers = {
            "pg_get_partkeydef": roots or [],
            "default_is_partitioned": defaults or [],
            "pg_get_expr(c.relpartbound": bounds or [],
            "i.indisunique": unique or [],
            "pg_db_role_setting": overrides or [],
            "AS max_locks_per_transaction": [
                {"enable_partition_pruning": pruning, "pruning_source": pruning_source, "max_locks_per_transaction": max_locks}
            ],
            "FROM pg_locks": [{"fast_path_locks": 5, "shared_table_locks": 0, "backends_past_fast_path": 0}],
            "server_version_num": [{"server_version_num": str(version)}],
        }
        self.statements = statements
        self.context = None
        self.queries: list[str] = []

    async def execute_query(self, query: str, *args: Any, **kwargs: Any) -> list[SqlDriver.RowResult]:
        self.queries.append(query)
        if "pg_extension" in query:
            # Where pg_stat_statements lives, if it is installed.
            if self.statements is None:
                return []
            return [SqlDriver.RowResult(cells={"schema": "public", "version": "1.10"})]
        if "total_exec_time" in query:
            return [SqlDriver.RowResult(cells=row) for row in self.statements or []]
        for marker, rows in self.answers.items():
            if marker in query:
                return [SqlDriver.RowResult(cells=dict(row)) for row in rows]
        raise AssertionError(f"unexpected query: {query}")


async def run(driver: FakeDriver, **kwargs: Any):
    return await PartitionHealthCalc(driver, **kwargs).partition_health_check()  # type: ignore[arg-type]


def kinds(result) -> list[str]:
    return [finding["kind"] for finding in result.findings]


# -- Restricted mode ---------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        ROOTS_SQL,
        DEFAULTS_SQL,
        SafeSqlDriver.param_sql_to_query(BOUNDS_SQL, [["public.events"]]),
        UNIQUE_PER_PARTITION_SQL,
        PRUNING_OVERRIDES_SQL,
        SETTINGS_SQL,
        LOCKS_SQL,
        CONTEXT_PRUNING_SQL,
        SafeSqlDriver.param_sql_to_query(PARTITIONS_SQL, [["public.events_2026_10"]]),
    ],
)
def test_every_statement_is_allowed_in_restricted_mode(sql):
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(sql)


def test_partition_is_a_health_type_and_part_of_all():
    assert HealthType.parse("partition") == {HealthType.PARTITION}
    assert HealthType.PARTITION in HealthType.parse("all")


# -- No partitioned tables ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_database_with_no_partitioned_tables_is_healthy_and_says_why():
    result = await run(FakeDriver())

    assert result.healthy
    assert result.summary == "No partitioned tables found."
    assert result.metrics["partitioned_tables"] == 0


@pytest.mark.asyncio
async def test_a_well_kept_partitioned_table_is_healthy():
    """Partitions premade well ahead, an empty DEFAULT, few relations: nothing to say."""
    driver = FakeDriver(
        roots=[root(default_partition="public.events_default")],
        bounds=[
            bound("public.events_2026_10", "2026-10-01 00:00:00+00", "2026-11-01 00:00:00+00"),
            bound("public.events_2026_11", "2026-11-01 00:00:00+00", "2026-12-01 00:00:00+00"),
        ],
    )
    result = await run(driver)

    assert result.healthy, result.summary
    assert "1 partitioned table with 3 partitions examined." in result.summary


# -- Premade partitions -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_last_partition_that_ends_within_one_partition_width_is_flagged_with_the_next_one():
    driver = FakeDriver(
        roots=[root(default_partition="public.events_default")],
        bounds=[
            bound("public.events_2026_09", "2026-09-01 00:00:00+00", "2026-10-01 00:00:00+00"),
            bound("public.events_2026_10", "2026-10-01 00:00:00+00", "2026-11-01 00:00:00+00"),
        ],
    )
    result = await run(driver)

    (finding,) = result.findings
    assert finding["kind"] == "premade_partitions"
    assert finding["severity"] == "medium"
    assert finding["confidence"] == "proven"
    assert "17 days from now" in finding["message"]
    assert "DEFAULT partition public.events_default" in finding["message"]
    assert finding["evidence"]["last_partition"] == "public.events_2026_10"
    recipe = "\n".join(finding["recipe"])
    assert "CREATE TABLE public.events_2026_11 (LIKE public.events INCLUDING ALL EXCLUDING IDENTITY);" in recipe
    assert "FOR VALUES FROM ('2026-11-01 00:00:00+00:00') TO ('2026-12-01 00:00:00+00:00')" in recipe
    assert "ALTER TABLE public.events_default ADD CONSTRAINT" in recipe


@pytest.mark.asyncio
async def test_a_last_partition_that_has_ended_is_high_and_says_inserts_fail_without_a_default():
    driver = FakeDriver(
        roots=[root()],
        bounds=[bound("public.events_2026_08", "2026-08-01 00:00:00+00", "2026-09-01 00:00:00+00")],
    )
    result = await run(driver)

    (finding,) = result.findings
    assert finding["severity"] == "high"
    assert "ago" in finding["message"]
    assert "no partition of relation found for row" in finding["message"]


@pytest.mark.asyncio
async def test_a_maxvalue_partition_means_there_is_no_end_to_run_out_of():
    driver = FakeDriver(
        roots=[root()],
        bounds=[
            bound("public.events_2026_08", "2026-08-01 00:00:00+00", "2026-09-01 00:00:00+00"),
            {**bound("public.events_rest", "x", "y"), "bound": "FOR VALUES FROM ('2026-09-01 00:00:00+00') TO (MAXVALUE)"},
        ],
    )
    assert (await run(driver)).healthy


@pytest.mark.asyncio
async def test_a_date_key_is_measured_against_the_local_clock():
    driver = FakeDriver(
        roots=[root(key_types=["date"])],
        bounds=[bound("public.events_2026_10", "2026-10-01", "2026-11-01")],
    )
    (finding,) = (await run(driver)).findings
    assert finding["kind"] == "premade_partitions"
    assert "FOR VALUES FROM ('2026-11-01') TO ('2026-12-01')" in "\n".join(finding["recipe"])


@pytest.mark.asyncio
async def test_a_non_time_range_key_is_not_judged_by_the_clock():
    driver = FakeDriver(roots=[root(key_types=["int8"])], bounds=[bound("public.events_1", "1", "2")])
    assert (await run(driver)).healthy


# -- DEFAULT partitions ---------------------------------------------------------


def default_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "schema": "public",
        "table": "events",
        "table_ident": "public.events",
        "root": "public.events",
        "strategy": "r",
        "default_partition": "public.events_default",
        "default_is_partitioned": False,
        "live_rows": 0,
        "estimated_rows": 1200,
        "bytes": 81920,
        "checks": None,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_rows_in_the_default_partition_are_flagged_with_why_attach_will_scan_them():
    result = await run(FakeDriver(roots=[root(default_partition="public.events_default")], defaults=[default_row()]))

    (finding,) = result.findings
    assert finding["kind"] == "default_partition_rows"
    assert finding["severity"] == "low", "80 kB is a pause, not an outage"
    assert "about 1,200 rows" in finding["message"]
    assert "no validated CHECK constraint" in finding["message"]
    assert "a partition was not created in time" in finding["message"]


@pytest.mark.asyncio
async def test_the_estimate_is_preferred_to_the_counter_and_the_counter_used_when_never_analyzed():
    analyzed = await run(FakeDriver(roots=[root()], defaults=[default_row(live_rows=2400, estimated_rows=1200)]))
    never = await run(FakeDriver(roots=[root()], defaults=[default_row(live_rows=30, estimated_rows=-1)]))

    assert "about 1,200 rows" in analyzed.findings[0]["message"]
    assert "about 30 rows" in never.findings[0]["message"]


@pytest.mark.asyncio
async def test_an_empty_default_partition_is_not_a_finding():
    assert (await run(FakeDriver(roots=[root()], defaults=[default_row(estimated_rows=0, live_rows=0)]))).healthy


@pytest.mark.asyncio
async def test_a_large_default_is_medium_and_a_check_constraint_is_mentioned():
    row = default_row(bytes=2 * 1024**3, checks=["CHECK ((created_at < '2020-01-01'))"])
    (finding,) = (await run(FakeDriver(roots=[root()], defaults=[row]))).findings

    assert finding["severity"] == "medium"
    assert "1 validated CHECK constraint" in finding["message"]


# -- Unique per partition ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_unique_index_on_partitions_but_not_the_parent_is_one_candidate_finding():
    rows = [
        {
            "schema": "public",
            "table": "events",
            "table_ident": "public.events",
            "partition": f"public.events_{n}",
            "index": f"events_{n}_email_key",
            "is_primary": False,
            "has_expressions": False,
            "is_partial": False,
            "columns": ["email"],
        }
        for n in range(5)
    ]
    (finding,) = (await run(FakeDriver(roots=[root()], unique=rows))).findings

    assert finding["kind"] == "unique_per_partition"
    assert finding["confidence"] == "candidate"
    assert finding["column"] == "email"
    assert "on 5 partitions of public.events" in finding["message"]
    assert "and 2 more" in finding["message"]
    assert finding["evidence"]["partitions_with_index"] == 5


# -- Pruning ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pruning_switched_off_in_the_server_configuration_is_high():
    (finding,) = (await run(FakeDriver(roots=[root()], pruning="off", pruning_source="configuration file"))).findings

    assert finding["kind"] == "pruning_disabled"
    assert finding["severity"] == "high"
    assert "configuration file" in finding["message"]


@pytest.mark.asyncio
async def test_pruning_switched_off_for_a_role_is_reported_once_with_whom_it_applies_to():
    """pg-debug's own session shows a database setting too; that is the same finding, not a second one."""
    driver = FakeDriver(
        roots=[root()],
        pruning="off",
        pruning_source="database",
        overrides=[{"usename": "", "datname": "app", "value": "off"}, {"usename": "reporting", "datname": "", "value": "on"}],
    )
    (finding,) = (await run(driver)).findings

    assert finding["datname"] == "app"
    assert "database app" in finding["message"]


# -- Lock pressure ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "max_locks", "slots"),
    [(16, 64, 16), (17, 1000, 16), (18, 64, 64), (18, 100, 128), (18, 10, 16), (18, 10**7, 16384)],
)
def test_fast_path_slots_are_sixteen_before_18_and_scale_with_max_locks_from_18(version, max_locks, slots):
    assert fast_path_slots(version, max_locks) == slots


@pytest.mark.asyncio
async def test_a_tree_larger_than_the_fast_path_is_a_low_candidate_stated_as_the_worst_case_without_a_workload():
    driver = FakeDriver(roots=[root(leaves=40, relations=41, indexes=82, one_partition_locks=5)])
    (finding,) = (await run(driver)).findings

    assert finding["kind"] == "lock_pressure"
    assert finding["confidence"] == "candidate"
    assert finding["severity"] == "low"
    assert finding["evidence"]["estimated_locks"] == 123
    assert "pg_stat_statements could not be read" in finding["evidence"]["basis"]
    assert "fastpath" in finding["confirm"]


@pytest.mark.asyncio
async def test_a_workload_that_does_not_filter_on_the_key_makes_it_medium():
    driver = FakeDriver(
        roots=[root(leaves=40, relations=41, indexes=82, one_partition_locks=5)],
        statements=[{"query": "SELECT * FROM events WHERE user_id = $1", "calls": 10, "total_exec_time": 900.0}],
    )
    (finding,) = (await run(driver)).findings

    assert finding["severity"] == "medium"
    assert "100% of the recorded time" in finding["evidence"]["basis"]


@pytest.mark.asyncio
async def test_a_workload_that_always_filters_on_the_key_is_not_flagged():
    """Pruned to one partition, a query takes five locks; sixteen fit."""
    driver = FakeDriver(
        roots=[root(leaves=40, relations=41, indexes=82, one_partition_locks=5)],
        statements=[{"query": "SELECT * FROM events WHERE created_at >= $1", "calls": 10, "total_exec_time": 900.0}],
    )
    assert (await run(driver)).healthy


@pytest.mark.asyncio
async def test_on_18_a_larger_max_locks_per_transaction_holds_the_same_tree():
    driver = FakeDriver(roots=[root(leaves=40, relations=41, indexes=82)], version=180000, max_locks=128)
    result = await run(driver)

    assert result.healthy
    assert result.metrics["fast_path_slots"] == 128


# -- Shape --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_more_partitions_than_the_limit_is_stated_with_what_a_query_locks():
    driver = FakeDriver(roots=[root(leaves=12, relations=13, indexes=0, one_partition_locks=2)])
    (finding,) = (await run(driver, max_partitions=10)).findings

    assert finding["kind"] == "partition_count"
    assert "12 partitions, above 10" in finding["message"]


# -- Parsing --------------------------------------------------------------------------


def test_a_range_bound_is_split_into_its_two_ends():
    assert _range_bound("FOR VALUES FROM ('2026-01-01') TO ('2026-02-01')") == ("'2026-01-01'", "'2026-02-01'")
    assert _range_bound("FOR VALUES FROM (MINVALUE) TO ('2026-02-01')") == ("MINVALUE", "'2026-02-01'")
    assert _range_bound("FOR VALUES FROM ('a', 1) TO ('b', 2)") is None, "a two-column key is not one this reads"
    assert _range_bound("DEFAULT") is None


def test_a_bound_is_read_as_a_point_in_time_only_when_it_is_one():
    assert _point("'2026-02-01 00:00:00+00'", aware=True) == datetime(2026, 2, 1, tzinfo=UTC)
    assert _point("'2026-02-01'", aware=False) == datetime(2026, 2, 1)
    assert _point("MAXVALUE", aware=True) is None
    assert _point("'infinity'", aware=True) is None
    assert _point("'2026-02-01'", aware=True) is None, "a naive value is not compared with an aware clock"


def test_monthly_partitions_advance_by_calendar_months_and_others_by_their_width():
    assert _advance(datetime(2026, 2, 1), timedelta(days=31)) == datetime(2026, 3, 1)
    assert _advance(datetime(2026, 12, 1), timedelta(days=30)) == datetime(2027, 1, 1)
    assert _advance(datetime(2026, 3, 9), timedelta(days=7)) == datetime(2026, 3, 16)


def test_the_next_partition_is_named_after_the_last_one():
    assert _next_name("public.events_2026_10", datetime(2026, 11, 1)) == "public.events_2026_11"
    assert _next_name("public.events_p2026_10", datetime(2026, 11, 1)) == "public.events_2026_11"
    assert _next_name("app.events_20261009", datetime(2026, 10, 16)) == "app.events_2026_10_16"
    assert _next_name('app."Events_2026_10"', datetime(2026, 11, 1)) == 'app."Events_2026_11"'

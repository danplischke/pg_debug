"""The decision, and the arithmetic behind it.

Most of these are about the advisor declining. A tool that recommends
partitioning whenever a table is large and a timestamp exists will be right
often enough to be trusted and wrong often enough to hurt, so the cases that
must produce "no" are tested at least as hard as the ones that produce "yes".
"""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SqlDriver
from pg_debug.partition.advisor import MAX_LIST_VALUES
from pg_debug.partition.advisor import MAX_SHOWN_PARTITIONS
from pg_debug.partition.advisor import MIN_HASH_DISTINCT
from pg_debug.partition.advisor import PartitionAdvisor
from pg_debug.partition.advisor import _blockers
from pg_debug.partition.advisor import _ddl
from pg_debug.partition.advisor import _hash_modulus
from pg_debug.partition.advisor import _months
from pg_debug.partition.advisor import _parse_bound
from pg_debug.partition.advisor import _strategy_for
from pg_debug.partition.advisor import _weigh_workload
from pg_debug.partition.result import Strategy


def column(name="created_at", type_name="timestamptz", distinct=None, **extra):
    return {
        "column": name,
        "column_ident": name,
        "type_name": type_name,
        "column_type": type_name,
        "distinct_values": distinct,
        **extra,
    }


def workload(*queries):
    return [{"query": query, "weight": weight, "calls": 1} for query, weight in queries]


# -- Choosing a strategy ----------------------------------------------------


def test_a_timestamp_gets_a_range_partition():
    assert _strategy_for(column(), {"kinds": {"range"}}) is Strategy.RANGE


def test_a_column_nothing_restricts_on_supports_no_strategy():
    """Not "no good strategy" — no strategy at all. A key nothing filters on
    makes every query read every partition."""
    assert _strategy_for(column(), {}) is None
    assert _strategy_for(column(), {"kinds": set()}) is None


def test_a_low_cardinality_discriminator_gets_a_list_partition():
    assert _strategy_for(column("status", "text", distinct=4), {"kinds": {"equality"}}) is Strategy.LIST


def test_a_high_cardinality_column_queried_by_equality_gets_a_hash_partition():
    assert _strategy_for(column("tenant_id", "int4", distinct=MIN_HASH_DISTINCT), {"kinds": {"equality"}}) is Strategy.HASH


def test_a_range_query_rules_out_hash():
    """HASH prunes on equality and nothing else, so a column queried by range
    must not be handed a HASH partition — every such query would read the whole
    table through more relations than before."""
    assert _strategy_for(column("tenant_id", "int4", distinct=10_000), {"kinds": {"range"}}) is None
    assert _strategy_for(column("tenant_id", "int4", distinct=10_000), {"kinds": {"equality", "range"}}) is None


def test_a_cardinality_between_the_two_thresholds_supports_neither():
    """Too many values for one partition each, too few for HASH to spread them
    evenly. Saying nothing beats picking the less bad of two bad shapes."""
    between = (MAX_LIST_VALUES + MIN_HASH_DISTINCT) // 2

    assert _strategy_for(column("region", "text", distinct=between), {"kinds": {"equality"}}) is None


def test_a_type_that_cannot_be_a_sensible_key_is_refused():
    assert _strategy_for(column("payload", "jsonb", distinct=10_000), {"kinds": {"equality"}}) is None
    assert _strategy_for(column("amount", "numeric", distinct=10_000), {"kinds": {"equality"}}) is None


# -- Weighing the workload --------------------------------------------------


def test_time_is_split_between_what_touches_the_table_and_what_prunes():
    touching, pruning, _deleting = _weigh_workload(
        workload(
            ("SELECT * FROM events WHERE created_at > now()", 70.0),
            ("SELECT count(*) FROM events", 30.0),
        ),
        "events",
        "public.events",
    )

    assert touching == 100.0
    assert pruning["created_at"]["weight"] == 70.0


def test_a_query_against_another_table_is_not_counted():
    touching, _pruning, _deleting = _weigh_workload(
        workload(("SELECT * FROM orders WHERE created_at > now()", 500.0)),
        "events",
        "public.events",
    )

    assert touching == 0.0


def test_a_same_named_table_in_another_schema_is_a_different_table():
    """Counting its queries here would dilute the share of the table being
    assessed with time it never spent."""
    touching, _pruning, _deleting = _weigh_workload(
        workload(("SELECT * FROM archive.events WHERE created_at > now()", 500.0)),
        "events",
        "public.events",
    )

    assert touching == 0.0


def test_a_delete_that_prunes_is_recorded_separately():
    """It is the strongest argument there is, because the delete becomes a
    DROP rather than a scan and a vacuum."""
    _touching, _pruning, deleting = _weigh_workload(
        workload(("DELETE FROM events WHERE created_at < now()", 40.0)),
        "events",
        "public.events",
    )

    assert deleting["created_at"] == 40.0


def test_an_unparseable_query_does_not_stop_the_rest_being_weighed():
    touching, _pruning, _deleting = _weigh_workload(
        workload(
            ("this is not sql <<<", 999.0),
            ("SELECT * FROM events WHERE created_at > now()", 10.0),
        ),
        "events",
        "public.events",
    )

    assert touching == 10.0


# -- The verdict ------------------------------------------------------------


def advisor_with(tables, columns, keys, queries):
    """An advisor whose four reads are answered by content, not call order."""
    driver = MagicMock(spec=SqlDriver)
    advisor = PartitionAdvisor(driver, min_table_bytes=0)

    async def answer(query, params=None):
        if "FROM pg_extension e" in query:
            return [SqlDriver.RowResult(cells={"schema": "public", "version": "1.10"})]
        if "pg_stat_statements" in query:
            return [SqlDriver.RowResult(cells=row) for row in queries]
        if "indisunique" in query:
            return [SqlDriver.RowResult(cells=row) for row in keys]
        if "pg_stats" in query:
            return [SqlDriver.RowResult(cells=row) for row in columns]
        return [SqlDriver.RowResult(cells=row) for row in tables]

    driver.execute_query = AsyncMock(side_effect=answer)
    return advisor


def table_row(**overrides):
    base = {
        "schema": "public",
        "table": "events",
        "table_ident": "public.events",
        "table_bytes": 400 * 1024**3,
        "row_count": 2_000_000_000,
        "deletes": 0,
    }
    return {**base, **overrides}


def column_row(**overrides):
    base = {
        "schema": "public",
        "table": "events",
        "column": "created_at",
        "type_name": "timestamptz",
        "column_type": "timestamp with time zone",
        "column_ident": "created_at",
        "correlation": 0.99,
        "n_distinct": -1.0,
        "distinct_values": 2_000_000_000,
        "common_values": None,
        "bounds": ["2024-01-01 00:00:00+00", "2024-06-30 00:00:00+00"],
    }
    return {**base, **overrides}


def query_row(query, total_exec_time, calls=100):
    return {"query": query, "calls": calls, "total_exec_time": total_exec_time}


@pytest.mark.asyncio
async def test_a_table_the_workload_filters_on_is_recommended():
    advisor = advisor_with(
        [table_row()],
        [column_row()],
        [],
        [query_row("SELECT * FROM events WHERE created_at > now()", 900.0)],
    )

    candidate = (await advisor.analyze())[0]

    assert candidate.recommended is True
    assert candidate.strategy is Strategy.RANGE
    assert candidate.column == "created_at"


@pytest.mark.asyncio
async def test_a_large_table_the_workload_mostly_does_not_filter_on_is_refused():
    """The case the whole design turns on. 400 GB and a timestamp is not a
    reason; a third of the time pruning is a reason against."""
    advisor = advisor_with(
        [table_row()],
        [column_row()],
        [],
        [
            query_row("SELECT * FROM events WHERE created_at > now()", 300.0),
            query_row("SELECT count(*) FROM events", 700.0),
        ],
    )

    candidate = (await advisor.analyze())[0]

    assert candidate.recommended is False
    assert candidate.pruning_share == 0.3
    assert candidate.ddl == [], "DDL for a change advised against reads as endorsement"
    assert "would make more of this workload slower than it makes faster" in candidate.reasons[0]


@pytest.mark.asyncio
async def test_the_refused_table_still_reports_the_number_it_was_refused_on():
    """ "No" without the measurement is not an answer anybody can act on."""
    advisor = advisor_with(
        [table_row()],
        [column_row()],
        [],
        [
            query_row("SELECT * FROM events WHERE created_at > now()", 100.0),
            query_row("SELECT count(*) FROM events", 900.0),
        ],
    )

    candidate = (await advisor.analyze())[0]

    assert candidate.evidence["query_time_touching_table"] == 1000.0
    assert candidate.evidence["query_time_that_would_prune"] == 100.0


@pytest.mark.asyncio
async def test_the_threshold_can_be_moved():
    def build():
        return advisor_with(
            [table_row()],
            [column_row()],
            [],
            [
                query_row("SELECT * FROM events WHERE created_at > now()", 300.0),
                query_row("SELECT count(*) FROM events", 700.0),
            ],
        )

    strict = build()
    lenient = build()
    lenient.min_pruning_share = 0.2

    assert (await strict.analyze())[0].recommended is False
    assert (await lenient.analyze())[0].recommended is True


@pytest.mark.asyncio
async def test_a_table_no_recorded_query_touches_is_not_judged_at_all():
    """Silence beats a verdict reached from no evidence."""
    advisor = advisor_with([table_row()], [column_row()], [], [query_row("SELECT 1", 500.0)])

    assert await advisor.analyze() == []


@pytest.mark.asyncio
async def test_a_range_key_wins_a_tie_against_hash():
    """Both prune the same share, but only RANGE makes old data droppable."""
    advisor = advisor_with(
        [table_row()],
        [
            column_row(),
            column_row(column="tenant_id", type_name="int4", column_ident="tenant_id", distinct_values=10_000),
        ],
        [],
        [query_row("SELECT * FROM events WHERE created_at > now() AND tenant_id = 1", 900.0)],
    )

    candidate = (await advisor.analyze())[0]

    assert candidate.strategy is Strategy.RANGE
    assert candidate.column == "created_at"


@pytest.mark.asyncio
async def test_recommended_tables_are_reported_before_refused_ones():
    advisor = advisor_with(
        [table_row(), table_row(table="ledger", table_ident="public.ledger", table_bytes=800 * 1024**3)],
        [column_row(), column_row(table="ledger")],
        [],
        [
            query_row("SELECT * FROM events WHERE created_at > now()", 900.0),
            query_row("SELECT count(*) FROM ledger", 900.0),
            query_row("SELECT * FROM ledger WHERE created_at > now()", 100.0),
        ],
    )

    candidates = await advisor.analyze()

    assert [c.table for c in candidates] == ["events", "ledger"], "the larger but refused table comes second"


# -- Blockers ---------------------------------------------------------------


def test_a_key_that_already_contains_the_partition_column_is_not_a_blocker():
    assert _blockers([{"index_name": "events_pkey", "is_primary": True, "columns": ["id", "created_at"]}], "created_at") == []


def test_a_key_without_the_partition_column_is_named_as_a_blocker():
    blockers = _blockers([{"index_name": "events_pkey", "is_primary": True, "columns": ["id"]}], "created_at")

    assert len(blockers) == 1
    assert "events_pkey" in blockers[0]
    assert "primary key" in blockers[0]


# -- The DDL sketch ---------------------------------------------------------


def test_a_key_that_has_to_change_is_recreated_rather_than_copied():
    """INCLUDING ALL copies the old primary key, and PostgreSQL then rejects
    the CREATE outright. A sketch that cannot run is worse than no sketch."""
    statements = _ddl(
        table_row(),
        column_row(),
        Strategy.RANGE,
        [{"index_name": "events_pkey", "is_primary": True, "columns": ["id"]}],
    )
    script = "\n".join(statements)

    assert "EXCLUDING INDEXES" in script
    assert "ADD PRIMARY KEY (id, created_at)" in script


def test_a_table_whose_keys_already_fit_keeps_them():
    script = "\n".join(_ddl(table_row(), column_row(), Strategy.RANGE, []))

    assert "INCLUDING ALL)" in script
    assert "EXCLUDING INDEXES" not in script


def test_the_range_bounds_come_from_the_histogram():
    script = "\n".join(_ddl(table_row(), column_row(), Strategy.RANGE, []))

    assert "FOR VALUES FROM ('2024-01-01') TO ('2024-02-01')" in script
    assert "DEFAULT;" in script, "a row outside the sampled span has to land somewhere"


def test_only_a_few_partitions_are_written_out():
    statements = _ddl(table_row(), column_row(bounds=["2020-01-01 00:00:00+00", "2024-12-31 00:00:00+00"]), Strategy.RANGE, [])
    created = [s for s in statements if s.startswith("CREATE TABLE") and "PARTITION OF" in s and "DEFAULT" not in s]

    assert len(created) == MAX_SHOWN_PARTITIONS
    assert any("and 57 more" in s for s in statements)


def test_a_column_with_no_histogram_says_so_instead_of_inventing_bounds():
    script = "\n".join(_ddl(table_row(), column_row(bounds=None), Strategy.RANGE, []))

    assert "ANALYZE the table" in script


# -- Bounds arithmetic ------------------------------------------------------


def test_a_timestamptz_bound_is_parsed_despite_its_two_digit_offset():
    """PostgreSQL renders the offset as '+00', which is not ISO 8601 until it
    has minutes on it."""
    parsed = _parse_bound("2024-03-15 12:30:00.123456+00")

    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2024, 3, 15)


def test_a_date_bound_is_parsed():
    parsed = _parse_bound("2024-03-15")

    assert parsed is not None and parsed.month == 3


def test_a_bound_that_is_not_a_date_is_refused_rather_than_guessed_at():
    assert _parse_bound("not a date") is None


def bound(text):
    parsed = _parse_bound(text)
    assert parsed is not None, f"the fixture {text!r} has to parse for the test to mean anything"
    return parsed


def test_the_month_walk_covers_both_ends():
    months = _months(bound("2024-01-15"), bound("2024-03-02"))

    assert [(m.year, m.month) for m in months] == [(2024, 1), (2024, 2), (2024, 3)]


def test_the_month_walk_crosses_a_year():
    months = _months(bound("2023-11-05"), bound("2024-01-05"))

    assert [(m.year, m.month) for m in months] == [(2023, 11), (2023, 12), (2024, 1)]


def test_a_span_inside_one_month_still_produces_a_partition():
    assert len(_months(bound("2024-03-05"), bound("2024-03-06"))) == 1


# -- Hash modulus -----------------------------------------------------------


def test_the_modulus_grows_with_the_table():
    assert _hash_modulus(10 * 1024**3) < _hash_modulus(1000 * 1024**3)


def test_the_modulus_is_bounded():
    assert _hash_modulus(10_000 * 1024**4) <= 256

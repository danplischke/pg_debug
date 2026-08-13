"""The structured output of the individual health calculators."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.configuration import MIN_BLOCK_READS
from pg_debug.database_health import HealthCheckResult
from pg_debug.database_health.buffer_health_calc import BufferHealthCalc
from pg_debug.database_health.connection_health_calc import ConnectionHealthCalc
from pg_debug.database_health.constraint_health_calc import ConstraintHealthCalc
from pg_debug.database_health.index_health_calc import IndexHealthCalc
from pg_debug.database_health.vacuum_health_calc import VacuumHealthCalc
from pg_debug.sql import SqlDriver


def make_driver(*result_sets):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(side_effect=[None if rows is None else [SqlDriver.RowResult(cells=r) for r in rows] for rows in result_sets])
    return driver


def test_result_to_dict_round_trips():
    result = HealthCheckResult(
        name="Index bloat",
        healthy=False,
        summary="Bloated indexes found:",
        findings=[{"index": "orders_pkey"}],
        metrics={"min_bloat_bytes": 100},
    )

    assert result.to_dict() == {
        "name": "Index bloat",
        "healthy": False,
        "summary": "Bloated indexes found:",
        "findings": [{"index": "orders_pkey"}],
        "metrics": {"min_bloat_bytes": 100},
    }


def test_result_defaults_to_no_findings():
    result = HealthCheckResult(name="Vacuum health", healthy=True, summary="ok")
    assert result.findings == []
    assert result.metrics == {}


# -- Buffer -----------------------------------------------------------------


def buffer_row(rate, blocks=1_000_000):
    """A hit-rate row over enough blocks for the ratio to be worth reading."""
    return {"rate": rate, "hits": int(blocks * rate), "reads": blocks - int(blocks * rate)}


@pytest.mark.asyncio
async def test_buffer_hit_rate_above_threshold():
    result = await BufferHealthCalc(make_driver([buffer_row(0.99)])).index_hit_rate()

    assert result.name == "Buffer health for indexes"
    assert result.healthy is True
    assert result.summary == "Index cache hit rate: 99.0% (above 95.0% threshold) over 1,000,000 blocks"
    assert result.metrics["hit_rate_percent"] == pytest.approx(99.0)
    assert result.metrics["threshold_percent"] == 95.0


@pytest.mark.asyncio
async def test_buffer_hit_rate_below_threshold():
    result = await BufferHealthCalc(make_driver([buffer_row(0.5)])).table_hit_rate()

    assert result.name == "Buffer health for tables"
    assert result.healthy is False
    assert result.summary == "Table cache hit rate: 50.0% (below 95.0% threshold) over 1,000,000 blocks"
    assert result.metrics["hit_rate_percent"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_buffer_hit_rate_respects_custom_threshold():
    result = await BufferHealthCalc(make_driver([buffer_row(0.9)])).index_hit_rate(threshold=0.8)

    assert result.healthy is True
    assert result.metrics["threshold_percent"] == pytest.approx(80.0)


@pytest.mark.asyncio
async def test_a_hit_rate_over_too_few_blocks_is_not_reported_as_a_problem():
    """A server that has just started reads every page once and reports a
    terrible hit rate for as long as that takes. Calling it unhealthy sends
    someone to resize shared_buffers on a server with nothing wrong with it."""
    result = await BufferHealthCalc(make_driver([buffer_row(0.0, blocks=1082)])).table_hit_rate()

    assert result.healthy is True
    assert "not enough read to judge" in result.summary
    assert "1,082 blocks" in result.summary


@pytest.mark.asyncio
async def test_the_withheld_verdict_still_carries_the_numbers_behind_it():
    """Withholding a verdict is only useful if the reader can see how close it
    came, and check it again once the server has been up a while."""
    result = await BufferHealthCalc(make_driver([buffer_row(0.0, blocks=1082)])).table_hit_rate()

    assert result.metrics["blocks_read"] == 1082
    assert result.metrics["blocks_hit"] == 0
    assert result.metrics["hit_rate_percent"] == pytest.approx(0.0)
    assert result.metrics["minimum_blocks_to_judge"] == MIN_BLOCK_READS


@pytest.mark.asyncio
async def test_a_judged_hit_rate_reports_the_blocks_it_was_taken_over():
    """The same 0% means opposite things over a thousand blocks and over a
    billion, so the count travels with the ratio."""
    result = await BufferHealthCalc(make_driver([buffer_row(0.6, blocks=500_000)])).table_hit_rate()

    assert result.metrics["blocks_hit"] + result.metrics["blocks_read"] == 500_000
    assert "over 500,000 blocks" in result.summary


@pytest.mark.asyncio
async def test_buffer_hit_rate_without_statistics_is_not_a_problem():
    result = await BufferHealthCalc(make_driver([{"rate": None}])).index_hit_rate()

    assert result.healthy is True
    assert result.summary == "No index cache statistics available."
    assert result.metrics["hit_rate_percent"] is None


# -- Connections ------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_health_reports_counts():
    result = await ConnectionHealthCalc(make_driver([{"count": 7}], [{"count": 2}])).connection_health_check()

    assert result.healthy is True
    assert result.summary == "Connections healthy: 7 total, 2 idle"
    assert result.metrics == {
        "total_connections": 7,
        "idle_connections": 2,
        "max_total_connections": 500,
        "max_idle_connections": 100,
    }


@pytest.mark.asyncio
async def test_connection_health_flags_too_many_connections():
    calc = ConnectionHealthCalc(make_driver([{"count": 600}], [{"count": 2}]), max_total_connections=500)

    result = await calc.connection_health_check()

    assert result.healthy is False
    assert result.summary == "High number of connections: 600"


@pytest.mark.asyncio
async def test_connection_health_flags_idle_in_transaction():
    calc = ConnectionHealthCalc(make_driver([{"count": 10}], [{"count": 200}]), max_idle_connections=100)

    result = await calc.connection_health_check()

    assert result.healthy is False
    assert result.summary == "High number of connections idle in transaction: 200"


# -- Constraints ------------------------------------------------------------


@pytest.mark.asyncio
async def test_constraint_health_when_clean():
    result = await ConstraintHealthCalc(make_driver([])).invalid_constraints_check()

    assert result.healthy is True
    assert result.summary == "No invalid constraints found."
    assert result.findings == []


@pytest.mark.asyncio
async def test_constraint_health_lists_invalid_constraints():
    rows = [
        {
            "schema": "public",
            "table": "orders",
            "name": "orders_customer_fk",
            "referenced_schema": "public",
            "referenced_table": "customers",
        }
    ]

    result = await ConstraintHealthCalc(make_driver(rows)).invalid_constraints_check()

    assert result.healthy is False
    assert "orders_customer_fk" in result.summary
    assert result.findings == rows


# -- Vacuum -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_vacuum_health_without_candidates():
    result = await VacuumHealthCalc(make_driver([])).transaction_id_danger_check()

    assert result.healthy is True
    assert result.summary == "No tables found with transaction ID wraparound danger."
    assert result.metrics["threshold"] == 10000000


@pytest.mark.asyncio
async def test_vacuum_health_flags_tables_near_wraparound():
    rows = [{"schema": "public", "table": "orders", "transactions_left": 5}]

    result = await VacuumHealthCalc(make_driver(rows)).transaction_id_danger_check()

    assert result.healthy is False
    assert "public.orders" in result.summary
    assert result.findings == [{"schema": "public", "table": "orders", "transactions_left": 5, "is_healthy": False}]


@pytest.mark.asyncio
async def test_vacuum_health_when_all_tables_are_above_threshold():
    rows = [{"schema": "public", "table": "orders", "transactions_left": 20000000}]

    result = await VacuumHealthCalc(make_driver(rows)).transaction_id_danger_check()

    assert result.healthy is True
    assert result.summary == "All tables have healthy transaction ID age."
    assert result.findings == []


# -- Indexes ----------------------------------------------------------------


def index_row(**overrides):
    row = {
        "schema": "public",
        "table": "orders",
        "name": "orders_status_idx",
        "columns": "status",
        "using": "btree",
        "unique": False,
        "primary": False,
        "valid": True,
        "indexprs": None,
        "indpred": None,
        "definition": "CREATE INDEX orders_status_idx ON public.orders USING btree (status)",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_invalid_index_check_when_clean():
    result = await IndexHealthCalc(make_driver([index_row()])).invalid_index_check()

    assert result.name == "Invalid index check"
    assert result.healthy is True
    assert result.findings == []


@pytest.mark.asyncio
async def test_invalid_index_check_lists_the_index():
    result = await IndexHealthCalc(make_driver([index_row(valid=False)])).invalid_index_check()

    assert result.healthy is False
    assert "orders_status_idx on orders is invalid." in result.summary
    assert result.findings == [
        {
            "schema": "public",
            "table": "orders",
            "index": "orders_status_idx",
            "definition": "CREATE INDEX orders_status_idx ON public.orders USING btree (status)",
        }
    ]


@pytest.mark.asyncio
async def test_duplicate_index_check_reports_the_covering_index():
    rows = [
        index_row(name="orders_status_idx", columns="status"),
        index_row(name="orders_status_created_idx", columns="status, created_at"),
    ]

    result = await IndexHealthCalc(make_driver(rows)).duplicate_index_check()

    assert result.healthy is False
    assert result.findings == [
        {
            "schema": "public",
            "table": "orders",
            "index": "orders_status_idx",
            "definition": "CREATE INDEX orders_status_idx ON public.orders USING btree (status)",
            "covered_by": "orders_status_created_idx",
        }
    ]


@pytest.mark.asyncio
async def test_unused_index_check_reports_scans_and_size():
    rows = [
        {
            "schema": "public",
            "table": "orders",
            "index": "orders_status_idx",
            "size_bytes": 2 * 1024 * 1024,
            "index_scans": 3,
            "definition": "CREATE INDEX orders_status_idx ON public.orders USING btree (status)",
            "primary": False,
        }
    ]

    result = await IndexHealthCalc(make_driver(rows)).unused_indexes()

    assert result.name == "Unused index check"
    assert result.healthy is False
    assert "scanned 3 times and uses 2.0MB" in result.summary
    assert result.findings == rows
    assert result.metrics == {"max_scans": 50}


@pytest.mark.asyncio
async def test_unused_index_check_when_clean():
    result = await IndexHealthCalc(make_driver([])).unused_indexes()

    assert result.healthy is True
    assert result.summary == "No unused indexes found."


@pytest.mark.asyncio
async def test_index_bloat_reports_wasted_space():
    rows = [
        {
            "schema": "public",
            "table": "orders",
            "index": "orders_pkey",
            "bloat_bytes": 200 * 1024 * 1024,
            "index_bytes": 500 * 1024 * 1024,
            "definition": "CREATE UNIQUE INDEX orders_pkey ON public.orders USING btree (id)",
            "primary": True,
        }
    ]

    result = await IndexHealthCalc(make_driver(rows)).index_bloat()

    assert result.healthy is False
    assert "200.0MB bloat out of 500.0MB total size" in result.summary
    assert result.findings == rows
    assert result.metrics == {"min_bloat_bytes": 104857600}

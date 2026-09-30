"""The column-order rule: what it claims, and how sure it says it is."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SqlDriver
from pg_debug.model import Confidence
from pg_debug.model import ReviewOptions
from pg_debug.model import Severity
from pg_debug.model.layout import COLUMN_ORDER_PADDING_SQL
from pg_debug.model.layout import HIGH_PADDING_BYTES
from pg_debug.model.layout import MEDIUM_PADDING_BYTES
from pg_debug.model.layout import MIN_PADDING_BYTES
from pg_debug.model.layout import column_order_padding
from pg_debug.queries.tuple_layout import DECLARED_ORDER
from pg_debug.queries.tuple_layout import PACKED_ORDER
from pg_debug.queries.tuple_layout import row_layout_ctes


def driver_returning(rows):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=cells) for cells in rows])
    return driver


def padding_row(**overrides):
    cells = {
        "schema": "public",
        "table": "events",
        "table_ident": "public.events",
        "columns": ["b", "d", "f", "a", "c", "e"],
        "column_definitions": "b bigint, d bigint, f bigint, a boolean, c boolean, e boolean",
        "all_fixed_width": True,
        "current_row_bytes": 76,
        "packed_row_bytes": 60,
        "saved_bytes_per_row": 16,
        "row_count": 200_000,
        "current_pages": 1870,
        "packed_pages": 1471,
        "impact_bytes": 3_268_608,
        "table_bytes": 15_319_040,
    }
    return {**cells, **overrides}


@pytest.mark.asyncio
async def test_the_recommended_order_is_the_useful_part_of_the_finding():
    findings = await column_order_padding(driver_returning([padding_row()]), ReviewOptions())

    finding = findings[0]
    assert finding.columns == ["b", "d", "f", "a", "c", "e"]
    assert "CREATE TABLE public.events_packed (b bigint, d bigint, f bigint, a boolean, c boolean, e boolean);" in finding.remediation
    assert finding.impact_bytes == 3_268_608


@pytest.mark.asyncio
async def test_the_rewrite_it_needs_is_stated_rather_than_implied():
    """A generated CREATE TABLE that silently drops constraints would be worse
    than no remediation at all."""
    findings = await column_order_padding(driver_returning([padding_row()]), ReviewOptions())

    remediation = " ".join(findings[0].remediation)
    assert "cannot reorder columns in place" in remediation
    assert "Constraints, defaults, indexes" in remediation


@pytest.mark.asyncio
async def test_a_fixed_width_table_is_measured_not_guessed():
    findings = await column_order_padding(driver_returning([padding_row()]), ReviewOptions())

    assert findings[0].confidence is Confidence.PROVEN
    assert "exact" in findings[0].detail
    assert findings[0].verify is None


@pytest.mark.asyncio
async def test_a_variable_width_table_is_only_a_candidate():
    """pg_stats records one average width, so the packing is an estimate."""
    findings = await column_order_padding(driver_returning([padding_row(all_fixed_width=False)]), ReviewOptions())

    assert findings[0].confidence is Confidence.CANDIDATE
    assert "estimate" in findings[0].detail
    assert findings[0].verify is not None


@pytest.mark.asyncio
async def test_the_detail_reports_pages_because_that_is_what_is_saved():
    findings = await column_order_padding(driver_returning([padding_row()]), ReviewOptions())

    assert "1,870 pages instead of 1,471" in findings[0].detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("impact_bytes", "expected"),
    [
        (HIGH_PADDING_BYTES, Severity.HIGH),
        (MEDIUM_PADDING_BYTES, Severity.MEDIUM),
        (MEDIUM_PADDING_BYTES - 1, Severity.LOW),
    ],
)
async def test_severity_follows_the_size_of_the_saving(impact_bytes, expected):
    findings = await column_order_padding(driver_returning([padding_row(impact_bytes=impact_bytes)]), ReviewOptions())

    assert findings[0].severity is expected


@pytest.mark.asyncio
async def test_the_thresholds_are_bound_in_the_order_the_query_expects():
    driver = driver_returning([])

    await column_order_padding(driver, ReviewOptions(min_rows=5000))

    query = driver.execute_query.call_args[0][0]
    assert str(MIN_PADDING_BYTES) in query
    assert "5000" in query


def test_the_saving_is_computed_in_pages_not_in_bytes_a_row():
    """Rows do not straddle pages, so a few bytes off the row can be worth no
    pages at all — or rather more than the row arithmetic suggests."""
    assert "current_pages > p.packed_pages" in COLUMN_ORDER_PADDING_SQL.replace("p.current_pages", "current_pages")
    assert "* p.block_size)::bigint AS impact_bytes" in COLUMN_ORDER_PADDING_SQL


def test_both_layouts_come_from_the_shared_tuple_layout():
    """If this rule computed its own row width it could recommend a packing the
    bloat check would then report as bloat."""
    assert row_layout_ctes("c.relkind IN ('r', 'm') AND c.reltuples > 0", (DECLARED_ORDER, PACKED_ORDER)) in COLUMN_ORDER_PADDING_SQL

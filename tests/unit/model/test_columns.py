"""Column rules, and the narrowness that keeps two of them useful."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.model import Confidence
from pg_debug.model import ReviewOptions
from pg_debug.model import Severity
from pg_debug.model.columns import BLANK_PADDED_CHAR_SQL
from pg_debug.model.columns import LOW_CARDINALITY_TEXT_SQL
from pg_debug.model.columns import MAX_DISTINCT_VALUES
from pg_debug.model.columns import NAIVE_TIMESTAMP_SQL
from pg_debug.model.columns import NULLABLE_NEVER_NULL_SQL
from pg_debug.model.columns import blank_padded_chars
from pg_debug.model.columns import low_cardinality_text
from pg_debug.model.columns import naive_timestamps
from pg_debug.model.columns import nullable_never_null
from pg_debug.sql import SqlDriver


def driver_returning(rows):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=cells) for cells in rows])
    return driver


def column_row(**overrides):
    cells = {
        "schema": "public",
        "table": "orders",
        "column": "created_at",
        "table_ident": "public.orders",
        "column_ident": "created_at",
        "column_type": "timestamp without time zone",
        "row_count": 100_000,
        "table_bytes": 8 * 1024 * 1024,
    }
    return {**cells, **overrides}


# -- Types ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_naive_timestamp_names_the_zone_the_conversion_assumes():
    """Converting without saying what the old values meant is how a column
    silently shifts by a few hours."""
    findings = await naive_timestamps(driver_returning([column_row()]), ReviewOptions())

    assert findings[0].confidence is Confidence.PROVEN
    assert findings[0].severity is Severity.MEDIUM
    assert findings[0].remediation == ["ALTER TABLE public.orders ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC';"]
    assert "assumes the existing values were written in UTC" in findings[0].detail


def test_timestamptz_is_not_mistaken_for_the_naive_type():
    """Their type names differ by four characters; matching loosely would flag
    every correctly typed column in the database."""
    assert "t.typname = 'timestamp'" in NAIVE_TIMESTAMP_SQL
    assert "timestamptz" not in NAIVE_TIMESTAMP_SQL


@pytest.mark.asyncio
async def test_a_blank_padded_char_keeps_its_declared_length():
    row = column_row(column="code", column_ident="code", column_type="character(10)", declared_length=10)

    findings = await blank_padded_chars(driver_returning([row]), ReviewOptions())

    assert findings[0].remediation == ["ALTER TABLE public.orders ALTER COLUMN code TYPE varchar(10);"]


def test_the_one_byte_internal_char_type_is_not_flagged():
    """`"char"` is a different type from `char(n)` and is not blank-padded."""
    assert "t.typname = 'bpchar'" in BLANK_PADDED_CHAR_SQL


# -- Nullability ------------------------------------------------------------


def never_null_row(**overrides):
    cells = column_row(
        column="customer_id",
        column_ident="customer_id",
        column_type="bigint",
        null_frac=0.0,
        constraint_name="orders_customer_id_not_null",
        last_analyzed="2026-08-12 09:00:00+00",
        in_foreign_key=True,
        in_unique_index=False,
    )
    return {**cells, **overrides}


@pytest.mark.asyncio
async def test_the_not_null_path_avoids_a_long_exclusive_lock():
    """SET NOT NULL on its own scans the table under ACCESS EXCLUSIVE; a
    validated CHECK first means it does not have to."""
    findings = await nullable_never_null(driver_returning([never_null_row()]), ReviewOptions())

    remediation = findings[0].remediation
    assert remediation[0].endswith("CHECK (customer_id IS NOT NULL) NOT VALID;")
    assert "VALIDATE CONSTRAINT" in remediation[1]
    assert "SET NOT NULL" in remediation[2]
    assert "DROP CONSTRAINT" in remediation[3]


@pytest.mark.asyncio
async def test_it_is_a_candidate_and_carries_the_query_that_settles_it():
    findings = await nullable_never_null(driver_returning([never_null_row()]), ReviewOptions())

    assert findings[0].confidence is Confidence.CANDIDATE
    assert findings[0].verify == "SELECT count(*) FROM public.orders WHERE customer_id IS NULL;"


@pytest.mark.asyncio
async def test_a_never_null_foreign_key_is_described_as_a_mandatory_relationship():
    findings = await nullable_never_null(driver_returning([never_null_row()]), ReviewOptions())

    assert "the schema says the relationship is optional while every row says it is required" in findings[0].detail


@pytest.mark.asyncio
async def test_a_nullable_unique_column_is_described_as_a_gap_in_the_constraint():
    row = never_null_row(in_foreign_key=False, in_unique_index=True)

    findings = await nullable_never_null(driver_returning([row]), ReviewOptions())

    assert "uniqueness does not apply between NULLs" in findings[0].detail


@pytest.mark.asyncio
async def test_the_statistics_are_stamped_with_when_they_were_collected():
    """A candidate drawn from stale numbers should look like one."""
    findings = await nullable_never_null(driver_returning([never_null_row()]), ReviewOptions())

    assert findings[0].evidence["last_analyzed"] == "2026-08-12 09:00:00+00"
    assert "2026-08-12 09:00:00+00" in findings[0].detail


def test_only_key_columns_are_considered():
    """Most columns in most databases hold no NULLs. Reporting all of them
    would bury the review in findings that say nothing."""
    assert "con.contype = 'f'" in NULLABLE_NEVER_NULL_SQL
    assert "i.indisunique" in NULLABLE_NEVER_NULL_SQL


# -- Cardinality ------------------------------------------------------------


def low_cardinality_row(**overrides):
    cells = column_row(
        column="status",
        column_ident="status",
        column_type="text",
        n_distinct=3.0,
        avg_width=6,
        distinct_values=3,
        common_values=["paid", "open", "void"],
        enum_type_ident="public.orders_status",
    )
    return {**cells, **overrides}


@pytest.mark.asyncio
async def test_the_enum_is_built_from_the_values_the_sample_saw():
    findings = await low_cardinality_text(driver_returning([low_cardinality_row()]), ReviewOptions())

    remediation = findings[0].remediation
    assert "CREATE TYPE public.orders_status AS ENUM ('open', 'paid', 'void');" in remediation
    assert findings[0].evidence["sampled_values"] == ["open", "paid", "void"]


@pytest.mark.asyncio
async def test_the_enum_is_created_in_the_same_schema_as_its_table():
    """An unqualified type name lands wherever the search_path points."""
    findings = await low_cardinality_text(driver_returning([low_cardinality_row()]), ReviewOptions())

    assert "CREATE TYPE public.orders_status" in " ".join(findings[0].remediation)


@pytest.mark.asyncio
async def test_it_says_why_the_conversion_might_fail():
    findings = await low_cardinality_text(driver_returning([low_cardinality_row()]), ReviewOptions())

    assert findings[0].confidence is Confidence.CANDIDATE
    assert "a value the sample missed will make the conversion fail" in findings[0].detail
    assert findings[0].verify == "SELECT status, count(*) FROM public.orders GROUP BY 1 ORDER BY 2 DESC;"


@pytest.mark.asyncio
async def test_the_saving_is_the_difference_from_an_integer_key():
    findings = await low_cardinality_text(driver_returning([low_cardinality_row()]), ReviewOptions())

    assert findings[0].impact_bytes == (6 - 4) * 100_000


@pytest.mark.asyncio
async def test_a_column_with_no_sampled_values_still_suggests_the_two_options():
    findings = await low_cardinality_text(driver_returning([low_cardinality_row(common_values=None)]), ReviewOptions())

    assert "lookup table" in " ".join(findings[0].remediation)
    assert not any(line.startswith("CREATE TYPE") for line in findings[0].remediation)


@pytest.mark.asyncio
async def test_the_cardinality_limit_is_bound_to_the_query():
    driver = driver_returning([])

    await low_cardinality_text(driver, ReviewOptions())

    assert str(MAX_DISTINCT_VALUES) in driver.execute_query.call_args[0][0]


def test_a_negative_n_distinct_is_read_as_a_proportion():
    """Reading the sign wrongly is how a column with every value distinct gets
    mistaken for a category — the same mistake the index advisor once made."""
    assert "WHEN s.n_distinct > 0 THEN s.n_distinct ELSE -s.n_distinct * c.reltuples END" in LOW_CARDINALITY_TEXT_SQL


def test_a_column_narrower_than_an_integer_key_has_nothing_to_gain():
    assert "s.avg_width > 4" in LOW_CARDINALITY_TEXT_SQL

"""The json, primary key overflow and mostly-NULL index rules: what each reports, and from what."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.model import Confidence
from pg_debug.model import ReviewOptions
from pg_debug.model import Severity
from pg_debug.model.columns import JSON_COLUMN_SQL
from pg_debug.model.columns import json_columns
from pg_debug.model.indexes import MOSTLY_NULL_FRACTION
from pg_debug.model.indexes import MOSTLY_NULL_INDEX_SQL
from pg_debug.model.indexes import mostly_null_indexes
from pg_debug.model.keys import PRIMARY_KEY_OVERFLOW_SQL
from pg_debug.model.keys import primary_key_overflow
from pg_debug.queries.sequences import INTEGER_MAX


def driver_returning(rows):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=cells) for cells in rows])
    return driver


# -- json ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_json_column_is_a_candidate_with_a_query_for_what_jsonb_would_change():
    row = {
        "schema": "public",
        "table": "events",
        "column": "payload",
        "table_ident": "public.events",
        "column_ident": "payload",
        "column_type": "json",
        "row_count": 10,
        "table_bytes": 8192,
    }

    [finding] = await json_columns(driver_returning([row]), ReviewOptions())

    assert finding.confidence is Confidence.CANDIDATE
    assert finding.severity is Severity.LOW
    assert finding.remediation == ["ALTER TABLE public.events ALTER COLUMN payload TYPE jsonb USING payload::jsonb;"]
    assert finding.verify is not None and "jsonb_object_keys(payload::jsonb)" in finding.verify


def test_jsonb_is_not_mistaken_for_json():
    assert "t.typname = 'json'" in JSON_COLUMN_SQL
    assert "jsonb" not in JSON_COLUMN_SQL


# -- Primary key overflow ----------------------------------------------------


def key_row(**overrides):
    cells = {
        "schema": "public",
        "table": "orders",
        "column": "id",
        "table_ident": "public.orders",
        "column_ident": "id",
        "column_type": "integer",
        "sequence_ident": "public.orders_id_seq",
        "is_identity": False,
        "sequence_type": "integer",
        "sequence_max": INTEGER_MAX,
        "last_value": 1_800_000_000,
        "row_count": 1_000_000,
        "table_bytes": 10**9,
        "referencing_columns": ["public.lines.order_id"],
        "referencing_alters": ["ALTER TABLE public.lines ALTER COLUMN order_id TYPE bigint;"],
    }
    return {**cells, **overrides}


@pytest.mark.asyncio
async def test_a_serial_key_past_eighty_percent_is_high_and_widens_its_sequence_and_references():
    [finding] = await primary_key_overflow(driver_returning([key_row()]), ReviewOptions())

    assert finding.confidence is Confidence.PROVEN
    assert finding.severity is Severity.HIGH
    assert finding.remediation == [
        "ALTER TABLE public.orders ALTER COLUMN id TYPE bigint;",
        "ALTER SEQUENCE public.orders_id_seq AS bigint;",
        "ALTER TABLE public.lines ALTER COLUMN order_id TYPE bigint;",
    ]
    assert finding.evidence["limited_by"] == "column"


@pytest.mark.asyncio
async def test_an_identity_key_does_not_widen_its_sequence_separately():
    """ALTER COLUMN TYPE on an identity column changes its sequence's type with it."""
    [finding] = await primary_key_overflow(
        driver_returning([key_row(is_identity=True, last_value=1_200_000_000, referencing_alters=None, referencing_columns=None)]), ReviewOptions()
    )

    assert finding.severity is Severity.MEDIUM
    assert finding.remediation == ["ALTER TABLE public.orders ALTER COLUMN id TYPE bigint;"]


@pytest.mark.asyncio
async def test_a_bigint_key_whose_serial_sequence_is_still_integer_stops_at_the_sequence():
    """Widening the column and forgetting the sequence leaves the same limit."""
    row = key_row(column_type="bigint", last_value=2_000_000_000, referencing_alters=None, referencing_columns=None)

    [finding] = await primary_key_overflow(driver_returning([row]), ReviewOptions())

    assert finding.evidence["limited_by"] == "sequence"
    assert finding.remediation == ["ALTER SEQUENCE public.orders_id_seq AS bigint;"]


@pytest.mark.asyncio
async def test_a_smallint_key_is_judged_against_the_smallint_limit():
    row = key_row(column_type="smallint", sequence_type="smallint", sequence_max=32_767, last_value=20_000)

    [finding] = await primary_key_overflow(driver_returning([row]), ReviewOptions())

    assert finding.evidence["limit"] == 32_767
    assert finding.severity is Severity.MEDIUM


@pytest.mark.asyncio
@pytest.mark.parametrize("last_value", [None, 1_000_000_000])
async def test_an_unread_or_half_used_sequence_is_not_reported(last_value):
    """NULL is a sequence never called or one the role may not read: no number, no verdict."""
    assert await primary_key_overflow(driver_returning([key_row(last_value=last_value)]), ReviewOptions()) == []


def test_identity_and_serial_columns_are_both_found_through_pg_depend():
    """An identity column has no nextval() default to parse."""
    assert "d.deptype IN ('a', 'i')" in PRIMARY_KEY_OVERFLOW_SQL
    assert "pg_sequences" in PRIMARY_KEY_OVERFLOW_SQL


# -- Mostly-NULL indexes -------------------------------------------------------


def index_row(**overrides):
    cells = {
        "schema": "public",
        "table": "users",
        "column": "deleted_at",
        "index": "users_deleted_at_idx",
        "table_ident": "public.users",
        "index_ident": "public.users_deleted_at_idx",
        "index_name_ident": "users_deleted_at_idx",
        "partial_name_ident": "users_deleted_at_idx_not_null",
        "partial_ident": "public.users_deleted_at_idx_not_null",
        "column_ident": "deleted_at",
        "index_definition": "CREATE INDEX users_deleted_at_idx ON public.users USING btree (deleted_at)",
        "null_frac": 0.97,
        "row_count": 2_000_000,
        "index_bytes": 40 * 1024 * 1024,
        "idx_scan": 12,
        "last_analyzed": "2026-10-01 00:00:00+00",
    }
    return {**cells, **overrides}


@pytest.mark.asyncio
async def test_a_mostly_null_index_is_rebuilt_partial_under_its_own_name():
    [finding] = await mostly_null_indexes(driver_returning([index_row()]), ReviewOptions())

    assert finding.confidence is Confidence.CANDIDATE
    assert finding.remediation == [
        "CREATE INDEX CONCURRENTLY users_deleted_at_idx_not_null ON public.users USING btree (deleted_at) WHERE deleted_at IS NOT NULL;",
        "DROP INDEX CONCURRENTLY public.users_deleted_at_idx;",
        "ALTER INDEX public.users_deleted_at_idx_not_null RENAME TO users_deleted_at_idx;",
    ]
    assert finding.impact_bytes == int(40 * 1024 * 1024 * 0.97)
    assert "IS NULL" in finding.detail


@pytest.mark.asyncio
async def test_a_definition_it_cannot_rewrite_is_described_rather_than_mangled():
    row = index_row(index_definition="CREATE INDEX something_else ON public.users USING btree (deleted_at)")

    [finding] = await mostly_null_indexes(driver_returning([row]), ReviewOptions())

    assert finding.remediation[0].startswith("-- Recreate")


@pytest.mark.asyncio
async def test_the_thresholds_are_passed_to_the_query():
    driver = driver_returning([])

    await mostly_null_indexes(driver, ReviewOptions(min_rows=500))

    statement = str(driver.execute_query.call_args.args[0])
    assert "c.reltuples >= 500" in statement
    assert f"s.null_frac >= {MOSTLY_NULL_FRACTION}" in statement


def test_unique_constraint_and_expression_indexes_are_left_alone():
    assert "NOT i.indisunique" in MOSTLY_NULL_INDEX_SQL
    assert "con.conindid = i.indexrelid" in MOSTLY_NULL_INDEX_SQL
    assert "i.indkey[0] <> 0" in MOSTLY_NULL_INDEX_SQL
    assert "i.indpred IS NULL" in MOSTLY_NULL_INDEX_SQL


@pytest.mark.parametrize(
    "statement",
    [
        JSON_COLUMN_SQL,
        PRIMARY_KEY_OVERFLOW_SQL,
        SafeSqlDriver.param_sql_to_query(MOSTLY_NULL_INDEX_SQL, [1000, MOSTLY_NULL_FRACTION]),
    ],
)
def test_every_new_statement_passes_restricted_mode(statement):
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(statement)

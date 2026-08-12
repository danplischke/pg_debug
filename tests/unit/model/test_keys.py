"""Key rules: what they report, and what their SQL must never stop saying."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.model import Confidence
from pg_debug.model import ReviewOptions
from pg_debug.model import Severity
from pg_debug.model.keys import FOREIGN_KEY_TYPE_MISMATCH_SQL
from pg_debug.model.keys import MISSING_PRIMARY_KEY_SQL
from pg_debug.model.keys import SIGNIFICANT_TABLE_BYTES
from pg_debug.model.keys import UNINDEXED_FOREIGN_KEY_SQL
from pg_debug.model.keys import foreign_key_type_mismatches
from pg_debug.model.keys import missing_primary_keys
from pg_debug.model.keys import unindexed_foreign_keys
from pg_debug.sql import SqlDriver


def driver_returning(rows):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=cells) for cells in rows])
    return driver


def fk_row(**overrides):
    cells = {
        "schema": "public",
        "table": "orders",
        "constraint_name": "orders_customer_id_fkey",
        "columns": ["customer_id"],
        "column_list": "customer_id",
        "table_ident": "public.orders",
        "referenced_schema": "public",
        "referenced_table": "customers",
        "row_count": 1_000_000,
        "table_bytes": 500 * 1024 * 1024,
        "referenced_deletes": 4_200,
        "referenced_updates": 10,
    }
    return {**cells, **overrides}


# -- Unindexed foreign keys -------------------------------------------------


@pytest.mark.asyncio
async def test_an_unindexed_key_reports_the_index_that_fixes_it():
    findings = await unindexed_foreign_keys(driver_returning([fk_row()]), ReviewOptions())

    assert len(findings) == 1
    finding = findings[0]
    assert finding.confidence is Confidence.PROVEN
    assert finding.columns == ["customer_id"]
    assert finding.remediation == ["CREATE INDEX CONCURRENTLY ON public.orders (customer_id);"]
    assert "4,200 deletes" in finding.detail


@pytest.mark.asyncio
async def test_a_big_child_with_parent_deletes_is_the_urgent_case():
    findings = await unindexed_foreign_keys(driver_returning([fk_row()]), ReviewOptions())

    assert findings[0].severity is Severity.HIGH


@pytest.mark.asyncio
async def test_without_recorded_deletes_the_cost_is_called_latent():
    findings = await unindexed_foreign_keys(driver_returning([fk_row(referenced_deletes=0)]), ReviewOptions())

    assert findings[0].severity is Severity.MEDIUM
    assert "latent" in findings[0].detail


@pytest.mark.asyncio
async def test_a_small_child_is_reported_but_not_urgent():
    """The finding is still true; scanning a 40-row table is just not a problem."""
    findings = await unindexed_foreign_keys(driver_returning([fk_row(table_bytes=SIGNIFICANT_TABLE_BYTES - 1)]), ReviewOptions())

    assert findings[0].severity is Severity.LOW


@pytest.mark.asyncio
async def test_a_composite_key_is_indexed_as_one_index():
    findings = await unindexed_foreign_keys(
        driver_returning([fk_row(columns=["a", "c"], column_list="a, c")]),
        ReviewOptions(),
    )

    assert findings[0].remediation == ["CREATE INDEX CONCURRENTLY ON public.orders (a, c);"]


def test_only_an_index_that_answers_the_check_counts():
    """These four conditions are the whole difference between this rule being
    right and it reporting keys that are perfectly well indexed."""
    # Set equality on the leading columns: a btree on (b, a) serves {a, b}.
    assert "<@ con.conkey::int2[]" in UNINDEXED_FOREIGN_KEY_SQL
    assert "@> con.conkey::int2[]" in UNINDEXED_FOREIGN_KEY_SQL
    # An invalid index answers nothing.
    assert "i.indisvalid" in UNINDEXED_FOREIGN_KEY_SQL
    # A partial index does not cover the rows the check looks at.
    assert "i.indpred IS NULL" in UNINDEXED_FOREIGN_KEY_SQL
    # An expression index cannot be looked up by column value.
    assert "i.indexprs IS NULL" in UNINDEXED_FOREIGN_KEY_SQL
    # INCLUDE columns are payload, not key.
    assert "i.indnkeyatts >= array_length(con.conkey, 1)" in UNINDEXED_FOREIGN_KEY_SQL


def test_only_index_types_that_answer_an_equality_lookup_count():
    assert "am.amname IN ('btree', 'hash')" in UNINDEXED_FOREIGN_KEY_SQL


# -- Type mismatches --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mismatched_key_reports_both_types_and_the_conversion():
    rows = [
        {
            "schema": "public",
            "table": "orders",
            "constraint_name": "orders_customer_id_fkey",
            "column": "customer_id",
            "column_type": "integer",
            "referenced_schema": "public",
            "referenced_table": "customers",
            "referenced_column": "id",
            "referenced_type": "bigint",
            "table_ident": "public.orders",
            "column_ident": "customer_id",
            "row_count": 10,
            "table_bytes": 8192,
        }
    ]

    findings = await foreign_key_type_mismatches(driver_returning(rows), ReviewOptions())

    assert findings[0].summary == "Foreign key orders_customer_id_fkey joins integer to bigint."
    assert findings[0].remediation == ["ALTER TABLE public.orders ALTER COLUMN customer_id TYPE bigint;"]


def test_a_collation_only_counts_when_both_sides_have_one():
    """Comparing a collation against 0 would report every key on a non-text column."""
    assert "ca.attcollation <> 0 AND pa.attcollation <> 0" in FOREIGN_KEY_TYPE_MISMATCH_SQL


def test_the_columns_are_paired_by_position_not_crossed():
    """conkey and confkey are parallel arrays; joining them without ordinality
    would compare every child column against every parent one."""
    assert "WITH ORDINALITY" in FOREIGN_KEY_TYPE_MISMATCH_SQL
    assert "f.ord = k.ord" in FOREIGN_KEY_TYPE_MISMATCH_SQL


# -- Missing primary keys ---------------------------------------------------


def pk_row(**overrides):
    cells = {
        "schema": "public",
        "table": "events",
        "table_ident": "public.events",
        "row_count": 1_000,
        "table_bytes": 8192,
        "relreplident": "d",
        "unique_index": None,
    }
    return {**cells, **overrides}


@pytest.mark.asyncio
async def test_a_table_with_no_way_to_address_a_row_is_the_urgent_case():
    findings = await missing_primary_keys(driver_returning([pk_row()]), ReviewOptions())

    assert findings[0].severity is Severity.HIGH
    assert "logical replication rejects" in findings[0].detail
    assert "GENERATED ALWAYS AS IDENTITY" in findings[0].remediation[0]


@pytest.mark.asyncio
async def test_an_existing_unique_index_becomes_the_primary_key():
    findings = await missing_primary_keys(driver_returning([pk_row(unique_index="events_ref_key")]), ReviewOptions())

    assert findings[0].severity is Severity.MEDIUM
    assert findings[0].remediation == ["ALTER TABLE public.events ADD PRIMARY KEY USING INDEX events_ref_key;"]


@pytest.mark.asyncio
async def test_replica_identity_full_answers_the_replication_half():
    findings = await missing_primary_keys(driver_returning([pk_row(relreplident="f")]), ReviewOptions())

    assert findings[0].severity is Severity.MEDIUM
    assert "REPLICA IDENTITY FULL" in findings[0].detail


def test_a_unique_index_only_substitutes_when_its_columns_are_not_null():
    """Uniqueness does not apply between NULLs, so a nullable unique index
    cannot identify a row."""
    assert "NOT a.attnotnull" in MISSING_PRIMARY_KEY_SQL


def test_partitions_are_not_reported_separately_from_their_parent():
    assert "NOT c.relispartition" in MISSING_PRIMARY_KEY_SQL


# -- Scoping ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_schema_is_passed_as_a_parameter_rather_than_interpolated():
    driver = driver_returning([])

    await missing_primary_keys(driver, ReviewOptions(schema="reporting"))

    query = driver.execute_query.call_args[0][0]
    assert "n.nspname = " in query
    assert "reporting" in query  # bound by execute_param_query, not concatenated blindly


def test_system_schemas_are_never_reviewed():
    for sql in (UNINDEXED_FOREIGN_KEY_SQL, FOREIGN_KEY_TYPE_MISMATCH_SQL, MISSING_PRIMARY_KEY_SQL):
        assert "information_schema" in sql
        assert "NOT LIKE 'pg\\_%'" in sql

"""Schema-qualified table references through the tuning pipeline."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from pglast import parse_sql

from pg_debug.db import SqlDriver
from pg_debug.db import split_table_reference
from pg_debug.db import table_identifier
from pg_debug.db.identifiers import MAX_IDENTIFIER_BYTES
from pg_debug.index.dta import HYPOTHETICAL_INDEX_SIZES_SQL
from pg_debug.index.dta import ConditionColumnCollector
from pg_debug.index.dta import DatabaseTuningAdvisor
from pg_debug.parsing import ColumnCollector
from pg_debug.parsing import TableAliasVisitor
from pg_debug.parsing import resolve_table_reference
from pg_debug.plans.hypothetical import IndexDefinition


def parse(sql):
    return parse_sql(sql)[0].stmt


# -- Reference splitting ----------------------------------------------------


def test_split_unqualified_reference():
    assert split_table_reference("orders") == (None, "orders")


def test_split_qualified_reference():
    assert split_table_reference("reporting.orders") == ("reporting", "orders")


def test_split_keeps_everything_before_the_last_dot_as_schema():
    assert split_table_reference("db.reporting.orders") == ("db.reporting", "orders")


# -- Reference resolution ---------------------------------------------------


def test_resolve_prefers_an_alias():
    assert resolve_table_reference("o", {"o": "reporting.orders"}, {"reporting.orders"}) == "reporting.orders"


def test_resolve_returns_an_exact_table_match():
    assert resolve_table_reference("orders", {}, {"orders"}) == "orders"


def test_resolve_maps_a_bare_name_onto_the_qualified_table():
    assert resolve_table_reference("orders", {}, {"reporting.orders"}) == "reporting.orders"


def test_resolve_leaves_ambiguous_names_alone():
    tables = {"reporting.orders", "archive.orders"}
    assert resolve_table_reference("orders", {}, tables) == "orders"


# -- Table extraction -------------------------------------------------------


def test_visitor_keeps_the_schema_qualifier():
    _, tables = TableAliasVisitor()(parse("SELECT * FROM reporting.orders WHERE id = 1"))
    assert tables == {"reporting.orders"}


def test_visitor_leaves_unqualified_tables_alone():
    _, tables = TableAliasVisitor()(parse("SELECT * FROM orders WHERE id = 1"))
    assert tables == {"orders"}


def test_visitor_maps_aliases_to_the_qualified_table():
    aliases, _ = TableAliasVisitor()(parse("SELECT * FROM reporting.orders o WHERE o.id = 1"))
    assert aliases == {"o": "reporting.orders"}


# -- Column collection ------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM reporting.orders WHERE customer_id = 1 AND status = 'paid'",
        "SELECT * FROM reporting.orders o WHERE o.customer_id = 1 AND o.status = 'paid'",
        "SELECT * FROM reporting.orders WHERE orders.customer_id = 1 AND orders.status = 'paid'",
    ],
)
def test_condition_columns_are_keyed_by_the_qualified_table(sql):
    assert ConditionColumnCollector()(parse(sql)) == {"reporting.orders": {"customer_id", "status"}}


def test_columns_are_keyed_by_the_qualified_table():
    columns = ColumnCollector()(parse("SELECT id FROM reporting.orders WHERE customer_id = 1"))
    assert set(columns) == {"reporting.orders"}


def test_joined_qualified_tables_stay_separate():
    sql = """
        SELECT *
        FROM reporting.orders o
        JOIN archive.customers c ON c.id = o.customer_id
        WHERE o.status = 'paid'
    """
    assert ConditionColumnCollector()(parse(sql)) == {
        "reporting.orders": {"customer_id", "status"},
        "archive.customers": {"id"},
    }


# -- Generated DDL ----------------------------------------------------------


def test_table_identifier_quotes_each_part():
    assert table_identifier("reporting.orders").as_string(None) == '"reporting"."orders"'
    assert table_identifier("orders").as_string(None) == '"orders"'


def test_table_identifier_escapes_quotes():
    assert table_identifier('we"ird').as_string(None) == '"we""ird"'


@pytest.mark.asyncio
async def test_table_size_lookup_resolves_a_qualified_table():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells={"rel_size": 4096})])
    advisor = DatabaseTuningAdvisor(driver)

    assert await advisor._get_table_size("reporting.orders") == 4096

    query = driver.execute_query.call_args[0][0]
    assert "to_regclass" in query
    # The name is passed as a literal holding a quoted identifier, not interpolated raw.
    assert '\'"reporting"."orders"\'' in query


@pytest.mark.asyncio
async def test_table_size_falls_back_to_a_row_count():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(
        side_effect=[
            [SqlDriver.RowResult(cells={"rel_size": None})],
            [SqlDriver.RowResult(cells={"row_count": 3})],
        ]
    )
    advisor = DatabaseTuningAdvisor(driver)

    assert await advisor._get_table_size("reporting.orders") == 3 * 1024

    fallback_query = driver.execute_query.call_args[0][0]
    assert 'FROM "reporting"."orders"' in fallback_query


def test_index_definition_for_a_qualified_table():
    index = IndexDefinition(table="reporting.orders", columns=("customer_id",))

    assert index.name == "pg_debug_idx_reporting_orders_customer_id_1"
    assert index.definition == "CREATE INDEX pg_debug_idx_reporting_orders_customer_id_1 ON reporting.orders USING btree (customer_id)"


def test_index_sizes_come_from_the_hypopg_function_not_its_view():
    """HypoPG grants no privileges on its views.

    A view is private to its owner by default, so reading
    hypopg_list_indexes fails with "permission denied for view" for any role
    that did not create the extension. The function behind it defaults to
    EXECUTE for PUBLIC and exposes the same two columns.
    """
    assert "FROM hypopg()" in HYPOTHETICAL_INDEX_SIZES_SQL
    assert "hypopg_list_indexes" not in HYPOTHETICAL_INDEX_SIZES_SQL
    assert "hypopg_relation_size(indexrelid)" in HYPOTHETICAL_INDEX_SIZES_SQL


def test_hypopg_function_is_allowed_in_restricted_mode():
    from pg_debug.db.restricted import SafeSqlDriver

    assert "hypopg" in SafeSqlDriver.ALLOWED_FUNCTIONS


# -- Expression index names -------------------------------------------------


@pytest.mark.parametrize(
    "columns,expected",
    [
        (("lower(email)",), "pg_debug_idx_users_lower_email_1"),
        (("date_trunc('day', created_at)",), "pg_debug_idx_users_date_trunc_day_created_at_1"),
        (("(first_name || ' ' || last_name)",), "pg_debug_idx_users_first_name_last_name_1"),
        (("status", "lower(email)"), "pg_debug_idx_users_status_lower_email_2"),
    ],
)
def test_expression_indexes_get_a_valid_identifier(columns, expected):
    """An index name has to be a bare identifier, whatever the expression was."""
    name = IndexDefinition(table="users", columns=columns).name

    assert name == expected
    assert name.isidentifier(), f"{name!r} is not a valid unquoted identifier"


def test_expression_index_definition_keeps_the_expression_intact():
    index = IndexDefinition(table="users", columns=("date_trunc('day', created_at)",))

    assert index.definition.endswith("ON users USING btree (date_trunc('day', created_at))")


def test_long_names_stay_within_the_identifier_limit():
    columns = ("lower(coalesce(some_very_long_column_name, another_very_long_column_name))",)

    name = IndexDefinition(table="a_table_with_a_long_name", columns=columns).name

    assert len(name.encode()) <= MAX_IDENTIFIER_BYTES
    assert name.isidentifier()


def test_long_names_that_share_a_prefix_stay_distinct():
    """PostgreSQL truncates past NAMEDATALEN, which would collide silently."""
    prefix = "lower(coalesce(a_very_long_column_name_that_keeps_going_and_going"

    first = IndexDefinition(table="t", columns=(f"{prefix}, first_alternative))",)).name
    second = IndexDefinition(table="t", columns=(f"{prefix}, second_alternative))",)).name

    assert first != second
    assert len(first.encode()) <= MAX_IDENTIFIER_BYTES
    assert len(second.encode()) <= MAX_IDENTIFIER_BYTES

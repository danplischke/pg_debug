"""Reading a query's restrictions: which column, bare or wrapped, which operator, under what."""

import pytest

from pg_debug.errors import InvalidArgumentError
from pg_debug.index_use.predicates import normalise_expression
from pg_debug.index_use.predicates import read_predicates


def by_column(sql, columns_by_table=None):
    return {p.column: p for p in read_predicates(sql, columns_by_table).predicates}


def test_a_bare_column_compared_with_a_constant():
    (p,) = read_predicates("SELECT * FROM orders WHERE status = 'open'").predicates

    assert (p.table, p.column, p.bare, p.operator) == ("orders", "status", True, "=")
    assert p.expression == "status"
    assert p.or_group is None and not p.negated


def test_the_constant_may_be_on_the_left_and_the_operator_is_mirrored():
    (p,) = read_predicates("SELECT * FROM orders WHERE 100 < total").predicates

    assert (p.column, p.operator) == ("total", ">")


def test_a_wrapped_column_keeps_the_expression():
    predicates = by_column("SELECT * FROM users u WHERE lower(u.email) = 'a' AND u.created_at::date = '2026-01-01' AND u.n + 1 = 2")

    assert predicates["email"].bare is False
    assert normalise_expression(predicates["email"].expression) == "lower(email)".replace("(", "").replace(")", "")
    assert normalise_expression(predicates["created_at"].expression) == normalise_expression("(created_at)::date")
    assert predicates["n"].bare is False


def test_operators_are_named_as_postgres_names_them():
    predicates = by_column(
        "SELECT * FROM t WHERE a <> 1 AND b LIKE 'x%' AND c ILIKE 'y' AND d IN (1, 2) AND e NOT IN (3) "
        "AND f BETWEEN 1 AND 2 AND g IS NULL AND h IS NOT NULL AND i = ANY(ARRAY[1]) AND j ~ 'r'"
    )

    assert predicates["a"].operator == "<>"
    assert predicates["b"].operator == "~~" and predicates["b"].pattern == "x%"
    assert predicates["c"].operator == "~~*" and predicates["c"].pattern == "y"
    assert predicates["d"].operator == "IN"
    assert predicates["e"].operator == "NOT IN"
    assert predicates["f"].operator == "BETWEEN"
    assert predicates["g"].operator == "IS NULL"
    assert predicates["h"].operator == "IS NOT NULL"
    assert predicates["i"].operator == "= ANY"
    assert predicates["j"].operator == "~"


def test_not_marks_the_predicate_negated():
    predicates = by_column("SELECT * FROM t WHERE NOT (a = 1) AND b = 2")

    assert predicates["a"].negated is True
    assert predicates["b"].negated is False


def test_predicates_under_one_or_share_a_group():
    result = read_predicates("SELECT * FROM o JOIN c ON c.id = o.c_id WHERE (o.a = 1 OR c.b = 2) AND o.d = 3")
    predicates = {p.column: p for p in result.predicates}

    group = predicates["a"].or_group
    assert group is not None and predicates["b"].or_group == group
    assert predicates["d"].or_group is None
    assert result.or_group_tables(group) == {"o", "c"}


def test_join_conditions_restrict_both_sides_and_name_the_other_table():
    result = read_predicates("SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id")
    predicates = {p.column: p for p in result.predicates}

    assert predicates["customer_id"].table == "orders"
    assert predicates["customer_id"].join_table == "customers"
    assert predicates["id"].table == "customers"
    assert predicates["id"].join_table == "orders"


def test_a_bare_column_in_a_join_is_attributed_through_the_catalog():
    sql = "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id WHERE status = 'open'"

    assert by_column(sql)["status"].table is None, "two tables, no catalog: cannot tell"
    assert by_column(sql, {"orders": {"status"}, "customers": {"email"}})["status"].table == "orders"


def test_schema_qualified_tables_are_kept_qualified():
    (p,) = read_predicates("SELECT * FROM shop.orders WHERE orders.status = 'open'").predicates

    assert p.table == "shop.orders"


def test_update_and_delete_are_read_too():
    assert by_column("UPDATE orders SET x = 1 WHERE id = 5")["id"].operator == "="
    assert by_column("DELETE FROM orders WHERE created_at < now()")["created_at"].operator == "<"


def test_a_subquery_s_predicates_are_not_the_outer_query_s():
    predicates = read_predicates("SELECT * FROM orders WHERE customer_id IN (SELECT id FROM customers WHERE vip)").predicates

    assert [p.column for p in predicates] == ["customer_id"]


def test_two_statements_are_refused():
    with pytest.raises(InvalidArgumentError, match="one statement"):
        read_predicates("SELECT 1; SELECT 2")


def test_unparseable_sql_is_refused():
    with pytest.raises(InvalidArgumentError, match="parse"):
        read_predicates("SELECT FROM WHERE")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("lower(u.email)", "lower(email)"),
        ("CAST(o.created_at AS date)", "(created_at)::date"),
        ("date_trunc('day', created_at)", "date_trunc('day'::text, created_at)"),
        ("(status)", "status"),
        ("COALESCE(a, 0)", "coalesce(a, 0)"),
    ],
)
def test_the_parser_s_rendering_and_the_catalog_s_normalise_to_the_same_key(left, right):
    assert normalise_expression(left) == normalise_expression(right)


def test_different_expressions_stay_different():
    assert normalise_expression("lower(email)") != normalise_expression("email")
    assert normalise_expression("(created_at)::date") != normalise_expression("created_at")

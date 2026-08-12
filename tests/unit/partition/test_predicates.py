"""What a statement restricts on, which is what decides whether it prunes.

The distinction these pin down is between a column a query *mentions* and a
column it *restricts on*. Only the second one prunes, and getting it wrong in
the permissive direction is how a tool ends up recommending a partition key
that makes every query read every partition.
"""

from pglast import parse_sql

from pg_debug.partition.predicates import extract_predicates


def predicates(sql):
    return extract_predicates(parse_sql(sql)[0].stmt)


def test_a_column_that_is_only_selected_does_not_prune():
    """``SELECT created_at FROM events`` reads every partition. This is the
    whole reason the advisor reads predicates rather than column references."""
    assert predicates("SELECT created_at FROM events").by_table == {}


def test_a_range_predicate_is_recorded_as_a_range():
    """The kind matters: a HASH partition prunes on equality and not on this."""
    assert predicates("SELECT * FROM events WHERE created_at > now()").kinds("events", "created_at") == {"range"}


def test_an_equality_predicate_is_recorded_as_equality():
    assert predicates("SELECT * FROM events WHERE tenant_id = 1").kinds("events", "tenant_id") == {"equality"}


def test_both_kinds_are_kept_when_a_column_is_used_both_ways():
    found = predicates("SELECT * FROM events WHERE tenant_id = 1 UNION ALL SELECT * FROM events WHERE tenant_id > 5")
    assert found.kinds("events", "tenant_id") == {"equality", "range"}


def test_a_delete_is_read_like_any_other_statement():
    """Deletes are the ones most worth seeing — a table whose old rows are
    deleted by date is the strongest case for partitioning there is."""
    assert predicates("DELETE FROM events WHERE created_at < now()").columns("events") == {"created_at"}


def test_an_update_is_read_too():
    assert predicates("UPDATE events SET seen = true WHERE id = 1").columns("events") == {"id"}


def test_an_alias_resolves_to_the_table_it_stands_for():
    found = predicates("SELECT * FROM events e WHERE e.created_at > now()")

    assert found.columns("events") == {"created_at"}
    assert found.columns("e") == set()


def test_a_join_condition_prunes_as_much_as_a_where_clause():
    found = predicates("SELECT * FROM events e JOIN tenants t ON t.id = e.tenant_id")

    assert found.columns("events") == {"tenant_id"}
    assert found.columns("tenants") == {"id"}


def test_an_unqualified_column_in_a_join_is_skipped_rather_than_guessed_at():
    """Attributing it to the wrong table would invent pruning that does not
    happen. Silence is the conservative error here."""
    found = predicates("SELECT * FROM events e JOIN tenants t ON t.id = e.tenant_id WHERE created_at > now()")

    assert "created_at" not in found.columns("events")


def test_is_null_does_not_count_as_a_predicate():
    """It restricts, but never to a partition anybody wants."""
    assert predicates("SELECT * FROM events WHERE created_at IS NULL").by_table == {}


def test_an_operator_that_cannot_prune_is_ignored():
    assert predicates("SELECT * FROM events WHERE kind <> 'click'").by_table == {}


def test_a_subquery_predicate_is_found():
    found = predicates("SELECT * FROM (SELECT * FROM events WHERE created_at > now()) x")

    assert found.columns("events") == {"created_at"}


def test_a_cte_predicate_is_found():
    found = predicates("WITH recent AS (SELECT * FROM events WHERE created_at > now()) SELECT * FROM recent")

    assert found.columns("events") == {"created_at"}


def test_a_comparison_in_the_select_list_is_not_a_predicate():
    """It restricts nothing the planner can skip a partition with. Counting it
    would report time as pruning that does not prune."""
    assert predicates("SELECT created_at > now() AS is_recent FROM events").by_table == {}

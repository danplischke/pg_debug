import json
from datetime import datetime
from decimal import Decimal

from pg_debug.cli.render import note_withheld_backends
from pg_debug.cli.render import note_withheld_lock_tree
from pg_debug.cli.render import note_withheld_queries
from pg_debug.cli.render import note_withheld_sample
from pg_debug.cli.render import note_withheld_vacuums
from pg_debug.cli.render import render_activity
from pg_debug.cli.render import render_index_recommendations
from pg_debug.cli.render import render_lock_tree
from pg_debug.cli.render import render_mapping
from pg_debug.cli.render import render_object_details
from pg_debug.cli.render import render_rows
from pg_debug.cli.render import render_vacuum_progress
from pg_debug.cli.render import to_json
from pg_debug.db.privileges import HIDDEN_QUERY_TEXT
from pg_debug.db.privileges import StatisticsAccess


def test_to_json_handles_database_types():
    value = {"total": Decimal("1.5"), "at": datetime(2026, 1, 1)}
    assert json.loads(to_json(value)) == {"total": "1.5", "at": "2026-01-01 00:00:00"}


def test_a_numeric_ratio_is_rounded_rather_than_printed_raw():
    """pg_stat_statements returns these fractions as numeric, and Python prints
    a Decimal at full precision — 0.09286047596826878208 in a column of a
    table nobody can read."""
    rendered = render_rows([{"frac": Decimal("0.09286047596826878208")}])

    assert "0.093" in rendered
    assert "0.09286047596826878208" not in rendered


def test_a_numeric_zero_is_not_printed_in_scientific_notation():
    """A numeric division that came out zero carries its scale, so str() gives
    0E-20 — which reads as anything but zero. It is whole, so it prints whole."""
    rendered = render_rows([{"frac": Decimal("0E-20")}])

    assert "0E-20" not in rendered
    assert rendered.splitlines()[2].strip() == "0"


def test_a_whole_numeric_keeps_its_whole_number():
    """wal_bytes is a numeric because it can exceed a bigint, not because it
    has a fractional part. Printing it as 4198.000 would be worse than the raw
    value."""
    assert "4198" in render_rows([{"wal_bytes": Decimal("4198")}])
    assert "4198.000" not in render_rows([{"wal_bytes": Decimal("4198")}])


def test_a_boolean_stays_a_boolean():
    """A bool is an int in Python, so a numeric branch that catches it first
    turns True into 1."""
    rendered = render_rows([{"autovacuum": True, "healthy": False}])

    assert "True" in rendered
    assert "False" in rendered


def test_render_rows_aligns_columns():
    rows = [{"name": "orders", "kind": "table"}, {"name": "a", "kind": "view"}]

    lines = render_rows(rows).splitlines()

    assert lines[0] == "name    kind"
    assert lines[2] == "orders  table"
    assert lines[3] == "a       view"
    assert lines[-1] == "(2 rows)"


def test_render_rows_includes_columns_missing_from_some_rows():
    lines = render_rows([{"a": 1}, {"b": 2}]).splitlines()

    assert lines[0] == "a  b"
    assert lines[2] == "1"
    assert lines[3].startswith("   2")


def test_render_rows_collapses_whitespace_and_truncates():
    rows = [{"query": "SELECT\n  *\n  FROM orders WHERE id = 1"}]

    rendered = render_rows(rows, max_width=20)

    assert "\n  " not in rendered.split("\n")[2]
    assert "…" in rendered


def test_render_rows_when_empty():
    assert render_rows([]) == "(no rows)"
    assert render_rows([], empty="nothing here") == "nothing here"


def test_render_rows_singular_row_count():
    assert render_rows([{"a": 1}]).endswith("(1 row)")


def test_render_mapping():
    assert render_mapping({"Vacuum health": "ok"}) == "Vacuum health: ok"
    assert render_mapping({}) == "(nothing to report)"


def test_render_object_details():
    details = {
        "basic": {"schema": "public", "name": "orders", "type": "table"},
        "columns": [{"column": "id", "data_type": "integer", "is_nullable": "NO", "default": None}],
        "constraints": [{"name": "orders_pkey", "type": "PRIMARY KEY", "columns": ["id", "tenant_id"]}],
        "indexes": [{"name": "orders_pkey", "definition": "CREATE UNIQUE INDEX orders_pkey ON public.orders USING btree (id)"}],
    }

    rendered = render_object_details(details)

    assert rendered.startswith("Table public.orders")
    assert "id, tenant_id" in rendered
    # Index definitions are not truncated.
    assert "CREATE UNIQUE INDEX orders_pkey ON public.orders USING btree (id)" in rendered


def test_render_object_details_for_flat_objects():
    assert render_object_details({"name": "hypopg", "version": "1.4.1"}) == "name: hypopg\nversion: 1.4.1"


def test_render_object_details_when_missing():
    assert render_object_details({}) == "(object not found)"


def test_render_index_recommendations_when_none_found():
    assert render_index_recommendations({"recommendations": "No index recommendations found."}) == "No index recommendations found."


def test_render_index_recommendations_includes_warnings():
    result = {
        "summary": {
            "total_recommendations": 1,
            "base_cost": "100.0",
            "new_cost": "10.0",
            "total_size_bytes": "8.0 MB",
            "improvement_multiple": "10.0",
        },
        "recommendations": [
            {
                "index_apply_order": 1,
                "index_definition": "CREATE INDEX ON public.orders USING btree (user_id)",
                "index_estimated_size": "8.0 MB",
                "benefit_of_this_index_only": {"improvement_multiple": "10.0"},
                "benefit_after_previous_indexes": {"improvement_multiple": "4.0"},
                "warning": "includes a long text column",
            }
        ],
        "query_impact": [
            {
                "query": "SELECT * FROM orders WHERE user_id = 1",
                "base_cost": "100.0",
                "new_cost": "10.0",
                "improvement_multiple": "10.0",
            }
        ],
    }

    rendered = render_index_recommendations(result)

    assert "1 recommended index(es)" in rendered
    assert "warning: includes a long text column" in rendered
    assert "SELECT * FROM orders WHERE user_id = 1" in rendered


def test_the_lock_tree_renders_the_root_first_and_indents_the_waiters():
    tree = [
        {
            "pid": 41,
            "usename": "app",
            "datname": "shop",
            "application_name": "worker",
            "state": "idle in transaction",
            "state_seconds": 125.0,
            "transaction_seconds": 130.0,
            "query": "UPDATE   orders\n   SET status = 'paid'",
            "waits_for": None,
            "blocked_total": 1,
            "blocking": [
                {
                    "pid": 42,
                    "usename": "app",
                    "datname": "shop",
                    "application_name": "",
                    "state": "active",
                    "state_seconds": 90.0,
                    "transaction_seconds": 90.0,
                    "query": "SELECT count(*) FROM orders",
                    "waits_for": {"description": "AccessShareLock on public.orders (41 holds AccessExclusiveLock)"},
                    "blocked_total": 0,
                    "blocking": [],
                }
            ],
        }
    ]

    text = render_lock_tree(tree)

    lines = text.splitlines()
    assert lines[0] == "pid 41: idle in transaction for 2m05s in a transaction open 2m10s (app@shop worker) blocking 1"
    assert lines[1] == "    UPDATE orders SET status = 'paid'"
    assert lines[2] == "  pid 42: waits for AccessShareLock on public.orders (41 holds AccessExclusiveLock); active for 1m30s (app@shop)"
    assert lines[3] == "      SELECT count(*) FROM orders"


def test_an_empty_lock_tree_says_so():
    assert render_lock_tree([]) == "(nothing is blocked)"


# -- What PostgreSQL withheld from the role ---------------------------------

PLAIN = StatisticsAccess(role="app_user", reads_all_stats=False, remedy="GRANT pg_monitor TO app_user")
NOINHERIT = StatisticsAccess(role="app_user", reads_all_stats=False, member=True, remedy="GRANT pg_monitor TO app_user WITH INHERIT TRUE")
LOCKS = ["AccessExclusiveLock on public.orders", "ExclusiveLock on advisory lock 42"]


def withheld(pid, **fields):
    """A backend as PostgreSQL shows it to a role without pg_read_all_stats."""
    return {
        "pid": pid,
        "datname": "shop",
        "usename": "postgres",
        "application_name": "worker",
        "state": None,
        "backend_type": None,
        "blocked_by": [],
        "in_transaction": True,
        "query": HIDDEN_QUERY_TEXT,
        **fields,
    }


def test_a_withheld_backend_reads_as_in_a_transaction_with_what_is_still_shown():
    text = render_activity([withheld(41, locks=LOCKS, blocking=[42, 43])])

    (row,) = [line for line in text.splitlines() if line.startswith("41")]
    assert "in a transaction" in row
    # Not cut at the table's cell width: the locks are why it is listed.
    assert row.endswith(
        "state, query and wait withheld; holds AccessExclusiveLock on public.orders, ExclusiveLock on advisory lock 42; "
        "blocking 42, 43; database shop, application worker"
    )
    assert HIDDEN_QUERY_TEXT not in text


def test_a_withheld_backend_outside_a_transaction_says_so():
    text = render_activity([withheld(41, in_transaction=False, application_name="")])

    assert "not in a transaction" in text
    assert text.splitlines()[2].endswith("state, query and wait withheld; database shop")


def test_visible_backends_render_as_before():
    row = {"pid": 7, "usename": "app", "state": "active", "state_seconds": 1.5, "blocked_by": [], "query": "SELECT " + "x, " * 60 + "1"}

    text = render_activity([row])

    assert "active" in text
    assert "…" in text, "a long query is still cut to the cell width"


def test_a_withheld_blocker_in_the_lock_tree_reads_as_such():
    tree = [
        {
            **withheld(41),
            "blocked_total": 1,
            "blocking": [
                {
                    **withheld(42, usename="app", application_name=""),
                    "waits_for": {"description": "AccessShareLock on public.orders (41 holds AccessExclusiveLock)"},
                    "blocked_total": 0,
                    "blocking": [],
                }
            ],
        }
    ]

    lines = render_lock_tree(tree).splitlines()

    assert lines == [
        "pid 41: in a transaction; state, query and wait withheld (postgres@shop worker) blocking 1",
        "  pid 42: waits for AccessShareLock on public.orders (41 holds AccessExclusiveLock); "
        "in a transaction; state, query and wait withheld (app@shop)",
    ]


def test_a_withheld_vacuum_shows_blank_progress_rather_than_none():
    text = render_vacuum_progress([{"pid": 7, "phase": None, "heap_blks_total": None, "heap_blks_scanned": None, "query": HIDDEN_QUERY_TEXT}])

    assert "None" not in text


def test_activity_says_which_backends_it_lists_withheld_and_which_it_leaves_out():
    rows = [withheld(41), {"pid": 50, "usename": "app_user", "state": "active", "query": "SELECT 1"}]
    backends = [
        withheld(41, locks=LOCKS, blocking=[]),
        withheld(60, in_transaction=False, usename="svc"),
        withheld(61, in_transaction=False, usename=None),
        withheld(62, in_transaction=False, blocking=[63]),
    ]

    note = note_withheld_backends(PLAIN, rows, backends)

    assert note == (
        "1 of these backends, of postgres, is in a transaction, and PostgreSQL shows app_user its locks but not its state, "
        "query, wait or timings. 3 other backends, of postgres and svc, and of the server itself, are not listed: they "
        "show no sign of a transaction, and whether they are running cannot be told; --all lists them. Even so, 62 blocks "
        "63. That is because app_user lacks pg_read_all_stats, which pg_monitor includes; GRANT pg_monitor TO app_user "
        "grants it."
    )


def test_cannot_be_told_is_kept_for_the_backends_with_no_sign_of_a_transaction():
    rows = [withheld(41), withheld(42, in_transaction=False), withheld(43, in_transaction=False)]

    note = note_withheld_backends(PLAIN, rows, rows, include_idle=True)

    assert note is not None
    assert note.startswith(
        "3 of these backends, of postgres, are shown without state, query, wait or timings. 1 is in a transaction; for the "
        "other 2, which show no sign of one, whether they are running cannot be told."
    )


def test_a_minimum_duration_names_the_withheld_backends_in_a_transaction():
    backends = [withheld(41), withheld(42, in_transaction=False, usename=None)]

    note = note_withheld_backends(PLAIN, [], backends, min_duration=30)

    assert note is not None
    assert note.startswith(
        "2 other backends, of postgres and of the server itself, are not listed: how long they have been in their state "
        "is withheld. Pid 41 is in a transaction."
    )


def test_a_withheld_blocker_of_the_blocked_backends_is_described_rather_than_left_a_pid():
    rows = [{"pid": 42, "usename": "app_user", "state": "active", "blocked_by": [41], "query": "SELECT 1"}]
    backends = [withheld(41, locks=LOCKS[:1], blocking=[42]), withheld(60, in_transaction=False)]

    note = note_withheld_backends(PLAIN, rows, backends, blocked_only=True)

    assert note is not None
    assert note.startswith(
        "Pid 41 (postgres@shop worker), which blocks 42, is in a transaction and holds AccessExclusiveLock on "
        "public.orders; its state, query and wait are withheld."
    )
    assert "60" not in note


def test_a_member_that_does_not_inherit_is_told_so_and_how_to_inherit():
    note = note_withheld_backends(NOINHERIT, [], [withheld(41, in_transaction=False)])

    assert note is not None
    assert note.endswith(
        "That is because app_user is a member of pg_read_all_stats but does not inherit its privileges, which "
        "PostgreSQL 15 and later ask for; GRANT pg_monitor TO app_user WITH INHERIT TRUE makes it inherit them."
    )
    assert "lacks" not in note


def test_nothing_withheld_is_no_note():
    assert note_withheld_backends(PLAIN, [{"pid": 1, "query": "SELECT 1"}], []) is None
    assert note_withheld_lock_tree(PLAIN, [{"pid": 1, "query": "SELECT 1", "blocking": []}]) is None
    assert note_withheld_vacuums(PLAIN, []) is None
    assert note_withheld_queries(PLAIN, [{"query": "SELECT 1"}]) is None
    assert note_withheld_sample(PLAIN, 0, "total_time") is None


def test_a_lock_tree_says_it_is_whole():
    tree = [{**withheld(41), "blocking": [{**withheld(42, usename="svc"), "blocking": []}]}]

    note = note_withheld_lock_tree(PLAIN, tree)

    assert note is not None
    assert note.startswith(
        "2 of the backends in these trees, of postgres and svc, are shown without state, query, wait or timings; who "
        "blocks whom is complete, since pg_locks shows every role's locks."
    )


def test_vacuums_and_queries_say_how_many_were_withheld():
    assert (note_withheld_vacuums(PLAIN, [{"query": HIDDEN_QUERY_TEXT}]) or "").startswith(
        "1 of these vacuums is shown without table, phase or progress, and whether it is autovacuum cannot be told."
    )
    assert (note_withheld_queries(PLAIN, [{"query": HIDDEN_QUERY_TEXT}] * 2) or "").startswith(
        "2 of these queries are other roles': pg_stat_statements shows app_user their counters but not their text."
    )
    assert (note_withheld_sample(PLAIN, 3, "resources") or "").startswith(
        "3 statements of other roles ran during the interval and are not ranked: pg_stat_statements shows app_user "
        "neither their text nor their queryid, so their counters cannot be followed from one snapshot to the next. "
        "The resource shares are of the ranked statements alone."
    )

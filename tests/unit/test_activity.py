from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.activity import LOCK_TREE_BACKENDS_SQL
from pg_debug.activity import LOCK_TREE_HOLDERS_SQL
from pg_debug.activity import WITHHELD_BACKENDS_SQL
from pg_debug.activity import ActivityInspector
from pg_debug.activity import assemble_lock_tree
from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.db.privileges import HIDDEN_QUERY_TEXT
from pg_debug.db.privileges import StatisticsAccess

#: A role shown everything, as a superuser or a member of pg_monitor is.
MONITOR = StatisticsAccess(role="monitor", reads_all_stats=True)
#: A role without pg_read_all_stats.
PLAIN = StatisticsAccess(role="app_user", reads_all_stats=False)


def make_driver(rows=()):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=row) for row in rows])
    return driver


def executed_query(driver):
    return driver.execute_query.call_args[0][0]


@pytest.fixture(autouse=True)
def access():
    """The role reads every role's statistics, unless a test says otherwise."""
    with patch("pg_debug.activity.statistics_access", AsyncMock(return_value=MONITOR)) as asked:
        yield asked


@pytest.fixture
def pg16():
    """Report PostgreSQL 16, the common case for both queries."""
    with patch("pg_debug.activity.get_postgres_version", AsyncMock(return_value=16)):
        yield


# -- Backends ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_backends_exclude_our_own_connection(pg16):
    driver = make_driver()

    await ActivityInspector(driver).list_backends()

    assert "a.pid <> pg_backend_pid()" in executed_query(driver)


@pytest.mark.asyncio
async def test_backends_hide_idle_by_default(pg16):
    driver = make_driver()

    await ActivityInspector(driver).list_backends()

    query = executed_query(driver)
    assert "a.state IS DISTINCT FROM 'idle'" in query
    # Idle auxiliary processes report an 'Activity' wait.
    assert "wait_event_type = 'Activity'" in query


@pytest.mark.asyncio
async def test_backends_can_include_idle(pg16):
    driver = make_driver()

    await ActivityInspector(driver).list_backends(include_idle=True)

    assert "IS DISTINCT FROM 'idle'" not in executed_query(driver)


@pytest.mark.asyncio
async def test_backends_filter_by_duration(pg16):
    driver = make_driver()

    await ActivityInspector(driver).list_backends(min_duration_seconds=30)

    query = executed_query(driver)
    assert "clock_timestamp() - a.state_change >=" in query
    assert "make_interval(secs => 30" in query


@pytest.mark.asyncio
async def test_backends_return_rows(pg16):
    driver = make_driver([{"pid": 42, "blocked_by": [41], "state": "active"}])

    assert await ActivityInspector(driver).list_backends() == [{"pid": 42, "blocked_by": [41], "state": "active"}]


@pytest.mark.asyncio
async def test_query_id_is_selected_on_pg14_and_later():
    driver = make_driver()
    with patch("pg_debug.activity.get_postgres_version", AsyncMock(return_value=14)):
        await ActivityInspector(driver).list_backends()

    assert "a.query_id" in executed_query(driver)


@pytest.mark.asyncio
async def test_query_id_is_null_before_pg14():
    driver = make_driver()
    with patch("pg_debug.activity.get_postgres_version", AsyncMock(return_value=13)):
        await ActivityInspector(driver).list_backends()

    assert "NULL::bigint AS query_id" in executed_query(driver)


@pytest.mark.asyncio
async def test_blocked_backends_keep_only_waiters(pg16):
    driver = make_driver(
        [
            {"pid": 1, "blocked_by": []},
            {"pid": 2, "blocked_by": [1]},
            {"pid": 3, "blocked_by": None},
        ]
    )

    assert [row["pid"] for row in await ActivityInspector(driver).blocked_backends()] == [2]


@pytest.mark.asyncio
async def test_blocked_backends_look_at_idle_ones_too(pg16):
    """A backend can be blocked while idle in transaction."""
    driver = make_driver()

    await ActivityInspector(driver).blocked_backends()

    assert "IS DISTINCT FROM 'idle'" not in executed_query(driver)


# -- Vacuum progress --------------------------------------------------------


@pytest.mark.asyncio
async def test_vacuum_progress_uses_legacy_counters_before_pg17():
    driver = make_driver()
    with patch("pg_debug.activity.get_postgres_version", AsyncMock(return_value=16)):
        await ActivityInspector(driver).vacuum_progress()

    query = executed_query(driver)
    assert "v.num_dead_tuples AS dead_item_ids" in query
    assert "v.max_dead_tuples AS max_dead_item_ids" in query
    assert "NULL::bigint AS dead_tuple_bytes" in query
    assert "num_dead_item_ids" not in query.replace("AS dead_item_ids", "")


@pytest.mark.asyncio
async def test_vacuum_progress_uses_byte_counters_on_pg17():
    driver = make_driver()
    with patch("pg_debug.activity.get_postgres_version", AsyncMock(return_value=17)):
        await ActivityInspector(driver).vacuum_progress()

    query = executed_query(driver)
    assert "v.num_dead_item_ids AS dead_item_ids" in query
    assert "v.dead_tuple_bytes" in query
    assert "v.indexes_processed" in query
    assert "v.max_dead_tuples" not in query


@pytest.mark.asyncio
async def test_vacuum_progress_reports_percentage(pg16):
    driver = make_driver([{"pid": 7, "percent_scanned": 79.0, "phase": "scanning heap"}])

    rows = await ActivityInspector(driver).vacuum_progress()

    assert rows[0]["percent_scanned"] == 79.0
    assert "percent_scanned" in executed_query(driver)


@pytest.mark.asyncio
async def test_vacuum_progress_when_nothing_is_running(pg16):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=None)

    assert await ActivityInspector(driver).vacuum_progress() == []


# -- Lock tree --------------------------------------------------------------


def backend(pid, blocked_by=(), **fields):
    row = {
        "pid": pid,
        "datname": "app",
        "usename": "svc",
        "application_name": "",
        "backend_type": "client backend",
        "state": "active" if blocked_by else "idle in transaction",
        "wait_event_type": "Lock" if blocked_by else "Client",
        "wait_event": "relation" if blocked_by else "ClientRead",
        "blocked_by": list(blocked_by),
        "state_seconds": 10.0,
        "query_seconds": 10.0,
        "transaction_seconds": 60.0,
        "query": f"query of {pid}",
        "waiting_locktype": "relation" if blocked_by else None,
        "waiting_mode": "AccessShareLock" if blocked_by else None,
        "waiting_relation_oid": 16400 if blocked_by else None,
        "waiting_relation": "public.orders" if blocked_by else None,
        "waiting_transactionid": None,
        "tuple_relation": None,
    }
    row.update(fields)
    return row


def holder(waiter, holder, mode):
    return {"waiter": waiter, "holder": holder, "held_mode": mode}


def tree_driver(backends, holders=()):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(
        side_effect=[
            [SqlDriver.RowResult(cells=row) for row in backends],
            [SqlDriver.RowResult(cells=row) for row in holders],
        ]
    )
    return driver


def test_lock_tree_queries_are_allowed_in_restricted_mode():
    """Both statements only read, and restricted mode has to agree."""
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(LOCK_TREE_BACKENDS_SQL)
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(LOCK_TREE_HOLDERS_SQL)


@pytest.mark.asyncio
async def test_lock_tree_is_empty_when_nothing_is_blocked():
    driver = tree_driver([])

    assert await ActivityInspector(driver).lock_tree() == []
    # No pile-up, no second query.
    assert driver.execute_query.await_count == 1


@pytest.mark.asyncio
async def test_lock_tree_nests_a_chain_under_its_root():
    """41 holds the table; 42 waits on 41; 43 waits on 42."""
    driver = tree_driver(
        [backend(41), backend(42, blocked_by=[41]), backend(43, blocked_by=[42])],
        [holder(42, 41, "AccessExclusiveLock"), holder(43, 42, "AccessShareLock")],
    )

    roots = await ActivityInspector(driver).lock_tree()

    assert [root["pid"] for root in roots] == [41]
    root = roots[0]
    assert root["waits_for"] is None
    assert root["blocked_total"] == 2
    assert root["state"] == "idle in transaction"

    (child,) = root["blocking"]
    assert child["pid"] == 42
    assert child["waits_for"]["held_modes"] == ["AccessExclusiveLock"]
    assert child["waits_for"]["description"] == "AccessShareLock on public.orders (41 holds AccessExclusiveLock)"
    assert child["blocked_total"] == 1

    (grandchild,) = child["blocking"]
    assert grandchild["pid"] == 43
    assert grandchild["blocking"] == []


def test_a_row_lock_wait_names_the_table_the_row_is_in():
    """A row lock waits on the other transaction's id; the waiter's own tuple
    lock is what says which table."""
    rows = [
        backend(7),
        backend(
            8,
            blocked_by=[7],
            waiting_locktype="transactionid",
            waiting_mode="ShareLock",
            waiting_relation_oid=None,
            waiting_relation=None,
            waiting_transactionid="12345",
            tuple_relation="public.orders",
        ),
    ]

    (root,) = assemble_lock_tree(rows, [holder(8, 7, "ExclusiveLock")])

    waits_for = root["blocking"][0]["waits_for"]
    assert waits_for["locktype"] == "transactionid"
    assert waits_for["transactionid"] == "12345"
    assert waits_for["tuple_relation"] == "public.orders"
    assert waits_for["description"] == "transaction 12345 to end, to lock a row in public.orders (7 holds ExclusiveLock)"


def test_a_relation_in_another_database_is_named_by_oid():
    rows = [backend(1), backend(2, blocked_by=[1], waiting_relation=None, waiting_relation_oid=99999)]

    (root,) = assemble_lock_tree(rows, [])

    assert root["blocking"][0]["waits_for"]["relation"] == "99999"
    assert root["blocking"][0]["waits_for"]["description"] == "AccessShareLock on 99999"


def test_a_lock_released_between_the_two_reads_is_said_so():
    rows = [backend(1), backend(2, blocked_by=[1], waiting_locktype=None, waiting_mode=None, waiting_relation=None, waiting_relation_oid=None)]

    (root,) = assemble_lock_tree(rows, [])

    assert root["blocking"][0]["waits_for"]["description"] == "a lock pg_locks no longer shows"


def test_roots_are_ordered_by_how_much_they_block():
    rows = [
        backend(1),
        backend(2, blocked_by=[1]),
        backend(5),
        backend(6, blocked_by=[5]),
        backend(7, blocked_by=[5]),
        backend(8, blocked_by=[6]),
    ]

    roots = assemble_lock_tree(rows, [])

    assert [(root["pid"], root["blocked_total"]) for root in roots] == [(5, 3), (1, 1)]


def test_a_waiter_blocked_by_two_holders_appears_under_each():
    """Two share-lockers both block the one wanting an exclusive lock; a tree
    has to show it under both, since releasing either alone frees nothing."""
    rows = [backend(1), backend(2), backend(3, blocked_by=[1, 2])]

    roots = assemble_lock_tree(rows, [holder(3, 1, "AccessShareLock"), holder(3, 2, "AccessShareLock")])

    assert [root["pid"] for root in roots] == [1, 2]
    assert [root["blocking"][0]["pid"] for root in roots] == [3, 3]


def test_the_longest_waiting_child_comes_first():
    rows = [backend(1), backend(2, blocked_by=[1], query_seconds=5.0), backend(3, blocked_by=[1], query_seconds=50.0)]

    (root,) = assemble_lock_tree(rows, [])

    assert [child["pid"] for child in root["blocking"]] == [3, 2]


def test_a_deadlock_is_reported_once_and_marked():
    """Two backends waiting on each other have no root. The server breaks the
    cycle within deadlock_timeout; until then it must not be dropped, nor
    recursed into forever."""
    rows = [backend(1, blocked_by=[2]), backend(2, blocked_by=[1])]

    roots = assemble_lock_tree(rows, [])

    assert len(roots) == 1
    assert roots[0]["cycle"] is True
    assert roots[0]["blocking"][0]["pid"] != roots[0]["pid"]
    assert roots[0]["blocking"][0]["blocking"] == []


def test_an_unblocked_backend_blocking_nobody_is_not_a_root():
    rows = [backend(1), backend(2, blocked_by=[1]), backend(9)]

    roots = assemble_lock_tree(rows, [])

    assert [root["pid"] for root in roots] == [1]


# -- What PostgreSQL withholds from a role without pg_read_all_stats ---------


def withheld(pid, usename: str | None = "postgres", **fields):
    """A backend as PostgreSQL 16 shows it to a role that may not see it."""
    row = {
        "pid": pid,
        "datname": "app",
        "usename": usename,
        "application_name": "",
        "backend_type": None,
        "state": None,
        "wait_event_type": None,
        "wait_event": None,
        "blocked_by": [],
        "state_seconds": None,
        "query_seconds": None,
        "transaction_seconds": None,
        "query": HIDDEN_QUERY_TEXT,
    }
    row.update(fields)
    return row


def answering(listed=(), withheld_count=0, roles=("postgres",), server_processes=0):
    """A driver that lists these backends, and counts this many withheld."""
    driver = MagicMock(spec=SqlDriver)

    async def execute(query, *args, **kwargs):
        if query == WITHHELD_BACKENDS_SQL:
            cells = {"withheld": withheld_count, "roles": list(roles), "server_processes": server_processes}
            return [SqlDriver.RowResult(cells=cells)]
        return [SqlDriver.RowResult(cells=row) for row in listed]

    driver.execute_query = AsyncMock(side_effect=execute)
    return driver


def ran(driver, query):
    return any(call.args[0] == query for call in driver.execute_query.await_args_list)


@pytest.mark.asyncio
async def test_backends_withheld_from_the_role_are_counted_rather_than_called_nothing(pg16, access):
    """The reported failure: activity said "(nothing running)" while other
    roles' queries ran. PostgreSQL still lists their backends to a role
    without pg_read_all_stats, with the state, query and wait withheld."""
    access.return_value = PLAIN
    driver = answering(withheld_count=7, roles=["postgres", "svc"], server_processes=4)

    backends = await ActivityInspector(driver).list_backends()

    assert backends == []
    assert backends.hidden_count == 7
    assert backends.hidden is not None
    assert backends.hidden.startswith("7 other backends, of postgres and svc, and of the server itself, are not listed")
    assert "app_user" in backends.hidden and "pg_read_all_stats" in backends.hidden and "pg_monitor" in backends.hidden
    assert "Whether they are running cannot be told" in backends.hidden


@pytest.mark.asyncio
async def test_a_withheld_backend_is_left_out_of_the_running_ones_on_purpose(pg16):
    """Its NULL state and wait once dropped it by accident; whether it is idle
    cannot be told, so it is left out, and counted apart."""
    driver = make_driver()

    await ActivityInspector(driver).list_backends()

    assert f"a.query IS DISTINCT FROM '{HIDDEN_QUERY_TEXT}'" in executed_query(driver)


@pytest.mark.asyncio
async def test_a_minimum_duration_says_how_long_cannot_be_told(pg16, access):
    access.return_value = PLAIN
    driver = answering(withheld_count=1, roles=["postgres"])

    backends = await ActivityInspector(driver).list_backends(include_idle=True, min_duration_seconds=30)

    assert backends.hidden is not None
    assert backends.hidden.startswith("1 other backend, of postgres, is not listed")
    assert "How long they have been in their state cannot be told." in backends.hidden


@pytest.mark.asyncio
async def test_withheld_backends_listed_with_idle_ones_are_counted_among_them(pg16, access):
    access.return_value = PLAIN
    driver = answering(listed=[withheld(5), withheld(6, usename=None), {"pid": 7, "state": "idle", "query": "SELECT 1"}])

    backends = await ActivityInspector(driver).list_backends(include_idle=True)

    assert [row["pid"] for row in backends] == [5, 6, 7]
    assert backends.hidden_count == 2
    assert backends.hidden is not None
    assert backends.hidden.startswith("2 of these backends are shown without state, query, wait or timings.")
    assert not ran(driver, WITHHELD_BACKENDS_SQL), "the rows themselves say which are withheld"


@pytest.mark.asyncio
async def test_a_role_shown_everything_is_told_nothing_and_asked_nothing_more(pg16):
    driver = make_driver([{"pid": 42, "state": "active", "query": "SELECT 1"}])

    backends = await ActivityInspector(driver).list_backends()

    assert backends.hidden is None and backends.hidden_count == 0
    assert not ran(driver, WITHHELD_BACKENDS_SQL)


@pytest.mark.asyncio
async def test_a_role_without_the_privilege_is_told_nothing_when_nothing_was_withheld(pg16, access):
    """A database only its own role uses withholds nothing from it."""
    access.return_value = PLAIN
    driver = answering(withheld_count=0, roles=[])

    backends = await ActivityInspector(driver).list_backends()

    assert backends.hidden is None


@pytest.mark.asyncio
async def test_a_blocked_backend_withheld_from_the_role_is_listed_and_counted(pg16, access):
    """Blocking is read from the locks, which every role is shown."""
    access.return_value = PLAIN
    driver = answering(listed=[withheld(5, blocked_by=[4]), withheld(6), {"pid": 7, "blocked_by": [4], "query": "UPDATE t SET a = 1"}])

    blocked = await ActivityInspector(driver).blocked_backends()

    assert [row["pid"] for row in blocked] == [5, 7]
    assert blocked.hidden is not None and blocked.hidden.startswith("1 of these backends is shown without state")


@pytest.mark.asyncio
async def test_blocked_backends_ask_the_server_only_when_something_was_withheld(pg16, access):
    driver = make_driver([{"pid": 2, "blocked_by": [1], "query": "UPDATE t SET a = 1"}])

    blocked = await ActivityInspector(driver).blocked_backends()

    assert blocked.hidden is None
    access.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_lock_tree_says_which_backends_in_it_are_withheld_and_that_it_is_whole(access):
    access.return_value = PLAIN
    driver = tree_driver(
        [withheld(41), withheld(42, blocked_by=[41], waiting_locktype="relation", waiting_mode="AccessShareLock")],
        [holder(42, 41, "AccessExclusiveLock")],
    )

    roots = await ActivityInspector(driver).lock_tree()

    assert [root["pid"] for root in roots] == [41]
    assert roots.hidden_count == 2
    assert roots.hidden is not None
    assert roots.hidden.startswith("2 of the backends in these trees are shown without state, query or timings.")
    assert "Who blocks whom is complete" in roots.hidden


@pytest.mark.asyncio
async def test_a_lock_tree_of_visible_backends_asks_nothing_more(access):
    driver = tree_driver([backend(41), backend(42, blocked_by=[41])], [holder(42, 41, "AccessExclusiveLock")])

    roots = await ActivityInspector(driver).lock_tree()

    assert roots.hidden is None
    access.assert_not_awaited()
    assert driver.execute_query.await_count == 2


@pytest.mark.asyncio
async def test_a_vacuum_withheld_from_the_role_is_counted(pg16, access):
    access.return_value = PLAIN
    driver = make_driver([{"pid": 9, "phase": None, "table": None, "autovacuum": None, "query": HIDDEN_QUERY_TEXT}])

    vacuums = await ActivityInspector(driver).vacuum_progress()

    assert vacuums.hidden is not None
    assert vacuums.hidden.startswith("1 of these vacuums is shown without table, phase or progress.")
    assert "autovacuum" in vacuums.hidden


@pytest.mark.asyncio
async def test_whether_a_vacuum_is_autovacuum_is_read_from_the_backend_type(pg16):
    """Read from the query text, a withheld one said False: '<insufficient
    privilege>' does not start with 'autovacuum:'. The backend type is
    withheld with the rest, so the answer is NULL, which is the truth."""
    driver = make_driver()

    await ActivityInspector(driver).vacuum_progress()

    query = executed_query(driver)
    assert "a.backend_type = 'autovacuum worker' AS autovacuum" in query
    assert "LIKE 'autovacuum:%'" not in query


def test_the_withheld_backends_query_is_allowed_in_restricted_mode():
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(WITHHELD_BACKENDS_SQL)

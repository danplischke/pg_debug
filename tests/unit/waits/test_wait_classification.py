"""Naming the patterns in one snapshot of the server's waits, from the rows of one statement."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.db.privileges import HIDDEN_QUERY_TEXT
from pg_debug.severity import Confidence
from pg_debug.severity import Severity
from pg_debug.waits import LWLOCK_PRESSURE_MIN_BACKENDS
from pg_debug.waits import classify_waits
from pg_debug.waits.classify import SNAPSHOT_SQL
from pg_debug.waits.classify import classify


def row(pid, state="active", blocked_by=(), wait=(None, None), lock=None, held_by=(), query="SELECT 1", **extra):
    """A row of SNAPSHOT_SQL. ``lock`` is (locktype, mode, relation oid, relation name, transactionid)."""
    locktype, mode, oid, relation, xid = lock or (None, None, None, None, None)
    return {
        "pid": pid,
        "datname": "app",
        "usename": "app",
        "application_name": f"app{pid}",
        "backend_type": "client backend",
        "state": state,
        "wait_event_type": wait[0],
        "wait_event": wait[1],
        "blocked_by": list(blocked_by),
        "state_seconds": 30.0,
        "query_seconds": 30.0,
        "transaction_seconds": 60.0,
        "in_transaction": True,
        "query": query,
        "waiting_locktype": locktype,
        "waiting_mode": mode,
        "waiting_relation_oid": oid,
        "waiting_relation": relation,
        "waiting_transactionid": xid,
        "tuple_relation": None,
        "held_by": [{"pid": pid_, "mode": mode_} for pid_, mode_ in held_by] or None,
        **extra,
    }


def ddl_queue():
    """One transaction holds ACCESS SHARE, a DDL waits for ACCESS EXCLUSIVE, a SELECT queues behind it."""
    return [
        row(10, state="idle in transaction", wait=("Client", "ClientRead"), query="SELECT * FROM t"),
        row(
            20,
            blocked_by=[10],
            wait=("Lock", "relation"),
            lock=("relation", "AccessExclusiveLock", 1234, "public.t", None),
            held_by=[(10, "AccessShareLock")],
            query="ALTER TABLE t ADD COLUMN y int",
        ),
        row(
            30,
            blocked_by=[20],
            wait=("Lock", "relation"),
            lock=("relation", "AccessShareLock", 1234, "public.t", None),
            held_by=[(10, "AccessShareLock")],
            query="SELECT count(*) FROM t",
        ),
    ]


def test_a_queue_behind_a_waiting_ddl_is_proven_from_pg_locks():
    diagnosis = classify(ddl_queue())

    queue = next(c for c in diagnosis.classifications if c.kind == "ddl_lock_queue")
    assert queue.confidence is Confidence.PROVEN
    assert queue.severity is Severity.HIGH
    assert queue.evidence["waiter"] == 20
    assert queue.evidence["mode"] == "ACCESS EXCLUSIVE"
    assert queue.evidence["relation"] == "public.t"
    assert queue.evidence["holders"][0]["pid"] == 10
    assert queue.evidence["holders"][0]["modes"] == ["ACCESS SHARE"]
    assert queue.evidence["queued"] == [{"pid": 30, "mode": "ACCESS SHARE"}]
    assert "pg_cancel_backend(20)" in queue.detail
    assert "lock_timeout" in queue.detail
    assert queue.to_dict()["confidence"] == "proven"


def test_the_idle_in_transaction_holder_is_named_as_the_blocker():
    diagnosis = classify(ddl_queue())

    blocker = next(c for c in diagnosis.classifications if c.kind == "idle_in_transaction_blocker")
    assert blocker.confidence is Confidence.PROVEN
    assert blocker.evidence["pid"] == 10
    assert blocker.evidence["blocking"] == [20]
    assert blocker.evidence["holds"] == ["ACCESS SHARE"]
    assert blocker.evidence["relations"] == ["public.t"]
    assert "pg_terminate_backend(10)" in blocker.detail


def test_a_waiting_ddl_with_nothing_queued_behind_it_is_not_a_queue():
    rows = ddl_queue()[:2]

    diagnosis = classify(rows)

    assert [c.kind for c in diagnosis.classifications] == ["idle_in_transaction_blocker"]


def test_an_active_blocker_is_not_idle_in_transaction():
    rows = ddl_queue()
    rows[0]["state"] = "active"

    diagnosis = classify(rows)

    assert "idle_in_transaction_blocker" not in [c.kind for c in diagnosis.classifications]


def test_a_withheld_blocker_is_said_not_to_be_classifiable():
    rows = ddl_queue()
    rows[0].update(state=None, query=HIDDEN_QUERY_TEXT)

    diagnosis = classify(rows)

    assert any("state withheld" in refusal and "10" in refusal for refusal in diagnosis.declined)
    assert "idle_in_transaction_blocker" not in [c.kind for c in diagnosis.classifications]


def test_row_contention_is_proven_once_the_waiter_holds_the_row_lock():
    rows = [
        row(10, state="idle in transaction"),
        row(
            20,
            blocked_by=[10],
            wait=("Lock", "transactionid"),
            lock=("transactionid", "ShareLock", None, None, "777"),
            tuple_relation="public.accounts",
        ),
        row(30, blocked_by=[20], wait=("Lock", "tuple"), lock=("tuple", "ExclusiveLock", 99, "public.accounts", None)),
    ]

    diagnosis = classify(rows)

    contention = next(c for c in diagnosis.classifications if c.kind == "row_lock_contention")
    assert contention.confidence is Confidence.PROVEN
    assert contention.evidence["target"] == "public.accounts"
    assert contention.evidence["waiters"] == [20, 30]
    assert contention.evidence["holders"] == [10]
    assert contention.judgment is None


def test_a_wait_for_a_transaction_alone_is_row_contention_by_inference():
    rows = [
        row(10),
        row(20, blocked_by=[10], wait=("Lock", "transactionid"), lock=("transactionid", "ShareLock", None, None, "777")),
    ]

    diagnosis = classify(rows)

    contention = next(c for c in diagnosis.classifications if c.kind == "row_lock_contention")
    assert contention.confidence is Confidence.CANDIDATE
    assert contention.judgment is not None
    assert "uniqueness check" in contention.judgment
    assert contention.evidence["transactions"] == ["777"]


def test_lwlock_pressure_is_a_candidate_with_its_judgment_said():
    rows = [row(pid, wait=("LWLock", "LockManager")) for pid in range(1, LWLOCK_PRESSURE_MIN_BACKENDS + 1)]
    rows.append(row(99))

    diagnosis = classify(rows)

    [found] = diagnosis.classifications
    assert found.kind == "lwlock_lock_manager"
    assert found.confidence is Confidence.CANDIDATE
    assert found.evidence["backends"] == LWLOCK_PRESSURE_MIN_BACKENDS
    assert found.evidence["active_backends"] == LWLOCK_PRESSURE_MIN_BACKENDS + 1
    assert "judgment" in (found.judgment or "")
    assert "activity --sample" in found.detail


def test_the_names_before_postgresql_13_are_read_too():
    rows = [row(pid, wait=("LWLock", "buffer_mapping")) for pid in range(1, LWLOCK_PRESSURE_MIN_BACKENDS + 1)]

    diagnosis = classify(rows)

    assert [c.kind for c in diagnosis.classifications] == ["lwlock_buffer_mapping"]


def test_a_few_lwlock_waits_are_declined_with_the_count():
    diagnosis = classify([row(1, wait=("LWLock", "BufferMapping"))])

    assert diagnosis.classifications == []
    assert len(diagnosis.declined) == 1
    assert "1 backend waited on LWLock:BufferMapping" in diagnosis.declined[0]
    assert str(LWLOCK_PRESSURE_MIN_BACKENDS) in diagnosis.declined[0]


def test_a_lock_wait_no_pattern_accounts_for_is_listed_as_unclassified():
    rows = [
        row(10),
        row(20, blocked_by=[10], wait=("Lock", "advisory"), lock=("advisory", "ExclusiveLock", None, None, None)),
    ]

    diagnosis = classify(rows)

    assert diagnosis.classifications == []
    assert diagnosis.unclassified == [{"pid": 20, "locktype": "advisory", "mode": "EXCLUSIVE", "relation": None, "blocked_by": [10]}]
    assert diagnosis.examined == 2


@pytest.mark.asyncio
async def test_the_snapshot_is_one_statement():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=cells) for cells in ddl_queue()])

    diagnosis = await classify_waits(driver)

    driver.execute_query.assert_awaited_once_with(SNAPSHOT_SQL)
    assert diagnosis.classifications[0].severity is Severity.HIGH


def test_the_snapshot_passes_restricted_mode():
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(SNAPSHOT_SQL)

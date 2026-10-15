"""What the waits in one snapshot of the server amount to.

``activity`` lists each backend's wait and ``locks`` who blocks whom. Several
patterns in them have a name and a remedy, and are easy to miss in a list:

- a **lock queue behind DDL**: a statement waiting for ACCESS EXCLUSIVE (or
  another lock that blocks writes) on a table someone else holds, with
  ordinary statements queued behind *it*, since PostgreSQL grants locks in
  the order they were asked for;
- an **idle-in-transaction blocker**: a session that opened a transaction,
  took locks, and is running nothing while others wait on it;
- **row lock contention**: sessions waiting for another transaction to end
  so that they can lock a row it locked;
- **LWLock pressure** on ``BufferMapping`` or ``LockManager``: several
  sessions at once waiting for one of the server's internal locks.

Each is read from one statement, so its parts are consistent with each other.
The first two are read from ``pg_locks`` and ``pg_stat_activity`` and are
:attr:`Confidence.PROVEN`; row lock contention is proven when ``pg_locks``
shows the row lock and a candidate when it shows only the wait for the
transaction; LWLock pressure is a candidate, since one instant says nothing
of how long it lasts and the cause it usually has is a judgment.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..db.privileges import HIDDEN_QUERY_TEXT
from ..db.privileges import IN_TRANSACTION
from ..db.privileges import IN_TRANSACTION_CTE
from ..severity import Confidence
from ..severity import Severity

#: Lock modes that block writes, and so queue every later writer behind a
#: request for them, in pg_locks' names for them.
QUEUEING_MODES = frozenset({"AccessExclusiveLock", "ExclusiveLock", "ShareRowExclusiveLock", "ShareLock"})

#: The LWLock tranches whose waits are classified, by the names PostgreSQL 13
#: and later report; 12 and earlier report them in lower case with an
#: underscore, and both are read.
LWLOCK_TRANCHES = {
    "BufferMapping": "BufferMapping",
    "buffer_mapping": "BufferMapping",
    "LockManager": "LockManager",
    "lock_manager": "LockManager",
}

#: How many backends must wait on one of :data:`LWLOCK_TRANCHES` at the same
#: instant before it is reported. One or two at a time is ordinary on a busy
#: server; the figure is a judgment, not a property of the server, and below
#: it the wait is said to be seen and not classified.
LWLOCK_PRESSURE_MIN_BACKENDS = 3

#: Every backend but pg-debug's own, with the lock it waits on (a backend
#: waits on at most one), the holders of a lock on that same target with
#: their modes, and the relation of the row lock it holds, if any: that is
#: how a wait for another transaction is told to be a wait for a row.
SNAPSHOT_SQL = f"""
    WITH involved AS (
        SELECT a.pid, pg_blocking_pids(a.pid) AS blocked_by
        FROM pg_stat_activity a
        WHERE a.pid <> pg_backend_pid()
    ),
    {IN_TRANSACTION_CTE}
    SELECT
        a.pid,
        a.datname,
        a.usename,
        a.application_name,
        a.backend_type,
        a.state,
        a.wait_event_type,
        a.wait_event,
        i.blocked_by,
        EXTRACT(EPOCH FROM clock_timestamp() - a.state_change)::float8 AS state_seconds,
        EXTRACT(EPOCH FROM clock_timestamp() - a.query_start)::float8 AS query_seconds,
        EXTRACT(EPOCH FROM clock_timestamp() - a.xact_start)::float8 AS transaction_seconds,
        {IN_TRANSACTION} AS in_transaction,
        a.query,
        w.locktype AS waiting_locktype,
        w.mode AS waiting_mode,
        w.relation AS waiting_relation_oid,
        CASE WHEN wc.oid IS NOT NULL THEN quote_ident(wn.nspname) || '.' || quote_ident(wc.relname) END AS waiting_relation,
        w.transactionid::text AS waiting_transactionid,
        CASE WHEN tc.oid IS NOT NULL THEN quote_ident(tn.nspname) || '.' || quote_ident(tc.relname) END AS tuple_relation,
        (
            SELECT json_agg(json_build_object('pid', h.pid, 'mode', h.mode))
            FROM pg_locks h
            WHERE w.locktype IS NOT NULL
              AND h.granted
              AND h.pid <> a.pid
              AND h.locktype = w.locktype
              AND h.database IS NOT DISTINCT FROM w.database
              AND h.relation IS NOT DISTINCT FROM w.relation
              AND h.page IS NOT DISTINCT FROM w.page
              AND h.tuple IS NOT DISTINCT FROM w.tuple
              AND h.virtualxid IS NOT DISTINCT FROM w.virtualxid
              AND h.transactionid IS NOT DISTINCT FROM w.transactionid
              AND h.classid IS NOT DISTINCT FROM w.classid
              AND h.objid IS NOT DISTINCT FROM w.objid
              AND h.objsubid IS NOT DISTINCT FROM w.objsubid
        ) AS held_by
    FROM pg_stat_activity a
    JOIN involved i ON i.pid = a.pid
    LEFT JOIN LATERAL (
        SELECT l.locktype, l.mode, l.database, l.relation, l.page, l.tuple, l.virtualxid,
               l.transactionid, l.classid, l.objid, l.objsubid
        FROM pg_locks l
        WHERE l.pid = a.pid AND NOT l.granted
        LIMIT 1
    ) w ON true
    LEFT JOIN pg_class wc ON wc.oid = w.relation
    LEFT JOIN pg_namespace wn ON wn.oid = wc.relnamespace
    LEFT JOIN LATERAL (
        SELECT l.relation
        FROM pg_locks l
        WHERE l.pid = a.pid AND l.granted AND l.locktype = 'tuple'
        LIMIT 1
    ) t ON true
    LEFT JOIN pg_class tc ON tc.oid = t.relation
    LEFT JOIN pg_namespace tn ON tn.oid = tc.relnamespace
    WHERE a.state IS DISTINCT FROM 'idle'
       OR a.pid IN (SELECT unnest(blocked_by) FROM involved)
    ORDER BY a.pid
"""


@dataclass
class WaitClassification:
    """One named pattern in the server's waits, with the evidence it was read from.

    Attributes:
        kind: ``ddl_lock_queue``, ``idle_in_transaction_blocker``,
            ``row_lock_contention``, ``lwlock_buffer_mapping`` or
            ``lwlock_lock_manager``.
        confidence: ``proven`` when read from pg_locks and pg_stat_activity,
            ``candidate`` when part of it is inferred.
        severity: How much it warrants attention.
        summary: One sentence.
        detail: What it means and what ends it.
        pids: The backends it is about.
        evidence: What it was read from: pids, lock modes, relation, ages.
        judgment: The part of it that is a judgment rather than a reading,
            when there is one.
    """

    kind: str
    confidence: Confidence
    severity: Severity
    summary: str
    detail: str
    pids: list[int]
    evidence: dict[str, Any]
    judgment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "confidence": self.confidence.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "detail": self.detail,
            "pids": self.pids,
            "evidence": self.evidence,
        }
        if self.judgment:
            result["judgment"] = self.judgment
        return result


@dataclass
class WaitDiagnosis:
    """What one snapshot's waits amount to, and what it looked at and did not classify.

    Attributes:
        classifications: The patterns found, most severe first.
        examined: How many backends the snapshot held: every one not idle,
            and every one blocking another.
        declined: Patterns seen and not classified, each with the figure the
            refusal rests on.
        unclassified: Backends waiting on a lock that no pattern accounts
            for, with what they wait on.
    """

    classifications: list[WaitClassification] = field(default_factory=list)
    examined: int = 0
    declined: list[str] = field(default_factory=list)
    unclassified: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classifications": [c.to_dict() for c in self.classifications],
            "examined": self.examined,
            "declined": self.declined,
            "unclassified": self.unclassified,
        }


async def classify_waits(sql_driver: SqlDriver | SafeSqlDriver) -> WaitDiagnosis:
    """Read one snapshot of the server's backends and locks, and classify its waits.

    Runs one statement, in restricted mode too.
    """
    rows = await sql_driver.execute_query(SNAPSHOT_SQL)
    return classify([row.cells for row in rows or []])


def _mode(mode: str | None) -> str:
    """A lock mode as the documentation writes it: ``AccessShareLock`` as ``ACCESS SHARE``."""
    if not mode:
        return "?"
    words = re.findall(r"[A-Z][a-z]*", mode.removesuffix("Lock"))
    return " ".join(words).upper() if words else mode


def _held(row: dict[str, Any]) -> dict[int, list[str]]:
    """The modes each holder of the waited-on target holds, by pid."""
    held: dict[int, list[str]] = defaultdict(list)
    for entry in row.get("held_by") or []:
        if entry.get("pid") is not None:
            held[int(entry["pid"])].append(_mode(entry.get("mode")))
    return held


def _seconds(value: Any) -> float | None:
    return round(float(value), 1) if value is not None else None


def classify(rows: list[dict[str, Any]]) -> WaitDiagnosis:
    """Classify the waits in the rows of :data:`SNAPSHOT_SQL`."""
    by_pid = {int(row["pid"]): row for row in rows}
    waiting_on: dict[int, list[int]] = defaultdict(list)
    for pid, row in by_pid.items():
        for blocker in row.get("blocked_by") or []:
            waiting_on[int(blocker)].append(pid)

    diagnosis = WaitDiagnosis(examined=len(rows))
    accounted: set[int] = set()

    for queue in _ddl_queues(by_pid):
        diagnosis.classifications.append(queue)
        accounted.update(queue.pids)
    for blocker in _idle_blockers(by_pid, waiting_on):
        diagnosis.classifications.append(blocker)
        accounted.update(blocker.evidence["blocking"])
    for contention in _row_contention(by_pid):
        diagnosis.classifications.append(contention)
        accounted.update(contention.evidence["waiters"])
    lwlocks, declined = _lwlock_pressure(by_pid)
    diagnosis.classifications.extend(lwlocks)
    diagnosis.declined.extend(declined)

    withheld_blockers = [pid for pid, row in by_pid.items() if row.get("query") == HIDDEN_QUERY_TEXT and waiting_on.get(pid)]
    if withheld_blockers:
        diagnosis.declined.append(
            f"{len(withheld_blockers)} backend{'s' if len(withheld_blockers) != 1 else ''} blocking others ("
            f"{', '.join(str(pid) for pid in withheld_blockers)}) {'has its' if len(withheld_blockers) == 1 else 'have their'} "
            "state withheld from this role, so whether it is idle in a transaction cannot be told."
        )

    for pid, row in by_pid.items():
        if row.get("waiting_locktype") is not None and pid not in accounted:
            diagnosis.unclassified.append(
                {
                    "pid": pid,
                    "locktype": row.get("waiting_locktype"),
                    "mode": _mode(row.get("waiting_mode")),
                    "relation": row.get("waiting_relation"),
                    "blocked_by": [int(p) for p in row.get("blocked_by") or []],
                }
            )

    order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
    diagnosis.classifications.sort(key=lambda c: order[c.severity])
    return diagnosis


def _ddl_queues(by_pid: dict[int, dict[str, Any]]) -> list[WaitClassification]:
    found: list[WaitClassification] = []
    for pid, row in by_pid.items():
        if row.get("waiting_locktype") != "relation" or row.get("waiting_mode") not in QUEUEING_MODES:
            continue
        relation_oid = row.get("waiting_relation_oid")
        queued = sorted(
            other_pid
            for other_pid, other in by_pid.items()
            if other_pid != pid
            and pid in [int(p) for p in other.get("blocked_by") or []]
            and other.get("waiting_locktype") == "relation"
            and other.get("waiting_relation_oid") == relation_oid
        )
        if not queued:
            continue
        held = _held(row)
        blockers = [int(p) for p in row.get("blocked_by") or []]
        holders = [p for p in blockers if p in held]
        ahead = [p for p in blockers if p not in held]
        relation = row.get("waiting_relation") or f"relation {relation_oid}"
        mode = _mode(row.get("waiting_mode"))
        holder_text = ", ".join(f"pid {p} ({' and '.join(held[p])})" for p in holders) or "a holder pg_locks no longer shows"
        evidence = {
            "waiter": pid,
            "mode": mode,
            "relation": relation,
            "waiting_seconds": _seconds(row.get("query_seconds")),
            "query": row.get("query"),
            "holders": [
                {
                    "pid": p,
                    "modes": held[p],
                    "state": by_pid.get(p, {}).get("state"),
                    "transaction_seconds": _seconds(by_pid.get(p, {}).get("transaction_seconds")),
                }
                for p in holders
            ],
            "waiting_ahead": ahead,
            "queued": [{"pid": p, "mode": _mode(by_pid[p].get("waiting_mode"))} for p in queued],
        }
        found.append(
            WaitClassification(
                kind="ddl_lock_queue",
                confidence=Confidence.PROVEN,
                severity=Severity.HIGH,
                summary=(
                    f"pid {pid} waits for {mode} on {relation}, held up by {holder_text}, and {len(queued)} "
                    f"statement{'s' if len(queued) != 1 else ''} queue{'' if len(queued) != 1 else 's'} behind it."
                ),
                detail=(
                    "PostgreSQL grants a lock in the order it was asked for: a statement whose lock conflicts with the "
                    f"waiting {mode} request waits behind it, even when the holder's lock would have let it through "
                    f"(pg_blocking_pids names pid {pid} for {', '.join(str(p) for p in queued)}). Until the holder ends its "
                    f"transaction or pid {pid} gives up, every such statement on {relation} queues. Cancelling pid {pid} "
                    f"(SELECT pg_cancel_backend({pid})) releases the queue at once; a short lock_timeout on the statement, "
                    "retried, keeps it from forming."
                ),
                pids=[pid, *holders, *queued],
                evidence=evidence,
            )
        )
    return found


def _idle_blockers(by_pid: dict[int, dict[str, Any]], waiting_on: dict[int, list[int]]) -> list[WaitClassification]:
    found: list[WaitClassification] = []
    for pid, row in by_pid.items():
        state = row.get("state") or ""
        blocking = sorted(waiting_on.get(pid, []))
        if not state.startswith("idle in transaction") or not blocking:
            continue
        holds = sorted({mode for waiter in blocking for mode in _held(by_pid[waiter]).get(pid, [])})
        targets = sorted({str(by_pid[w].get("waiting_relation")) for w in blocking if by_pid[w].get("waiting_relation")})
        idle_for = _seconds(row.get("state_seconds"))
        evidence = {
            "pid": pid,
            "state": state,
            "idle_seconds": idle_for,
            "transaction_seconds": _seconds(row.get("transaction_seconds")),
            "blocking": blocking,
            "holds": holds,
            "relations": targets,
            "last_query": row.get("query"),
            "usename": row.get("usename"),
            "application_name": row.get("application_name"),
        }
        on = f" on {', '.join(targets)}" if targets else ""
        found.append(
            WaitClassification(
                kind="idle_in_transaction_blocker",
                confidence=Confidence.PROVEN,
                severity=Severity.HIGH,
                summary=(
                    f"pid {pid} is {state} for {idle_for}s and blocks {len(blocking)} backend{'s' if len(blocking) != 1 else ''}"
                    f"{on}: {', '.join(str(p) for p in blocking)}."
                ),
                detail=(
                    "It opened a transaction, took locks, and has run nothing since: the application has not ended it. "
                    "The locks stay until it commits or rolls back. Ending it in the application, or "
                    f"SELECT pg_terminate_backend({pid}), releases the waiters; idle_in_transaction_session_timeout ends "
                    "such sessions by itself."
                ),
                pids=[pid, *blocking],
                evidence=evidence,
            )
        )
    return found


def _row_contention(by_pid: dict[int, dict[str, Any]]) -> list[WaitClassification]:
    groups: dict[str, list[int]] = defaultdict(list)
    for pid, row in by_pid.items():
        locktype = row.get("waiting_locktype")
        if locktype == "tuple":
            groups[row.get("waiting_relation") or f"relation {row.get('waiting_relation_oid')}"].append(pid)
        elif locktype == "transactionid":
            groups[row.get("tuple_relation") or f"transaction {row.get('waiting_transactionid')}"].append(pid)

    found: list[WaitClassification] = []
    for target, waiters in groups.items():
        proven = all(by_pid[w].get("waiting_locktype") == "tuple" or by_pid[w].get("tuple_relation") for w in waiters)
        holders = sorted({int(p) for w in waiters for p in by_pid[w].get("blocked_by") or []} - set(waiters))
        transactions = sorted({str(by_pid[w]["waiting_transactionid"]) for w in waiters if by_pid[w].get("waiting_transactionid")})
        evidence = {
            "target": target,
            "waiters": sorted(waiters),
            "holders": holders,
            "transactions": transactions,
            "longest_wait_seconds": max((_seconds(by_pid[w].get("query_seconds")) or 0.0 for w in waiters), default=None),
            "waits": sorted({f"{by_pid[w].get('wait_event_type')}:{by_pid[w].get('wait_event')}" for w in waiters}),
        }
        judgment = None
        if not proven:
            judgment = (
                "A wait for another transaction's id is how a row lock waits, and pg_locks names the row's table only once "
                "the waiter holds the row's tuple lock. The same wait is also how an insert waits on a uniqueness check "
                "against a key another transaction inserted, so this is row contention by inference."
            )
        count = len(waiters)
        found.append(
            WaitClassification(
                kind="row_lock_contention",
                confidence=Confidence.PROVEN if proven else Confidence.CANDIDATE,
                severity=Severity.MEDIUM,
                summary=(
                    f"{count} backend{'s' if count != 1 else ''} wait{'s' if count == 1 else ''} to lock rows "
                    f"{'in ' + target if not target.startswith('transaction') else 'held by ' + target}"
                    f"{', held by pid ' + ', '.join(str(p) for p in holders) if holders else ''}."
                ),
                detail=(
                    "Each waits for the transaction that locked or changed the row to end. They are serialized on the "
                    "same rows: a hot row (a counter, a queue head) or a transaction that holds its row locks for long."
                ),
                pids=sorted({*waiters, *holders}),
                evidence=evidence,
                judgment=judgment,
            )
        )
    return found


_LWLOCK_JUDGMENTS = {
    "BufferMapping": (
        "lwlock_buffer_mapping",
        "Backends wait to look up or replace a page in shared buffers' mapping table. It usually means many sessions "
        "reading pages that are not in shared_buffers at once: a working set larger than shared_buffers, or many "
        "sessions reading the same newly loaded pages. That cause is a judgment, not established well enough to act on "
        "alone; health --type buffer gives the cache hit rates to check it against (they read low wherever a second "
        "cache tier sits outside shared_buffers). The waits are what was read.",
    ),
    "LockManager": (
        "lwlock_lock_manager",
        "Backends wait for the shared lock table. It usually means queries take more relation locks than a backend's "
        "fast path holds (16 before PostgreSQL 18, sized from max_locks_per_transaction from 18), typically by "
        "touching many partitions or many indexes per query, so they contend for the shared table. That cause is a "
        "judgment; pg_locks' fastpath column, counted per backend, checks it. The waits are what was read.",
    ),
}


def _lwlock_pressure(by_pid: dict[int, dict[str, Any]]) -> tuple[list[WaitClassification], list[str]]:
    waiting: dict[str, list[int]] = defaultdict(list)
    for pid, row in by_pid.items():
        if row.get("wait_event_type") == "LWLock" and row.get("wait_event") in LWLOCK_TRANCHES:
            waiting[LWLOCK_TRANCHES[row["wait_event"]]].append(pid)
    active = sum(1 for row in by_pid.values() if row.get("state") == "active")

    found: list[WaitClassification] = []
    declined: list[str] = []
    for tranche, pids in sorted(waiting.items()):
        count = len(pids)
        if count < LWLOCK_PRESSURE_MIN_BACKENDS:
            declined.append(
                f"{count} backend{'s' if count != 1 else ''} waited on LWLock:{tranche} ({', '.join(str(p) for p in sorted(pids))}), "
                f"fewer than the {LWLOCK_PRESSURE_MIN_BACKENDS} at once this reports; an instant of it is ordinary."
            )
            continue
        kind, judgment = _LWLOCK_JUDGMENTS[tranche]
        found.append(
            WaitClassification(
                kind=kind,
                confidence=Confidence.CANDIDATE,
                severity=Severity.MEDIUM,
                summary=f"{count} of {active} active backends wait on LWLock:{tranche} at this instant.",
                detail=(
                    "One snapshot shows the wait, not how long it lasts: activity --sample SECONDS measures how much of the active time goes to it."
                ),
                pids=sorted(pids),
                evidence={"wait": f"LWLock:{tranche}", "backends": count, "active_backends": active, "pids": sorted(pids)},
                judgment=judgment,
            )
        )
    return found, declined


__all__ = [
    "LWLOCK_PRESSURE_MIN_BACKENDS",
    "WaitClassification",
    "WaitDiagnosis",
    "classify",
    "classify_waits",
]

"""What a server log says happened: deadlocks, lock waits, spills, vacuums, checkpoints.

The statistics views say how much; the log says when, to whom and with which
statement. A deadlock leaves nothing in ``pg_stat_database`` but a counter; the
log names both processes, what each waited for and the statement each ran.
A lock wait that ``activity`` would have caught only while it lasted is in the
log afterwards, with its holder. So is each temporary file and the statement
that wrote it, each autovacuum run and how long it took, and each time
autovacuum gave up on a table because something held a lock on it.

Every finding carries the ``file:line`` it was read from. Most of these are
logged only when a setting asks for them, and a quiet log is not a healthy
server: when a kind of event is absent, the report says which setting it
depends on rather than reporting none. The messages are matched in English,
as PostgreSQL writes them with ``lc_messages`` set to C or English.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from ..rules import DOCS_URL
from .parse import LogEntry
from .parse import ParsedLog
from .parse import read_log_text
from .parse import read_logs

#: How many ``file:line`` references a grouped finding keeps.
MAX_SOURCES = 5

_DEADLOCK_WAIT = re.compile(r"Process (\d+) waits for (\S+) on (.+?); blocked by process (\d+)\.")
_DEADLOCK_QUERY = re.compile(r"^Process (\d+): (.*)$", re.MULTILINE)
_RELATION_IN_CONTEXT = re.compile(r'relation "([^"]+)"')
_LOCK_EVENT = re.compile(
    r"^process (\d+) (still waiting for|acquired|detected deadlock while waiting for|avoided deadlock for) (\S+) on (.+?) after ([\d.]+) ms"
)
_HOLDERS = re.compile(r"Process(?:es)? holding the lock: ([\d, ]+)\.")
_TEMP_FILE = re.compile(r'^temporary file: path "(.+?)", size (\d+)')
_SKIPPED = re.compile(r'^skipping (vacuum|analyze) of "(.+?)" --- (lock not available|relation no longer exists)')
_AUTOVACUUM = re.compile(r'^automatic (aggressive )?(vacuum|analyze) (to prevent wraparound )?of table "(.+?)"')
_ELAPSED = re.compile(r"elapsed: ([\d.]+) s")
_TUPLES = re.compile(r"tuples: (\d+) removed, (\d+) remain, (\d+) are dead but not yet removable")
_TOO_FREQUENT = re.compile(r"^checkpoints are occurring too frequently \((\d+) seconds? apart\)")
_CHECKPOINT_STARTING = re.compile(r"^(?:checkpoint|restartpoint) starting: (.*)$")


def _docs(rule: str) -> dict[str, str]:
    return {"rule": rule, "docs": f"{DOCS_URL}#{rule}"}


def _keep(sources: list[str], source: str) -> None:
    if len(sources) < MAX_SOURCES:
        sources.append(source)


@dataclass
class DeadlockProcess:
    """One process in a deadlock cycle: what it waited for, behind whom, running what."""

    pid: int
    waits_for: str
    on: str
    blocked_by: int
    query: str | None = None


@dataclass
class Deadlock:
    """A deadlock PostgreSQL broke by cancelling one transaction.

    Attributes:
        time: When it was detected.
        source: The ``file:line`` of the ``deadlock detected`` error.
        cancelled_pid: The process whose transaction was cancelled.
        processes: Every process in the cycle, from the error's DETAIL.
        relations: The relations named: by name from the CONTEXT, and as
            ``relation OID of database OID`` where the lock was on a relation.
            A log holds no catalog to turn an OID into a name;
            ``SELECT 'OID'::regclass`` in that database does.
        statement: The cancelled transaction's statement.
        database: The database, where the log line prefix records it.
        user: The role, where recorded.
    """

    time: str | None
    source: str
    cancelled_pid: int | None
    processes: list[DeadlockProcess] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    statement: str | None = None
    database: str | None = None
    user: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**_docs("deadlock"), **asdict(self)}


@dataclass
class LockWait:
    """One wait for a lock that lasted past ``deadlock_timeout``.

    Attributes:
        pid: The waiting process.
        mode: The lock mode it wanted: ``AccessShareLock``, ``ShareLock``...
        target: What the lock is on, as the log names it: ``relation 16384
            of database 16383``, ``transaction 7342``, ``tuple (0,1) of
            relation ...``.
        waited_ms: How long it had waited when last logged.
        outcome: ``acquired``; ``deadlock`` when the wait ended in one;
            ``cancelled`` when the process's statement was cancelled while it
            waited, by a statement timeout or a user; or ``unresolved`` when
            no later line says how it ended.
        cancelled_by: For a cancelled wait, the error that ended it.
        holders: The processes holding the lock, from the DETAIL.
        statement: The statement that waited.
        time: When the wait was first logged.
        sources: The ``file:line`` of each line about it.
    """

    pid: int
    mode: str
    target: str
    waited_ms: float
    outcome: str = "unresolved"
    cancelled_by: str | None = None
    holders: list[int] = field(default_factory=list)
    statement: str | None = None
    time: str | None = None
    database: str | None = None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**_docs("lock_wait"), **asdict(self)}


@dataclass
class TempFileUse:
    """The temporary files one statement wrote.

    Attributes:
        statement: The statement, from the STATEMENT line; None when the log
            did not record one.
        files: How many files it wrote.
        total_bytes: Their total size.
        largest_bytes: The largest of them.
        first_time: When the first was logged.
        last_time: When the last was.
        sources: Up to five ``file:line`` references.
    """

    statement: str | None
    files: int = 0
    total_bytes: int = 0
    largest_bytes: int = 0
    first_time: str | None = None
    last_time: str | None = None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**_docs("temp_file"), **asdict(self)}


@dataclass
class SkippedVacuum:
    """A relation a vacuum or analyze skipped, how often and why.

    Attributes:
        relation: As the log names it, which is without its schema.
        operation: ``vacuum`` or ``analyze``.
        by: ``autovacuum`` (logged as LOG), or ``a manual command`` with
            ``SKIP_LOCKED`` (logged as WARNING).
        reason: ``lock not available`` or ``relation no longer exists``.
        times: How many times.
    """

    relation: str
    operation: str
    by: str
    reason: str
    times: int = 0
    first_time: str | None = None
    last_time: str | None = None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**_docs("skipped_vacuum"), **asdict(self)}


@dataclass
class AutovacuumRuns:
    """Autovacuum's or autoanalyze's runs on one table, and how long they took.

    Attributes:
        table: ``database.schema.table``, as the log names it.
        operation: ``vacuum`` or ``analyze``.
        runs: How many runs were logged. Only runs that took at least
            ``log_autovacuum_min_duration`` are.
        total_seconds: Their elapsed time together.
        longest_seconds: The longest run.
        aggressive: How many were aggressive (freezing) vacuums.
        wraparound: How many ran to prevent wraparound, which cancels for
            nothing and blocks nothing out.
        tuples_removed: Dead tuples removed, across the runs.
        dead_not_removable: The most dead tuples a run found and could not
            remove, because a transaction or slot still needed them: the
            vacuum horizon, held back.
    """

    table: str
    operation: str
    runs: int = 0
    total_seconds: float = 0.0
    longest_seconds: float = 0.0
    aggressive: int = 0
    wraparound: int = 0
    tuples_removed: int = 0
    dead_not_removable: int = 0
    last_time: str | None = None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**_docs("autovacuum_run"), **asdict(self)}


@dataclass
class CheckpointWarning:
    """``checkpoints are occurring too frequently``: WAL filled ``max_wal_size`` before the timer."""

    time: str | None
    seconds_apart: int
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {**_docs("frequent_checkpoints"), **asdict(self)}


@dataclass
class LogReport:
    """Everything a set of server logs says, and how much of them could be read.

    Attributes:
        sources: The files read, each with the format it was read as.
        lines: Lines read.
        entries: Messages found in them.
        unparsed: Lines that were neither a message nor part of one.
        unparsed_examples: A few of them, to see why.
        first_time: The earliest timestamp read.
        last_time: The latest.
        deadlocks: Each deadlock, with the processes and relations in it.
        lock_waits: Each lock wait past ``deadlock_timeout``.
        temp_files: The temporary files written, per statement, largest first.
        skipped_vacuums: Relations a vacuum or analyze skipped.
        autovacuum: Autovacuum and autoanalyze runs per table, longest first.
        checkpoint_warnings: Each ``checkpoints are occurring too frequently``.
        checkpoints: Checkpoints logged (``log_checkpoints``), by what
            started them: ``time``, ``wal``, ``immediate force wait``...
        notes: What the log cannot show: each kind of event that is absent,
            and the setting it depends on.
    """

    sources: dict[str, str] = field(default_factory=dict)
    lines: int = 0
    entries: int = 0
    unparsed: int = 0
    unparsed_examples: list[str] = field(default_factory=list)
    first_time: str | None = None
    last_time: str | None = None
    deadlocks: list[Deadlock] = field(default_factory=list)
    lock_waits: list[LockWait] = field(default_factory=list)
    temp_files: list[TempFileUse] = field(default_factory=list)
    skipped_vacuums: list[SkippedVacuum] = field(default_factory=list)
    autovacuum: list[AutovacuumRuns] = field(default_factory=list)
    checkpoint_warnings: list[CheckpointWarning] = field(default_factory=list)
    checkpoints: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": self.sources,
            "lines": self.lines,
            "entries": self.entries,
            "unparsed": self.unparsed,
            "unparsed_examples": self.unparsed_examples,
            "first_time": self.first_time,
            "last_time": self.last_time,
            "deadlocks": [d.to_dict() for d in self.deadlocks],
            "lock_waits": [w.to_dict() for w in self.lock_waits],
            "temp_files": [t.to_dict() for t in self.temp_files],
            "skipped_vacuums": [s.to_dict() for s in self.skipped_vacuums],
            "autovacuum": [a.to_dict() for a in self.autovacuum],
            "checkpoint_warnings": [c.to_dict() for c in self.checkpoint_warnings],
            "checkpoints": self.checkpoints,
            "notes": self.notes,
        }


def analyze_logs(
    paths: Iterable[str | Path],
    prefix: str | None = None,
    log_format: str | None = None,
) -> LogReport:
    """Read PostgreSQL server logs and report what they say happened. No database is needed.

    Args:
        paths: Log files: stderr, csvlog or jsonlog, plain or ``.gz``; ``-``
            for standard input.
        prefix: The server's ``log_line_prefix``, for a stderr log. Without
            it the timestamp, the process id in brackets and ``user@db`` are
            found where the common prefixes put them; with it, every field it
            holds is read.
        log_format: ``stderr``, ``csvlog`` or ``jsonlog``. Detected from each
            file's first lines when left out.

    Returns:
        A :class:`LogReport`.

    Raises:
        InvalidArgumentError: If a file cannot be read, or the format or the
            prefix is not one PostgreSQL has.
    """
    return report(read_logs(paths, prefix=prefix, log_format=log_format))


def analyze_log_lines(
    lines: Iterable[str],
    prefix: str | None = None,
    log_format: str | None = None,
    source: str = "<lines>",
) -> LogReport:
    """:func:`analyze_logs` over lines already in hand, such as a platform's log API returns."""
    parsed = ParsedLog()
    read_log_text("\n".join(line.rstrip("\n") for line in lines), source, parsed, prefix=prefix, log_format=log_format)
    return report(parsed)


def report(parsed: ParsedLog) -> LogReport:
    """What the entries of a log say happened."""
    result = LogReport(
        sources=dict(parsed.formats),
        lines=parsed.lines,
        entries=len(parsed.entries),
        unparsed=parsed.unparsed,
        unparsed_examples=list(parsed.unparsed_examples),
    )
    waits: dict[tuple[int, str, str], LockWait] = {}
    temp: dict[str | None, TempFileUse] = {}
    skipped: dict[tuple[str, str, str, str], SkippedVacuum] = {}
    runs: dict[tuple[str, str], AutovacuumRuns] = {}
    times = [entry.time for entry in parsed.entries if entry.time]
    if times:
        result.first_time, result.last_time = min(times), max(times)

    for entry in parsed.entries:
        message = entry.message
        if entry.level == "ERROR" and message.startswith("deadlock detected"):
            result.deadlocks.append(_deadlock(entry))
        elif entry.level == "ERROR" and message.startswith("canceling statement"):
            # A wait still open in this process ended with its statement.
            for wait in waits.values():
                if wait.pid == entry.pid and wait.outcome == "unresolved":
                    wait.outcome = "cancelled"
                    wait.cancelled_by = message
                    _keep(wait.sources, entry.source)
        elif entry.level == "LOG" and (match := _LOCK_EVENT.match(message)):
            _lock_event(entry, match, waits, result)
        elif match := _TEMP_FILE.match(message):
            _temp_file(entry, match, temp)
        elif entry.level in ("LOG", "WARNING") and (match := _SKIPPED.match(message)):
            _skipped(entry, match, skipped)
        elif entry.level == "LOG" and (match := _AUTOVACUUM.match(message)):
            _autovacuum(entry, match, runs)
        elif entry.level == "LOG" and (match := _TOO_FREQUENT.match(message)):
            result.checkpoint_warnings.append(CheckpointWarning(time=entry.time, seconds_apart=int(match.group(1)), source=entry.source))
        elif entry.level == "LOG" and (match := _CHECKPOINT_STARTING.match(message)):
            reason = match.group(1).strip()
            result.checkpoints[reason] = result.checkpoints.get(reason, 0) + 1

    result.temp_files = sorted(temp.values(), key=lambda use: -use.total_bytes)
    result.skipped_vacuums = sorted(skipped.values(), key=lambda skip: -skip.times)
    result.autovacuum = sorted(runs.values(), key=lambda run: -run.longest_seconds)
    result.notes = _notes(result)
    return result


def _deadlock(entry: LogEntry) -> Deadlock:
    detail = entry.detail or ""
    queries = {int(pid): query.strip() for pid, query in _DEADLOCK_QUERY.findall(detail)}
    processes = [
        DeadlockProcess(pid=int(pid), waits_for=mode, on=target, blocked_by=int(blocker), query=queries.get(int(pid)))
        for pid, mode, target, blocker in _DEADLOCK_WAIT.findall(detail)
    ]
    relations: list[str] = []
    for name in _RELATION_IN_CONTEXT.findall(entry.context or ""):
        if name not in relations:
            relations.append(name)
    for process in processes:
        if "relation" in process.on and process.on not in relations:
            relations.append(process.on)
    return Deadlock(
        time=entry.time,
        source=entry.source,
        cancelled_pid=entry.pid,
        processes=processes,
        relations=relations,
        statement=entry.statement,
        database=entry.database,
        user=entry.user,
    )


def _lock_event(entry: LogEntry, match: re.Match[str], waits: dict[tuple[int, str, str], LockWait], result: LogReport) -> None:
    pid, event, mode, target, ms = int(match.group(1)), match.group(2), match.group(3), match.group(4), float(match.group(5))
    if event == "avoided deadlock for":
        # The lock manager rearranged the queue; nobody waited for it.
        return
    key = (pid, mode, target)
    wait = waits.get(key)
    if wait is None or wait.outcome != "unresolved":
        wait = LockWait(pid=pid, mode=mode, target=target, waited_ms=ms, time=entry.time, database=entry.database, statement=entry.statement)
        waits[key] = wait
        result.lock_waits.append(wait)
    wait.waited_ms = max(wait.waited_ms, ms)
    _keep(wait.sources, entry.source)
    if entry.detail and (holders := _HOLDERS.search(entry.detail)):
        for holder in holders.group(1).split(","):
            if holder.strip().isdigit() and int(holder) not in wait.holders:
                wait.holders.append(int(holder))
    if event == "acquired":
        wait.outcome = "acquired"
    elif event.startswith("detected deadlock"):
        wait.outcome = "deadlock"


def _temp_file(entry: LogEntry, match: re.Match[str], temp: dict[str | None, TempFileUse]) -> None:
    statement = " ".join(entry.statement.split()) if entry.statement else None
    use = temp.setdefault(statement, TempFileUse(statement=statement, first_time=entry.time))
    size = int(match.group(2))
    use.files += 1
    use.total_bytes += size
    use.largest_bytes = max(use.largest_bytes, size)
    use.last_time = entry.time
    _keep(use.sources, entry.source)


def _skipped(entry: LogEntry, match: re.Match[str], skipped: dict[tuple[str, str, str, str], SkippedVacuum]) -> None:
    # vacuum.c logs autovacuum's skips as LOG, and a manual command's as a
    # WARNING to the client that asked.
    by = "autovacuum" if entry.level == "LOG" else "a manual command"
    operation, relation, reason = match.group(1), match.group(2), match.group(3)
    key = (relation, operation, by, reason)
    skip = skipped.setdefault(key, SkippedVacuum(relation=relation, operation=operation, by=by, reason=reason, first_time=entry.time))
    skip.times += 1
    skip.last_time = entry.time
    _keep(skip.sources, entry.source)


def _autovacuum(entry: LogEntry, match: re.Match[str], runs: dict[tuple[str, str], AutovacuumRuns]) -> None:
    operation, table = match.group(2), match.group(4)
    run = runs.setdefault((table, operation), AutovacuumRuns(table=table, operation=operation))
    run.runs += 1
    if (elapsed := _ELAPSED.search(entry.message)) is not None:
        seconds = float(elapsed.group(1))
        run.total_seconds = round(run.total_seconds + seconds, 3)
        run.longest_seconds = max(run.longest_seconds, seconds)
    if (tuples := _TUPLES.search(entry.message)) is not None:
        run.tuples_removed += int(tuples.group(1))
        run.dead_not_removable = max(run.dead_not_removable, int(tuples.group(3)))
    run.aggressive += bool(match.group(1))
    run.wraparound += bool(match.group(3))
    run.last_time = entry.time
    _keep(run.sources, entry.source)


def _notes(result: LogReport) -> list[str]:
    """What the log cannot show, for each kind of event it does not hold."""
    notes: list[str] = []
    if result.entries == 0:
        notes.append(
            "No log message was read. Check the format (stderr, csvlog, jsonlog) and, for a stderr log, pass the server's "
            "log_line_prefix with --prefix."
        )
        return notes
    if result.unparsed > result.entries:
        notes.append(
            f"{result.unparsed:,} lines were not log messages, more than the {result.entries:,} that were: the prefix or the format is "
            "probably not the one the log was written with."
        )
    if not result.lock_waits:
        notes.append(
            "No lock wait was logged. Waits are logged only with log_lock_waits = on (off by default until PostgreSQL 18), "
            "and only once they pass deadlock_timeout; the absence says nothing unless it was on."
        )
    if not result.temp_files:
        notes.append("No temporary file was logged. They are logged only with log_temp_files set to a size (-1, the default, logs none).")
    if not result.autovacuum:
        notes.append(
            "No autovacuum run was logged. Runs are logged when they take longer than log_autovacuum_min_duration "
            "(10 minutes by default from PostgreSQL 15, never before), so the absence says the runs were shorter or not logged."
        )
    if not result.skipped_vacuums:
        notes.append("No skipped vacuum was logged. Autovacuum logs a skip only when log_autovacuum_min_duration is not -1.")
    if not result.deadlocks and not result.checkpoint_warnings and not result.checkpoints and not result.lock_waits:
        notes.append(
            "Messages are matched in English. A server whose lc_messages is another language logs these in that language, "
            "and none of them will have been recognised."
        )
    return notes


__all__ = [
    "MAX_SOURCES",
    "AutovacuumRuns",
    "CheckpointWarning",
    "Deadlock",
    "DeadlockProcess",
    "LockWait",
    "LogReport",
    "SkippedVacuum",
    "TempFileUse",
    "analyze_log_lines",
    "analyze_logs",
    "report",
]

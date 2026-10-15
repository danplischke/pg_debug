"""Reading a PostgreSQL server log into entries, whatever format it was written in.

PostgreSQL writes its log in three formats (``log_destination``): ``stderr``,
plain text with a prefix of the administrator's choosing (``log_line_prefix``)
on every line; ``csvlog``, one CSV record per message; and, from PostgreSQL 15,
``jsonlog``, one JSON object per line. A message is one entry: its severity
and text, the ``DETAIL``, ``HINT``, ``CONTEXT`` and ``STATEMENT`` that go with
it, and as much of who and when as the format records.

In the stderr format those parts are separate lines, each with the prefix,
and a message that runs over several lines continues on lines that begin with
a tab. The prefix is whatever ``log_line_prefix`` says. Given it, every field
it holds is read; without it the line is split at its severity, and the
timestamp, the process id in brackets and a ``user@database`` are recognised
where the common prefixes put them.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from ..errors import InvalidArgumentError

#: The formats this reads, as log_destination names them.
FORMATS = ("stderr", "csvlog", "jsonlog")

#: Severities that start a message.
PRIMARY_LEVELS = ("DEBUG5", "DEBUG4", "DEBUG3", "DEBUG2", "DEBUG1", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "LOG", "FATAL", "PANIC")

#: The parts of a message PostgreSQL writes on lines of their own.
SECONDARY_LEVELS = ("DETAIL", "HINT", "QUERY", "CONTEXT", "LOCATION", "STATEMENT")

_LEVEL = "|".join(PRIMARY_LEVELS + SECONDARY_LEVELS)

#: A line split at its severity: everything before it is the prefix. The two
#: spaces after the colon are PostgreSQL's.
_AT_SEVERITY = re.compile(rf"^(?P<prefix>.*?)\b(?P<level>{_LEVEL}):  (?P<message>.*)$")

_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?(?: ?(?:[A-Z]{2,5}|[+-]\d\d(?::?\d\d)?))?")
_PID = re.compile(r"\[(\d+)\]")
_USER_AT_DB = re.compile(r"(?:^|\s)([\w.$-]*)@([\w.$-]*)(?:\s|$)")
_KEY_VALUE = re.compile(r"\b(user|db|app|application_name)=([^,\s\]]*)")

#: log_line_prefix escapes, as patterns, with the field each one fills.
_ESCAPES: dict[str, tuple[str, str | None]] = {
    "a": (r".*?", "application"),
    "u": (r"[^\s@]*?", "user"),
    "d": (r"[^\s]*?", "database"),
    "r": (r"\S*?", None),
    "h": (r"\S*?", None),
    "L": (r"\S*?", None),
    "b": (r".*?", None),
    "p": (r"\d+", "pid"),
    "P": (r"\d*", None),
    "t": (r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?: \S+)?", "time"),
    "m": (r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+(?: \S+)?", "time"),
    "n": (r"\d+(?:\.\d+)?", "time"),
    "i": (r".*?", None),
    "e": (r"[0-9A-Z]{5}", None),
    "c": (r"[0-9a-f]+\.[0-9a-f]+", None),
    "l": (r"\d+", None),
    "s": (r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?: \S+)?", None),
    "v": (r"\S*?", None),
    "x": (r"\d+", None),
    "Q": (r"-?\d+", None),
}

#: csvlog's columns, by position. PostgreSQL 13 added backend_type and 14
#: leader_pid and query_id at the end; the ones before have never moved.
_CSV_COLUMNS = {
    "time": 0,
    "user": 1,
    "database": 2,
    "pid": 3,
    "level": 11,
    "message": 13,
    "detail": 14,
    "hint": 15,
    "internal_query": 16,
    "context": 18,
    "statement": 19,
    "application": 22,
}

#: csvlog records have at least this many columns (PostgreSQL 12).
_CSV_MIN_COLUMNS = 23


@dataclass
class LogEntry:
    """One message from the log, with the parts that go with it.

    Attributes:
        source: Where it starts: ``file:line``.
        level: Its severity: ``LOG``, ``ERROR``, ``WARNING``...
        message: Its text, continuation lines included.
        time: When it was logged, as the log wrote it; None if the format
            or the prefix does not record it.
        pid: The process that logged it, where recorded.
        user: The role, where recorded.
        database: The database, where recorded.
        application: The application name, where recorded.
        detail: Its DETAIL.
        hint: Its HINT.
        context: Its CONTEXT.
        statement: The statement it was logged for (STATEMENT).
    """

    source: str
    level: str
    message: str
    time: str | None = None
    pid: int | None = None
    user: str | None = None
    database: str | None = None
    application: str | None = None
    detail: str | None = None
    hint: str | None = None
    context: str | None = None
    statement: str | None = None


@dataclass
class ParsedLog:
    """What reading the logs produced, and what it could not read.

    Attributes:
        entries: Every message, in the order read.
        formats: The format each source was read as.
        lines: Lines read, across every source.
        unparsed: Lines that were neither a message nor part of one, with a
            few of them as examples. A count that is most of the file means
            the format or the prefix was not the one the log was written in.
    """

    entries: list[LogEntry] = field(default_factory=list)
    formats: dict[str, str] = field(default_factory=dict)
    lines: int = 0
    unparsed: int = 0
    unparsed_examples: list[str] = field(default_factory=list)


def prefix_pattern(prefix: str) -> re.Pattern[str]:
    """A pattern for a line that begins with ``log_line_prefix``, as the server sets it.

    The fields the prefix holds are read into groups named ``time``, ``pid``,
    ``user``, ``database`` and ``application``. ``%q`` ends the part a
    background process leaves out, so what follows it is optional.

    Raises:
        InvalidArgumentError: For an escape PostgreSQL does not have.
    """
    pattern = ""
    named: set[str] = set()
    optional_from: int | None = None
    i = 0
    while i < len(prefix):
        char = prefix[i]
        if char != "%":
            pattern += re.escape(char)
            i += 1
            continue
        # %-10u pads to a width; the padding is spaces, read as part of the field.
        match = re.match(r"%(-?\d+)?(.)", prefix[i:])
        if match is None:
            pattern += re.escape(char)
            i += 1
            continue
        escape = match.group(2)
        i += len(match.group(0))
        if escape == "%":
            pattern += "%"
        elif escape == "q":
            optional_from = len(pattern)
        elif escape in _ESCAPES:
            body, name = _ESCAPES[escape]
            if match.group(1):
                body = rf"\s*{body}\s*"
            if name is not None and name not in named:
                named.add(name)
                pattern += f"(?P<{name}>{body})"
            else:
                pattern += f"(?:{body})"
        else:
            raise InvalidArgumentError(f"%{escape} in the log line prefix {prefix!r} is not an escape log_line_prefix has.")
    if optional_from is not None:
        pattern = pattern[:optional_from] + "(?:" + pattern[optional_from:] + ")?"
    return re.compile(rf"^{pattern}(?P<level>{_LEVEL}):  (?P<message>.*)$")


def detect_format(first_lines: list[str]) -> str:
    """Which format a log is in, from its first lines."""
    for line in first_lines:
        text = line.strip()
        if not text:
            continue
        if text.startswith("{"):
            try:
                record = json.loads(text)
            except ValueError:
                return "stderr"
            return "jsonlog" if isinstance(record, dict) and "error_severity" in record else "stderr"
        try:
            row = next(csv.reader([text]))
        except (csv.Error, StopIteration):
            return "stderr"
        if len(row) >= _CSV_MIN_COLUMNS and row[_CSV_COLUMNS["level"]] in PRIMARY_LEVELS:
            return "csvlog"
        return "stderr"
    return "stderr"


def read_logs(
    sources: Iterable[str | Path],
    prefix: str | None = None,
    log_format: str | None = None,
) -> ParsedLog:
    """Read log files into entries. ``-`` reads standard input; a ``.gz`` file is read compressed.

    Raises:
        InvalidArgumentError: If a file cannot be read, or the format is not
            one PostgreSQL writes.
    """
    parsed = ParsedLog()
    for source in sources:
        name = str(source)
        try:
            if name == "-":
                text = sys.stdin.read()
            elif name.endswith(".gz"):
                with gzip.open(name, "rb") as compressed:
                    text = compressed.read().decode("utf-8", errors="replace")
            else:
                # A log holds whatever bytes clients sent; a line in another
                # encoding should not cost the rest of the file.
                text = Path(name).read_bytes().decode("utf-8", errors="replace")
        except OSError as e:
            raise InvalidArgumentError(f"Could not read {name}: {e.strerror or e}.") from e
        read_log_text(text, name, parsed, prefix=prefix, log_format=log_format)
    return parsed


def read_log_text(text: str, source: str, parsed: ParsedLog, prefix: str | None = None, log_format: str | None = None) -> ParsedLog:
    """Read one log's text into ``parsed``."""
    lines = text.splitlines()
    fmt = log_format or detect_format(lines[:20])
    if fmt not in FORMATS:
        raise InvalidArgumentError(f"{fmt!r} is not a log format PostgreSQL writes: {', '.join(FORMATS)}.")
    parsed.formats[source] = fmt
    parsed.lines += len(lines)
    if fmt == "csvlog":
        _read_csv(text, source, parsed)
    elif fmt == "jsonlog":
        _read_json(lines, source, parsed)
    else:
        _read_stderr(lines, source, parsed, prefix_pattern(prefix) if prefix is not None else None)
    return parsed


def _unparsed(parsed: ParsedLog, source: str, number: int, line: str) -> None:
    parsed.unparsed += 1
    if len(parsed.unparsed_examples) < 3:
        parsed.unparsed_examples.append(f"{source}:{number}: {line[:120]}")


def _read_stderr(lines: list[str], source: str, parsed: ParsedLog, pattern: re.Pattern[str] | None) -> None:
    # Each process's open message, which its DETAIL and STATEMENT lines join;
    # other processes' lines can come between them.
    open_entries: dict[int | None, LogEntry] = {}
    last: tuple[LogEntry, str] | None = None
    for number, line in enumerate(lines, start=1):
        if line.startswith("\t") and last is not None:
            entry, part = last
            setattr(entry, part, f"{getattr(entry, part) or ''}\n{line[1:]}")
            continue
        fields = _split_line(line, pattern)
        if fields is None:
            if line.strip():
                _unparsed(parsed, source, number, line)
            continue
        level, message = fields["level"], fields["message"]
        pid = int(fields["pid"]) if fields.get("pid") else None
        if level in SECONDARY_LEVELS:
            owner = open_entries.get(pid)
            if owner is None:
                _unparsed(parsed, source, number, line)
                continue
            part = _PART.get(level)
            if part is None:
                # Where in PostgreSQL's source it was raised: nothing to report.
                last = None
                continue
            setattr(owner, part, message if getattr(owner, part) is None else f"{getattr(owner, part)}\n{message}")
            last = (owner, part)
            continue
        entry = LogEntry(
            source=f"{source}:{number}",
            level=level,
            message=message,
            time=fields.get("time"),
            pid=pid,
            user=fields.get("user") or None,
            database=fields.get("database") or None,
            application=fields.get("application") or None,
        )
        parsed.entries.append(entry)
        open_entries[pid] = entry
        last = (entry, "message")


#: The entry field each secondary line fills.
_PART = {
    "DETAIL": "detail",
    "HINT": "hint",
    "QUERY": "context",
    "CONTEXT": "context",
    "STATEMENT": "statement",
}


def _split_line(line: str, pattern: re.Pattern[str] | None) -> dict[str, Any] | None:
    """A stderr line's fields, or None if it is not a log line."""
    if pattern is not None:
        match = pattern.match(line)
        return match.groupdict() if match else None
    match = _AT_SEVERITY.match(line)
    if match is None:
        return None
    prefix = match.group("prefix")
    fields: dict[str, Any] = {"level": match.group("level"), "message": match.group("message")}
    if (time := _TIMESTAMP.search(prefix)) is not None:
        fields["time"] = time.group(0)
    if (pid := _PID.search(prefix)) is not None:
        fields["pid"] = pid.group(1)
    for key, value in _KEY_VALUE.findall(prefix):
        fields[{"user": "user", "db": "database"}.get(key, "application")] = value
    if "user" not in fields and (at := _USER_AT_DB.search(prefix.split("]", 1)[-1])) is not None:
        fields["user"], fields["database"] = at.group(1), at.group(2)
    return fields


def _read_csv(text: str, source: str, parsed: ParsedLog) -> None:
    reader = csv.reader(io.StringIO(text))
    while True:
        start = reader.line_num + 1
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error as e:
            _unparsed(parsed, source, start, f"(not CSV: {e})")
            continue
        if len(row) < _CSV_MIN_COLUMNS or row[_CSV_COLUMNS["level"]] not in PRIMARY_LEVELS:
            _unparsed(parsed, source, start, ",".join(row))
            continue

        def column(name: str, row: list[str] = row) -> str | None:
            return row[_CSV_COLUMNS[name]] or None

        pid = column("pid")
        parsed.entries.append(
            LogEntry(
                source=f"{source}:{start}",
                level=row[_CSV_COLUMNS["level"]],
                message=row[_CSV_COLUMNS["message"]],
                time=column("time"),
                pid=int(pid) if pid and pid.isdigit() else None,
                user=column("user"),
                database=column("database"),
                application=column("application"),
                detail=column("detail"),
                hint=column("hint"),
                context=column("context") or column("internal_query"),
                statement=column("statement"),
            )
        )


def _read_json(lines: list[str], source: str, parsed: ParsedLog) -> None:
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            _unparsed(parsed, source, number, line)
            continue
        if not isinstance(record, dict) or "error_severity" not in record or "message" not in record:
            _unparsed(parsed, source, number, line)
            continue
        pid = record.get("pid")
        parsed.entries.append(
            LogEntry(
                source=f"{source}:{number}",
                level=str(record["error_severity"]),
                message=str(record["message"]),
                time=record.get("timestamp"),
                pid=int(pid) if isinstance(pid, int) else None,
                user=record.get("user"),
                database=record.get("dbname"),
                application=record.get("application_name"),
                detail=record.get("detail"),
                hint=record.get("hint"),
                context=record.get("context") or record.get("internal_query"),
                statement=record.get("statement"),
            )
        )


__all__ = [
    "FORMATS",
    "LogEntry",
    "ParsedLog",
    "detect_format",
    "prefix_pattern",
    "read_log_text",
    "read_logs",
]

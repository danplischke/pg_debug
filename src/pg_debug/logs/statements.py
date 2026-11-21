"""The statements a log timed: ``auto_explain`` plans and ``log_min_duration_statement`` lines.

``auto_explain`` logs the plan of every statement slower than
``auto_explain.log_min_duration``, as it ran, with the parameters it ran
with. That is the plan to diagnose: not the one ``EXPLAIN`` would choose today
for a value picked by hand, but the one the slow run had. A plan logged as JSON
with ``auto_explain.log_analyze`` on carries the actual rows and timings, and
is diagnosed here with the same rules ``diagnose`` applies, with no database:
the two findings that need the catalog (stale statistics, correlated columns)
say that they could not be checked.

``log_min_duration_statement`` logs the text and the duration of each
statement past it, without a plan. Both are grouped by query, constants aside,
so a thousand runs of one statement are one line with their count, their total
and their slowest.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from pglast import parser as pg_parser

from ..errors import PgDebugError
from ..rules import DOCS_URL
from ..tags import parse_sql_comment
from .parse import LogEntry

#: How many ``file:line`` references a grouped statement keeps.
MAX_SOURCES = 5

#: How many distinct sets of sqlcommenter tags a grouped statement keeps.
MAX_CALLERS = 5

_PLAN = re.compile(r"^duration: ([\d.]+) ms  plan:\s*\n?(.*)$", re.DOTALL)
_STATEMENT = re.compile(r"^duration: ([\d.]+) ms  (?:statement|execute [^:\n]*|bind [^:\n]*|parse [^:\n]*): (.*)$", re.DOTALL)
_TEXT_QUERY = re.compile(r"^\s*Query Text: (.*)$", re.MULTILINE)


def _keep(sources: list[str], source: str) -> None:
    if len(sources) < MAX_SOURCES:
        sources.append(source)


def _keep_caller(callers: list[dict[str, str]], tags: dict[str, str]) -> None:
    if tags and tags not in callers and len(callers) < MAX_CALLERS:
        callers.append(tags)


def query_key(sql: str) -> str:
    """What runs of one statement share: its fingerprint, which ignores constants and comments.

    A statement pglast cannot parse is known by its text with whitespace
    collapsed.
    """
    try:
        return pg_parser.fingerprint(sql)
    except Exception:  # pglast raises its own ParseError, and others for what it cannot fingerprint
        return " ".join(sql.split())


@dataclass
class LoggedPlan:
    """The ``auto_explain`` plans of one query, and what is wrong with the slowest.

    Attributes:
        query: The query's text, from the slowest run.
        key: What its runs were grouped by: the query identifier where
            ``auto_explain.log_verbose`` logged one, else the fingerprint.
        runs: How many plans were logged for it.
        total_ms: Their durations together.
        slowest_ms: The slowest run's duration; its plan is the one diagnosed.
        plan_format: ``json`` or ``text``, as ``auto_explain.log_format`` wrote it.
        analyzed: Whether the plan carries actual rows and timings
            (``auto_explain.log_analyze``). Without them nothing can be
            diagnosed: an estimate has nothing to be compared with.
        findings: What is wrong with the slowest run's plan, most severe
            first; :class:`~pg_debug.plans.PlanFinding` objects.
        not_diagnosed: Why the plan was not diagnosed, when it was not.
        plan: The slowest run's plan, as logged.
        callers: The distinct sqlcommenter tags its runs carried: who sent it.
        database: The database, where the log line prefix records it.
        user: The role, where recorded.
        first_time: When the first was logged.
        last_time: When the last was.
        sources: Up to five ``file:line`` references, the slowest first.
    """

    query: str
    key: str
    runs: int = 0
    total_ms: float = 0.0
    slowest_ms: float = 0.0
    plan_format: str = "json"
    analyzed: bool = False
    findings: list[Any] = field(default_factory=list)
    not_diagnosed: str | None = None
    plan: Any = None
    callers: list[dict[str, str]] = field(default_factory=list)
    database: str | None = None
    user: str | None = None
    first_time: str | None = None
    last_time: str | None = None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": "logged_plan",
            "docs": f"{DOCS_URL}#logged_plan",
            "query": self.query,
            "key": self.key,
            "runs": self.runs,
            "total_ms": round(self.total_ms, 3),
            "slowest_ms": round(self.slowest_ms, 3),
            "plan_format": self.plan_format,
            "analyzed": self.analyzed,
            "findings": [finding.to_dict() for finding in self.findings],
            "not_diagnosed": self.not_diagnosed,
            "plan": self.plan,
            "callers": self.callers,
            "database": self.database,
            "user": self.user,
            "first_time": self.first_time,
            "last_time": self.last_time,
            "sources": self.sources,
        }


@dataclass
class SlowStatement:
    """One query's runs that ``log_min_duration_statement`` logged.

    Attributes:
        query: Its text, from the slowest run.
        key: Its fingerprint, which ignores constants and comments.
        runs: How many runs were logged.
        total_ms: Their durations together.
        slowest_ms: The slowest.
        callers: The distinct sqlcommenter tags its runs carried.
        database: The database, where recorded.
        user: The role, where recorded.
        first_time: When the first was logged.
        last_time: When the last was.
        sources: Up to five ``file:line`` references.
    """

    query: str
    key: str
    runs: int = 0
    total_ms: float = 0.0
    slowest_ms: float = 0.0
    callers: list[dict[str, str]] = field(default_factory=list)
    database: str | None = None
    user: str | None = None
    first_time: str | None = None
    last_time: str | None = None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": "slow_statement",
            "docs": f"{DOCS_URL}#slow_statement",
            "query": self.query,
            "key": self.key,
            "runs": self.runs,
            "total_ms": round(self.total_ms, 3),
            "slowest_ms": round(self.slowest_ms, 3),
            "callers": self.callers,
            "database": self.database,
            "user": self.user,
            "first_time": self.first_time,
            "last_time": self.last_time,
            "sources": self.sources,
        }


class StatementReader:
    """Collects the plans and slow statements in a log's entries, grouped by query."""

    def __init__(self, min_ratio: float | None = None):
        self.min_ratio = min_ratio
        self.plans: dict[str, LoggedPlan] = {}
        self.statements: dict[str, SlowStatement] = {}
        # The slowest plan of each query, kept until the end and diagnosed then:
        # one diagnosis per query, not one per run.
        self._slowest: dict[str, Any] = {}

    def read(self, entry: LogEntry) -> bool:
        """Take the entry if it is a plan or a timed statement; say whether it was."""
        if entry.level != "LOG" or not entry.message.startswith("duration: "):
            return False
        if match := _PLAN.match(entry.message):
            self._plan(entry, float(match.group(1)), match.group(2))
            return True
        if match := _STATEMENT.match(entry.message):
            self._statement(entry, float(match.group(1)), match.group(2).strip())
            return True
        return False

    def _plan(self, entry: LogEntry, ms: float, body: str) -> None:
        body = body.strip()
        document: dict[str, Any] | None = None
        if body.startswith("{"):
            try:
                loaded = json.loads(body)
            except ValueError:
                loaded = None
            if isinstance(loaded, dict):
                document = loaded
        if document is not None:
            query = str(document.get("Query Text") or "")
            query_id = document.get("Query Identifier")
            key = f"queryid:{query_id}" if query_id not in (None, 0) else query_key(query)
            plan_format = "json"
            plan: Any = document
        else:
            found = _TEXT_QUERY.search(body)
            query = found.group(1).strip() if found else ""
            key = query_key(query) if query else " ".join(body.split())[:200]
            plan_format = "text"
            plan = body

        logged = self.plans.get(key)
        if logged is None:
            logged = LoggedPlan(query=query, key=key, plan_format=plan_format, first_time=entry.time, database=entry.database, user=entry.user)
            self.plans[key] = logged
        logged.runs += 1
        logged.total_ms += ms
        logged.last_time = entry.time
        _keep_caller(logged.callers, parse_sql_comment(query))
        if ms >= logged.slowest_ms:
            logged.slowest_ms = ms
            logged.query = query or logged.query
            logged.plan_format = plan_format
            logged.plan = plan
            logged.database = entry.database or logged.database
            logged.user = entry.user or logged.user
            # The slowest run's line first, then the others as they come.
            if entry.source in logged.sources:
                logged.sources.remove(entry.source)
            logged.sources.insert(0, entry.source)
            del logged.sources[MAX_SOURCES:]
        else:
            _keep(logged.sources, entry.source)

    def _statement(self, entry: LogEntry, ms: float, query: str) -> None:
        key = query_key(query)
        slow = self.statements.get(key)
        if slow is None:
            slow = SlowStatement(query=query, key=key, first_time=entry.time, database=entry.database, user=entry.user)
            self.statements[key] = slow
        slow.runs += 1
        slow.total_ms += ms
        slow.last_time = entry.time
        if ms >= slow.slowest_ms:
            slow.slowest_ms = ms
            slow.query = query
        _keep_caller(slow.callers, parse_sql_comment(query))
        _keep(slow.sources, entry.source)

    def finish(self) -> tuple[list[LoggedPlan], list[SlowStatement]]:
        """Diagnose each query's slowest plan; both lists, the most total time first."""
        from ..plans.diagnoser import DEFAULT_MIN_RATIO
        from ..plans.diagnoser import diagnose_plan

        for logged in self.plans.values():
            if logged.plan_format != "json":
                logged.not_diagnosed = (
                    "auto_explain logged this plan as text, which cannot be read node by node; set auto_explain.log_format = 'json'."
                )
                continue
            logged.analyzed = _has_actuals(logged.plan.get("Plan") if isinstance(logged.plan, dict) else None)
            if not logged.analyzed:
                logged.not_diagnosed = (
                    "The plan has the planner's estimates only: auto_explain.log_analyze is off, so there are no actual rows to compare them with."
                )
                continue
            try:
                logged.findings = diagnose_plan(logged.plan, min_ratio=self.min_ratio or DEFAULT_MIN_RATIO)
            except PgDebugError as e:
                # One unreadable plan should not cost the rest of the report.
                logged.not_diagnosed = str(e)
        plans = sorted(self.plans.values(), key=lambda p: -p.total_ms)
        statements = sorted(self.statements.values(), key=lambda s: -s.total_ms)
        return plans, statements


def _has_actuals(node: Any) -> bool:
    return isinstance(node, dict) and ("Actual Rows" in node or "Actual Loops" in node)


__all__ = ["MAX_CALLERS", "LoggedPlan", "SlowStatement", "StatementReader", "query_key"]

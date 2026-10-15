"""The public names of the kinds of finding, and what each needs to be found.

A finding is reported in prose that changes as the prose is improved, under a
check name written for reading ("Invalid index check"). Neither is something a
build should key on. Every kind of finding ``health``, ``model``, ``config``
and ``logs`` report therefore has a rule id here: short, lower case, stable
across releases, unique across the commands, and the anchor of its entry in
the rules reference. ``--suppress`` and the ``[suppress]`` table of
``pg-debug.toml`` name rules by it, and in JSON every finding carries its
``rule`` and a ``docs`` link.

Each rule also says what it reads (:class:`Needs`), which decides whether it
can say anything in CI. A rule that reads only the catalog sees the same thing
on a freshly migrated, empty database as on production: a foreign key without
an index is one in both. A rule that reads what the tables hold, or the
counters a workload leaves, finds nothing on an empty database, and nothing
is not a pass. ``--static`` runs only the first kind.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import InvalidArgumentError

#: Where the rules reference lives. Each rule's entry is the heading named by
#: its id, so ``DOCS_URL#unindexed_foreign_key`` lands on it.
DOCS_URL = "https://github.com/danplischke/pg_debug/blob/main/plugins/pg-debug/skills/pg-debug/references/rules.md"


class Needs(str, Enum):
    """What a rule reads, and so where it can run."""

    #: The schema's definitions: tables, columns, constraints, indexes. The
    #: same on a freshly migrated, empty database as anywhere else.
    CATALOG = "catalog"
    #: What the tables hold: sizes, row estimates, the samples ANALYZE took.
    #: An empty database has none of it.
    DATA = "data"
    #: The counters and sessions a running workload leaves behind. Only the
    #: database the workload runs on has them.
    WORKLOAD = "workload"
    #: The server's settings, which a CI database seldom shares with
    #: production.
    SETTINGS = "settings"
    #: A server log the user hands over; no connection at all.
    LOG = "log"


@dataclass(frozen=True)
class Rule:
    """One kind of finding.

    Attributes:
        id: The public name: stable, and unique across commands.
        command: The command that reports it.
        title: What it finds, in a few words.
        needs: What it reads.
        check: For a health rule, the name of the check that reports it.
    """

    id: str
    command: str
    title: str
    needs: Needs
    check: str | None = None

    @property
    def docs(self) -> str:
        """The link to this rule's entry in the rules reference."""
        return f"{DOCS_URL}#{self.id}"

    @property
    def static(self) -> bool:
        """Whether it reads only the catalog, so a CI database can answer it."""
        return self.needs is Needs.CATALOG

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "command": self.command, "title": self.title, "needs": self.needs.value, "docs": self.docs}


def _health(id: str, check: str, title: str, needs: Needs) -> Rule:
    return Rule(id=id, command="health", title=title, needs=needs, check=check)


# The health check names are the ones the checks report under; they are
# written out rather than imported, so that this module stays importable from
# anywhere in the package. tests/unit/test_rules.py holds them to the checks.
_RULES: tuple[Rule, ...] = (
    _health("invalid_index", "Invalid index check", "An index a failed concurrent build left invalid", Needs.CATALOG),
    _health("duplicate_index", "Duplicate index check", "An index another index already covers", Needs.CATALOG),
    _health("index_bloat", "Index bloat", "An index much larger than its rows need", Needs.DATA),
    _health("unused_index", "Unused index check", "An index no scan has used while the counters counted", Needs.WORKLOAD),
    _health("connections_in_use", "Connection health", "Client connections near the slots ordinary roles can use", Needs.WORKLOAD),
    _health("idle_in_transaction", "Connection health", "A session idle in a transaction past the limit", Needs.WORKLOAD),
    _health("transaction_id_wraparound", "Vacuum health", "A database approaching transaction id wraparound", Needs.WORKLOAD),
    _health("autovacuum_backlog", "Autovacuum backlog", "A table autovacuum is not keeping clean, and why", Needs.WORKLOAD),
    _health("vacuum_horizon", "Vacuum horizon", "A transaction, slot or prepared transaction holding back vacuum", Needs.WORKLOAD),
    _health("sequence_exhaustion", "Sequence health", "A sequence approaching its maximum", Needs.DATA),
    _health("replica_not_receiving", "Replication health", "A replica that is not receiving from its primary", Needs.WORKLOAD),
    _health("index_cache_hit_rate", "Buffer health for indexes", "Index reads missing the buffer cache", Needs.WORKLOAD),
    _health("table_cache_hit_rate", "Buffer health for tables", "Table reads missing the buffer cache", Needs.WORKLOAD),
    _health("invalid_constraint", "Constraint health", "A constraint added NOT VALID and never validated", Needs.CATALOG),
    _health("stale_statistics", "Statistics health", "A table never analyzed, or changed a lot since", Needs.WORKLOAD),
    _health("table_bloat", "Table bloat", "A table much larger than its rows need", Needs.DATA),
    _health("sequential_scans", "Sequential scan health", "A table read sequentially far more than through an index", Needs.WORKLOAD),
    # The data model review; the ids are RuleId's values.
    Rule("unindexed_foreign_key", "model", "A foreign key with no index behind it", Needs.CATALOG),
    Rule("foreign_key_type_mismatch", "model", "A foreign key whose two sides differ in type", Needs.CATALOG),
    Rule("missing_primary_key", "model", "A table without a primary key", Needs.CATALOG),
    Rule("column_order_padding", "model", "Columns ordered so that alignment padding costs bytes per row", Needs.DATA),
    Rule("naive_timestamp", "model", "A timestamp without time zone", Needs.CATALOG),
    Rule("blank_padded_char", "model", "A char(n) column", Needs.CATALOG),
    Rule("nullable_never_null", "model", "A nullable column the statistics never saw NULL in", Needs.DATA),
    Rule("low_cardinality_text", "model", "A text column with a handful of distinct values", Needs.DATA),
    Rule("json_column", "model", "A json column, which jsonb would index and compare", Needs.CATALOG),
    Rule("primary_key_overflow", "model", "An integer primary key whose sequence has used much of its range", Needs.DATA),
    Rule("mostly_null_index", "model", "A btree index whose leading column is mostly NULL", Needs.DATA),
    # The configuration review.
    Rule("fsync_off", "config", "fsync is off", Needs.SETTINGS),
    Rule("full_page_writes_off", "config", "full_page_writes is off", Needs.SETTINGS),
    Rule("autovacuum_off", "config", "Autovacuum is off for the whole server", Needs.SETTINGS),
    Rule("work_mem_spills", "config", "Queries spilling to temporary files", Needs.WORKLOAD),
    Rule("requested_checkpoints", "config", "Checkpoints forced by WAL volume rather than the timer", Needs.WORKLOAD),
    Rule("shared_buffers_cache_misses", "config", "The buffer cache missing reads", Needs.WORKLOAD),
    Rule("effective_cache_size_default", "config", "effective_cache_size at its default below the database's size", Needs.DATA),
    Rule("random_page_cost_default", "config", "random_page_cost at the spinning-disk default while reads hit the cache", Needs.WORKLOAD),
    Rule("slow_query_log_off", "config", "No statement is ever logged for being slow", Needs.SETTINGS),
    Rule("idle_transaction_timeout_off", "config", "No limit on how long a transaction may sit idle", Needs.SETTINGS),
    # The server log.
    Rule("deadlock", "logs", "A deadlock, with the processes and relations in it", Needs.LOG),
    Rule("lock_wait", "logs", "A lock waited for past deadlock_timeout", Needs.LOG),
    Rule("temp_file", "logs", "A statement that wrote temporary files", Needs.LOG),
    Rule("skipped_vacuum", "logs", "A vacuum or analyze skipped because a lock was not available", Needs.LOG),
    Rule("autovacuum_run", "logs", "How long autovacuum and autoanalyze took on a table", Needs.LOG),
    Rule("frequent_checkpoints", "logs", "Checkpoints occurring closer together than checkpoint_warning", Needs.LOG),
)

#: Every rule, by id.
RULES: Mapping[str, Rule] = {rule.id: rule for rule in _RULES}

#: The commands whose findings carry rules.
COMMANDS = ("health", "model", "config", "logs")

_BY_CHECK: dict[str, list[Rule]] = {}
for _rule in _RULES:
    if _rule.check is not None:
        _BY_CHECK.setdefault(_rule.check, []).append(_rule)


def rule(rule_id: str) -> Rule:
    """The rule of this id.

    Raises:
        InvalidArgumentError: If there is none, naming the ones there are.
    """
    try:
        return RULES[rule_id]
    except KeyError:
        raise InvalidArgumentError(f"No rule is called {rule_id!r}. The rules are: {', '.join(sorted(RULES))}.") from None


def rules_of(command: str) -> list[Rule]:
    """The rules a command reports, in the order the reference lists them."""
    return [r for r in _RULES if r.command == command]


def check_rules(check: str) -> list[Rule]:
    """The rules a health check reports; empty for a name no check reports under."""
    return list(_BY_CHECK.get(check, ()))


def health_rule(check: str, finding: Mapping[str, Any] | None = None) -> Rule | None:
    """The rule a health finding, or a check's verdict without findings, belongs to.

    A check that reports more than one kind of finding says which in the
    finding's ``kind``; the connection check does.
    """
    candidates = _BY_CHECK.get(check)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    kind = finding.get("kind") if finding is not None else None
    return next((r for r in candidates if r.id == kind), None)


def is_static_check(check: str) -> bool:
    """Whether every rule of a health check reads only the catalog."""
    rules = _BY_CHECK.get(check)
    return bool(rules) and all(r.static for r in rules)


def parse_rule_ids(ids: Iterable[str], source: str) -> set[str]:
    """Rule ids as given, each checked against the rules there are.

    Raises:
        InvalidArgumentError: Naming ``source`` and the id that is not a rule,
            so that a misspelt suppression does not silently suppress nothing.
    """
    parsed: set[str] = set()
    for value in ids:
        name = str(value).strip()
        if not name:
            continue
        if name not in RULES:
            raise InvalidArgumentError(f"{source}: no rule is called {name!r}. The rules are: {', '.join(sorted(RULES))}.")
        parsed.add(name)
    return parsed


__all__ = [
    "COMMANDS",
    "DOCS_URL",
    "RULES",
    "Needs",
    "Rule",
    "check_rules",
    "health_rule",
    "is_static_check",
    "parse_rule_ids",
    "rule",
    "rules_of",
]

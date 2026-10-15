"""Baselines: what a check found last time, so a gate fails only on what is new.

``health``, ``model``, ``config``, ``upgrade-check`` and ``security`` all have an ``--exit-code`` that turns
them into checks a build can run. The trouble with adopting such a check is
the first run: an existing database has findings, some of them deliberate,
and a gate that fails on all of them on day one is a gate that gets turned
off. A baseline is the set of findings a team has looked at and accepted.
With one in hand the gate fails only on a finding that is not in it, and a
finding that goes away simply stops being listed.

A baseline holds identities, not findings. A bloated index is the same
finding at 31% and at 34%, so what is recorded is what the finding is about
— the check and the object it names, the rule and the table, the setting —
and never the numbers that change between runs. The file is JSON, one entry
per line in a stable order, so it diffs cleanly and belongs in the
repository next to the workflow that reads it.

An entry can also carry a ``fingerprint`` of the object's definition: a hash
of ``pg_get_indexdef``, ``pg_get_constraintdef`` or the columns' types. It is
not part of the identity. A finding whose object is defined differently now
than when it was accepted is ``changed``, not ``known``, and fails the gate as
a new one would: what was accepted was a finding about the old definition.
``pg-debug plans`` records each query's plan shape the same way, and fails
when it changes. Version 1 files, which carry no fingerprints, still load;
their findings are known or new, never changed.

A rule can be suppressed outright, from the ``[suppress]`` table of
``pg-debug.toml`` or ``--suppress``. Its findings are still reported, marked
suppressed with the reason, and never fail the gate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit import AuditFinding
from .audit import AuditReport
from .configuration import ConfigFinding
from .errors import InvalidArgumentError
from .health import HealthCheckResult
from .model import ModelFinding
from .rules import health_rule
from .severity import Severity

#: The version this module writes. It reads every version in READABLE_VERSIONS.
BASELINE_VERSION = 2

#: Version 1 had no fingerprints; its entries are identities alone.
READABLE_VERSIONS = (1, 2)

#: Fields an entry may carry beside its identity. They are compared, not
#: matched on: a finding is the same finding whatever they hold.
ANNOTATIONS = ("fingerprint", "shape", "about")

#: The commands a baseline can be taken of.
BASELINE_COMMANDS = ("health", "model", "config", "upgrade-check", "security", "plans")

#: The keys of a health finding that say *what* it is about, in the order
#: they are recorded. Everything else a finding carries is a measurement, or
#: evidence that changes from run to run: a session left idle in a transaction
#: is known by its role, database and application name, never by its pid,
#: which the same job gets anew each time it reconnects.
HEALTH_IDENTITY_KEYS = (
    "schema",
    "table",
    "index",
    "name",
    "sequence",
    "column",
    "constraint",
    "collation",
    "slot_name",
    "database",
    "kind",
    "covered_by",
    "usename",
    "datname",
    "application_name",
)

#: Fields added to a finding to say which rule it is, not what it is about.
_RULE_FIELDS = ("rule", "docs")

Key = dict[str, Any]

KNOWN = "known"
NEW = "new"
CHANGED = "changed"


def identity(key: Mapping[str, Any]) -> Key:
    """A key without its annotations: what the finding is about, and nothing else."""
    return {name: value for name, value in key.items() if name not in ANNOTATIONS}


def _canonical(key: Mapping[str, Any]) -> str:
    return json.dumps(key, sort_keys=True, separators=(",", ":"), default=str)


def _identity_text(key: Mapping[str, Any]) -> str:
    return _canonical(identity(key))


def health_keys(results: Mapping[str, HealthCheckResult]) -> list[Key]:
    """The identity of everything the health checks flagged.

    Each finding becomes ``{"check": name, ...identifying fields}``. A check
    that is unhealthy without naming any object — a cache hit rate — becomes
    ``{"check": name}`` on its own, since the verdict is about the check
    itself. A check that could not run found nothing and gives no key:
    accepting its failure would accept whatever it would have found once it
    ran.
    """
    return [entry.key for entry in _health_entries(results)]


def health_finding_key(check: str, finding: Mapping[str, Any]) -> Key:
    """What one health finding is about."""
    key: Key = {"check": check}
    for field_name in HEALTH_IDENTITY_KEYS:
        value = finding.get(field_name)
        if value is not None and value != "":
            key[field_name] = str(value)
    if len(key) == 1:
        # Nothing identifies it by name; the finding's shape will have to.
        # Numbers are left out, so a changed measurement stays the same key,
        # and so is the rule it was tagged with, which is not what it is about.
        for field_name, value in sorted(finding.items()):
            if isinstance(value, str) and field_name not in _RULE_FIELDS:
                key[field_name] = value
    return key


def model_finding_key(finding: ModelFinding) -> Key:
    """What one data model finding is about: the rule, the table, the columns."""
    return {"rule": finding.rule.value, "schema": finding.schema, "table": finding.table, "columns": list(finding.columns)}


def config_finding_key(finding: ConfigFinding) -> Key:
    """What one configuration finding is about: the setting."""
    return {"setting": finding.setting}


def audit_finding_key(finding: AuditFinding) -> Key:
    """What one audit finding is about: the check and its subject."""
    return {"check": finding.check, "subject": finding.subject}


#: The commands whose result is an :class:`~pg_debug.audit.AuditReport`.
AUDIT_COMMANDS = ("upgrade-check", "security")


@dataclass(frozen=True)
class Entry:
    """One finding as the gate sees it.

    Attributes:
        key: What it is about.
        rule: The id of its rule, which a suppression names.
        fails_new: Whether it fails the gate when the baseline does not hold
            it — and so when there is no baseline at all.
        fails_changed: Whether it fails the gate when the baseline holds it
            with another fingerprint.
    """

    key: Key
    rule: str | None
    fails_new: bool
    fails_changed: bool = True


def _health_entries(results: Mapping[str, HealthCheckResult]) -> list[Entry]:
    entries: list[Entry] = []
    for name, result in results.items():
        if result.healthy or result.error is not None:
            continue
        if not result.findings:
            rule = health_rule(name)
            entries.append(Entry({"check": name}, rule.id if rule else None, fails_new=True))
            continue
        for finding in result.findings:
            rule = health_rule(name, finding)
            entries.append(Entry(health_finding_key(name, finding), rule.id if rule else None, fails_new=True))
    return entries


def entries_of(command: str, result: Any) -> list[Entry]:
    """Every finding of a result as the gate sees it, in result order."""
    if command == "health":
        return _health_entries(result)
    if command == "model":
        return [Entry(model_finding_key(f), f.rule.value, fails_new=f.severity is Severity.HIGH) for f in result]
    if command == "config":
        return [Entry(config_finding_key(f), f.rule, fails_new=f.severity is Severity.HIGH) for f in result]
    if command in AUDIT_COMMANDS:
        report: AuditReport = result
        return [Entry(audit_finding_key(f), None, fails_new=f.severity is Severity.HIGH) for f in report.findings]
    if command == "plans":
        # A query the baseline does not hold has no accepted shape to differ
        # from; only a shape that changed fails.
        return [Entry(shape.key(), None, fails_new=False) for shape in result if shape.fingerprint is not None]
    raise InvalidArgumentError(f"`pg-debug {command}` has no baseline: only {', '.join(BASELINE_COMMANDS)} do.")


def keys_of(command: str, result: Any) -> list[Key]:
    """Every finding of a result as a key, in result order."""
    return [entry.key for entry in entries_of(command, result)]


@dataclass
class Partition:
    """A result split against a baseline and the suppressed rules.

    Attributes:
        new: Keys of findings the baseline does not hold. Without a baseline,
            every finding that is not suppressed.
        known: Keys it holds, with the same fingerprint or none to compare.
        changed: Keys it holds with another fingerprint: the object the
            finding is about is defined differently than when it was
            accepted, or a query's plan has another shape.
        suppressed: Keys of findings whose rule is suppressed, whatever the
            baseline says of them.
        new_failures: How many findings fail the gate: those of ``new`` that
            would fail on their own — every health finding, the high-severity
            model and configuration findings — and those of ``changed``.
        has_baseline: Whether a baseline was compared against.
        suppressions: The suppressed rules, each with why.
        suppressed_by_rule: How many findings each suppressed rule held back.
        previous: For each changed key, the entry the baseline holds, so that
            what it was can be shown beside what it is.
    """

    new: list[Key] = field(default_factory=list)
    known: list[Key] = field(default_factory=list)
    changed: list[Key] = field(default_factory=list)
    suppressed: list[Key] = field(default_factory=list)
    new_failures: int = 0
    has_baseline: bool = True
    suppressions: dict[str, str] = field(default_factory=dict)
    suppressed_by_rule: dict[str, int] = field(default_factory=dict)
    previous: dict[str, Key] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict, repr=False)

    def status(self, key: Mapping[str, Any]) -> str | None:
        """``known``, ``new`` or ``changed``, or None when there is no baseline."""
        if not self.has_baseline:
            return None
        return self.statuses.get(_identity_text(key), NEW)

    def mark(self, payload: dict[str, Any], key: Mapping[str, Any], rule: str | None) -> dict[str, Any]:
        """A finding's JSON with what the gate made of it.

        ``baseline`` is its status against the baseline, when there is one,
        and ``suppressed`` the reason its rule is suppressed, when it is.
        """
        status = self.status(key)
        if status is not None:
            payload["baseline"] = status
        if rule is not None and rule in self.suppressions:
            payload["suppressed"] = self.suppressions[rule]
        return payload


@dataclass
class Baseline:
    """A set of accepted findings for one command."""

    command: str
    keys: list[Key] = field(default_factory=list)
    created: str = ""
    version: int = BASELINE_VERSION

    def __post_init__(self) -> None:
        self._entries = {_identity_text(key): key for key in self.keys}

    def __contains__(self, key: Mapping[str, Any]) -> bool:
        return _identity_text(key) in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def entry(self, key: Mapping[str, Any]) -> Key | None:
        """The accepted entry for what ``key`` is about, annotations and all."""
        return self._entries.get(_identity_text(key))

    def status(self, key: Mapping[str, Any]) -> str:
        """Whether a finding is ``new``, ``known`` or ``changed``.

        Changed needs a fingerprint on both sides: one the baseline was saved
        without (a version 1 file, an object that could not be read) is
        known, since nothing says it differs.
        """
        accepted = self.entry(key)
        if accepted is None:
            return NEW
        then, now = accepted.get("fingerprint"), key.get("fingerprint")
        if then is not None and now is not None and then != now:
            return CHANGED
        return KNOWN

    @classmethod
    def of(cls, command: str, result: Any, keys: Sequence[Key] | None = None) -> Baseline:
        """The baseline that accepts everything ``result`` found.

        Args:
            command: The command that produced ``result``.
            result: What it found.
            keys: The keys to record in place of ``keys_of(command, result)``,
                with their fingerprints.

        Raises:
            InvalidArgumentError: If health checks in ``result`` could not
                run, or queries could not be planned. Such a baseline would
                hold nothing for them, so whatever they find once they run
                would fail the gate as new, and saved over an accepted
                baseline it would discard what was accepted.
        """
        if command == "health":
            failed = [name for name, check in result.items() if check.error is not None]
            if failed:
                raise InvalidArgumentError(
                    f"No baseline of this run: {len(failed)} of {len(result)} health {'check' if len(result) == 1 else 'checks'} "
                    f"could not run ({', '.join(failed)}), and it would hold nothing for them. Take one from a run in which every check ran."
                )
        if command == "plans":
            failed = [shape.name for shape in result if shape.error is not None]
            if failed:
                raise InvalidArgumentError(
                    f"No baseline of this run: {len(failed)} of {len(result)} {'query' if len(result) == 1 else 'queries'} "
                    f"could not be planned ({', '.join(failed)}). Take one from a run in which every query was planned."
                )
        recorded = list(keys) if keys is not None else keys_of(command, result)
        return cls(command=command, keys=recorded, created=datetime.now(UTC).isoformat(timespec="seconds"))

    @classmethod
    def load(cls, path: str | Path, command: str) -> Baseline:
        """Read a baseline, refusing one written for a different command.

        Raises:
            InvalidArgumentError: If the file is missing, not a baseline, or
                was saved by another command. A ``health`` baseline applied to
                ``model`` would silently accept nothing, which is worse than
                an error.
        """
        path = Path(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise InvalidArgumentError(f"No baseline at {path}. Save one from a run you accept with --save-baseline {path}.") from None
        except ValueError as e:
            raise InvalidArgumentError(f"{path} is not a baseline: {e}") from e
        if not isinstance(payload, dict) or payload.get("version") not in READABLE_VERSIONS or not isinstance(payload.get("findings"), list):
            raise InvalidArgumentError(f"{path} is not a pg_debug baseline (expected version {' or '.join(str(v) for v in READABLE_VERSIONS)}).")
        if payload.get("command") != command:
            raise InvalidArgumentError(f"{path} is a baseline for `pg-debug {payload.get('command')}`, not `pg-debug {command}`.")
        return cls(
            command=command,
            keys=[dict(key) for key in payload["findings"]],
            created=str(payload.get("created") or ""),
            version=int(payload["version"]),
        )

    def save(self, path: str | Path) -> None:
        """Write the baseline, one finding per line in a stable order.

        Each identity is written once: three sessions of the same job left
        idle in transaction are one accepted finding, not three.
        """
        ordered = sorted(self._entries.values(), key=_identity_text)
        lines = [
            "{",
            f'  "version": {BASELINE_VERSION},',
            f'  "command": {json.dumps(self.command)},',
            f'  "created": {json.dumps(self.created)},',
        ]
        if ordered:
            lines.append('  "findings": [')
            lines.extend("    " + _canonical(key) + ("," if i < len(ordered) - 1 else "") for i, key in enumerate(ordered))
            lines.extend(["  ]", "}", ""])
        else:
            lines.extend(['  "findings": []', "}", ""])
        Path(path).write_text("\n".join(lines), encoding="utf-8")


def partition(
    command: str,
    result: Any,
    baseline: Baseline | None = None,
    suppress: Mapping[str, str] | None = None,
    keys: Sequence[Key] | None = None,
) -> Partition:
    """Split what a run found into new, known, changed and suppressed, and count what fails.

    Args:
        command: The command that produced ``result``.
        result: What it found.
        baseline: The accepted findings. Without one every finding is new.
        suppress: Rules whose findings are reported but never fail, each with
            the reason it is suppressed.
        keys: The findings' keys with their fingerprints, in result order, in
            place of ``keys_of(command, result)``.
    """
    entries = entries_of(command, result)
    if keys is not None:
        entries = [Entry(key, entry.rule, entry.fails_new, entry.fails_changed) for entry, key in zip(entries, keys, strict=True)]
    suppressions = dict(suppress or {})
    split = Partition(has_baseline=baseline is not None, suppressions=suppressions)

    for entry in entries:
        status = baseline.status(entry.key) if baseline is not None else NEW
        split.statuses[_identity_text(entry.key)] = status
        if entry.rule is not None and entry.rule in suppressions:
            split.suppressed.append(entry.key)
            split.suppressed_by_rule[entry.rule] = split.suppressed_by_rule.get(entry.rule, 0) + 1
            continue
        if status == KNOWN:
            split.known.append(entry.key)
        elif status == CHANGED:
            split.changed.append(entry.key)
            split.previous[_identity_text(entry.key)] = baseline.entry(entry.key) or {}  # type: ignore[union-attr]
            split.new_failures += entry.fails_changed
        else:
            split.new.append(entry.key)
            split.new_failures += entry.fails_new
    return split


def previous_entry(split: Partition, key: Mapping[str, Any]) -> Key | None:
    """What the baseline held for a changed key."""
    return split.previous.get(_identity_text(key))


def describe_key(key: Mapping[str, Any]) -> str:
    """A key as a short phrase: ``Index bloat: public.orders orders_idx``."""
    if "query" in key:
        return f"query {key['query']}" + (f": {key['about']}" if key.get("about") else "")
    parts: list[str] = []
    schema, table = key.get("schema"), key.get("table")
    if schema and table:
        parts.append(f"{schema}.{table}")
    elif table:
        parts.append(str(table))
    for name in (
        "index",
        "name",
        "sequence",
        "column",
        "constraint",
        "collation",
        "slot_name",
        "database",
        "setting",
        "kind",
        "covered_by",
        "subject",
    ):
        if key.get(name):
            parts.append(str(key[name]))
    # A session's identity reads as nothing without the field names.
    parts.extend(f"{name}={key[name]}" for name in ("usename", "datname", "application_name") if key.get(name))
    if key.get("columns"):
        parts.append("(" + ", ".join(str(c) for c in key["columns"]) + ")")
    label = key.get("check") or key.get("rule") or "finding"
    return f"{label}: {' '.join(parts)}" if parts else str(label)


__all__ = [
    "ANNOTATIONS",
    "AUDIT_COMMANDS",
    "BASELINE_COMMANDS",
    "BASELINE_VERSION",
    "CHANGED",
    "HEALTH_IDENTITY_KEYS",
    "KNOWN",
    "NEW",
    "READABLE_VERSIONS",
    "Baseline",
    "Entry",
    "Partition",
    "audit_finding_key",
    "config_finding_key",
    "describe_key",
    "entries_of",
    "health_finding_key",
    "health_keys",
    "identity",
    "keys_of",
    "model_finding_key",
    "partition",
    "previous_entry",
]

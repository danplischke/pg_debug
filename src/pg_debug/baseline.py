"""Baselines: what a check found last time, so a gate fails only on what is new.

``health``, ``model`` and ``config`` all have an ``--exit-code`` that turns
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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Mapping
from typing import Sequence

from .configuration import ConfigFinding
from .errors import InvalidArgumentError
from .health import HealthCheckResult
from .model import ModelFinding
from .severity import Severity

BASELINE_VERSION = 1

#: The keys of a health finding that say *what* it is about, in the order
#: they are recorded. Everything else a finding carries is a measurement.
HEALTH_IDENTITY_KEYS = (
    "schema",
    "table",
    "index",
    "name",
    "sequence",
    "column",
    "constraint",
    "slot_name",
    "database",
    "kind",
    "covered_by",
)

Key = Dict[str, Any]


def _canonical(key: Mapping[str, Any]) -> str:
    return json.dumps(key, sort_keys=True, separators=(",", ":"), default=str)


def health_keys(results: Mapping[str, HealthCheckResult]) -> List[Key]:
    """The identity of everything the health checks flagged.

    Each finding becomes ``{"check": name, ...identifying fields}``. A check
    that is unhealthy without naming any object — connection count, cache
    hit rate — becomes ``{"check": name}`` on its own, since the verdict is
    about the check itself.
    """
    keys: List[Key] = []
    for name, result in results.items():
        if result.healthy:
            continue
        if not result.findings:
            keys.append({"check": name})
            continue
        for finding in result.findings:
            keys.append(health_finding_key(name, finding))
    return keys


def health_finding_key(check: str, finding: Mapping[str, Any]) -> Key:
    """What one health finding is about."""
    key: Key = {"check": check}
    for field_name in HEALTH_IDENTITY_KEYS:
        value = finding.get(field_name)
        if value is not None and value != "":
            key[field_name] = str(value)
    if len(key) == 1:
        # Nothing identifies it by name; the finding's shape will have to.
        # Numbers are left out, so a changed measurement stays the same key.
        for field_name, value in sorted(finding.items()):
            if isinstance(value, str):
                key[field_name] = value
    return key


def model_finding_key(finding: ModelFinding) -> Key:
    """What one data model finding is about: the rule, the table, the columns."""
    return {"rule": finding.rule.value, "schema": finding.schema, "table": finding.table, "columns": list(finding.columns)}


def config_finding_key(finding: ConfigFinding) -> Key:
    """What one configuration finding is about: the setting."""
    return {"setting": finding.setting}


@dataclass
class Partition:
    """A result split against a baseline.

    Attributes:
        new: Keys of findings the baseline does not hold.
        known: Keys it does hold.
        new_failures: How many of ``new`` would fail the gate on their own —
            every new health finding, and the high-severity new model and
            configuration findings.
    """

    new: List[Key] = field(default_factory=list)
    known: List[Key] = field(default_factory=list)
    new_failures: int = 0


@dataclass
class Baseline:
    """A set of accepted findings for one command."""

    command: str
    keys: List[Key] = field(default_factory=list)
    created: str = ""

    def __post_init__(self) -> None:
        self._canonical = {_canonical(key) for key in self.keys}

    def __contains__(self, key: Mapping[str, Any]) -> bool:
        return _canonical(key) in self._canonical

    def __len__(self) -> int:
        return len(self._canonical)

    @classmethod
    def of(cls, command: str, result: Any) -> Baseline:
        """The baseline that accepts everything ``result`` found."""
        return cls(command=command, keys=keys_of(command, result), created=datetime.now(UTC).isoformat(timespec="seconds"))

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
            payload = json.loads(path.read_text())
        except FileNotFoundError:
            raise InvalidArgumentError(f"No baseline at {path}. Save one from a run you accept with --save-baseline {path}.") from None
        except ValueError as e:
            raise InvalidArgumentError(f"{path} is not a baseline: {e}") from e
        if not isinstance(payload, dict) or payload.get("version") != BASELINE_VERSION or not isinstance(payload.get("findings"), list):
            raise InvalidArgumentError(f"{path} is not a pg_debug baseline (expected version {BASELINE_VERSION}).")
        if payload.get("command") != command:
            raise InvalidArgumentError(f"{path} is a baseline for `pg-debug {payload.get('command')}`, not `pg-debug {command}`.")
        return cls(command=command, keys=[dict(key) for key in payload["findings"]], created=str(payload.get("created") or ""))

    def save(self, path: str | Path) -> None:
        """Write the baseline, one finding per line in a stable order."""
        ordered = sorted(self.keys, key=_canonical)
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
        Path(path).write_text("\n".join(lines))


def keys_of(command: str, result: Any) -> List[Key]:
    """Every finding of a result as a key, in result order."""
    if command == "health":
        return health_keys(result)
    if command == "model":
        return [model_finding_key(f) for f in result]
    if command == "config":
        return [config_finding_key(f) for f in result]
    raise InvalidArgumentError(f"`pg-debug {command}` has no baseline: only health, model and config do.")


def partition(command: str, result: Any, baseline: Baseline) -> Partition:
    """Split what a run found into new and known, and count what would fail."""
    split = Partition()
    if command == "health":
        for key in health_keys(result):
            if key in baseline:
                split.known.append(key)
            else:
                split.new.append(key)
                split.new_failures += 1
        return split

    findings: Sequence[ModelFinding | ConfigFinding] = result
    for finding in findings:
        key = model_finding_key(finding) if isinstance(finding, ModelFinding) else config_finding_key(finding)
        if key in baseline:
            split.known.append(key)
        else:
            split.new.append(key)
            if finding.severity is Severity.HIGH:
                split.new_failures += 1
    return split


def describe_key(key: Mapping[str, Any]) -> str:
    """A key as a short phrase: ``Index health: public.orders orders_idx``."""
    parts: List[str] = []
    schema, table = key.get("schema"), key.get("table")
    if schema and table:
        parts.append(f"{schema}.{table}")
    elif table:
        parts.append(str(table))
    for name in ("index", "name", "sequence", "column", "constraint", "slot_name", "setting", "kind", "covered_by"):
        if key.get(name):
            parts.append(str(key[name]))
    if key.get("columns"):
        parts.append("(" + ", ".join(str(c) for c in key["columns"]) + ")")
    label = key.get("check") or key.get("rule") or "finding"
    return f"{label}: {' '.join(parts)}" if parts else str(label)


__all__ = [
    "BASELINE_VERSION",
    "HEALTH_IDENTITY_KEYS",
    "Baseline",
    "Partition",
    "config_finding_key",
    "describe_key",
    "health_finding_key",
    "health_keys",
    "keys_of",
    "model_finding_key",
    "partition",
]

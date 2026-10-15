"""The structured result every health check returns."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

from ..rules import check_rules
from ..rules import health_rule


@dataclass
class HealthCheckResult:
    """The outcome of a single health check.

    Attributes:
        name: Human-readable check name, e.g. "Invalid index check".
        healthy: False when the check found a problem, and False when it could
            not run, since a check that never looked has not shown anything is
            well: a caller that reads only this flag must not take an
            unfinished run for a clean one. ``error`` tells the two apart. A
            check that ran and found nothing to measure (no statistics
            collected yet, for example) reports True — it found no problem,
            it just had nothing to look at.
        summary: One-line (occasionally multi-line) description of the outcome,
            suitable for a text report.
        findings: The objects the check flagged, one dict each. Empty for a
            healthy check, and for one that could not run.
        metrics: Scalar measurements and the thresholds they were compared
            against.
        error: Why the check could not run, when it could not: the server's
            refusal, a statement timeout, restricted mode declining a query.
            None for a check that ran, whatever it found.
    """

    name: str
    healthy: bool
    summary: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def could_not_run(cls, name: str, error: str) -> HealthCheckResult:
        """The result of a check that raised instead of returning one."""
        return cls(name=name, healthy=False, summary=f"Could not run: {error}", error=error)

    def to_dict(self) -> dict[str, Any]:
        """Return the result as a JSON-friendly dict.

        ``error`` is there only when the check could not run, so the output of
        a check that ran is what it always was.

        ``rules`` lists the rules the check reports, each with its ``id`` and
        ``docs`` link, and each finding carries the ``rule`` and ``docs`` of
        its own kind. The replication check's findings are the slots it lists
        whatever its verdict, not findings of its rule, so they carry none.
        """
        rules = check_rules(self.name)
        findings = self.findings
        if rules and self.name not in VERDICT_ONLY_CHECKS:
            findings = [_with_rule(self.name, finding) for finding in self.findings]
        payload: dict[str, Any] = {
            "name": self.name,
            "healthy": self.healthy,
            "summary": self.summary,
            "findings": findings,
            "metrics": self.metrics,
        }
        if rules:
            payload["rules"] = [{"id": r.id, "docs": r.docs} for r in rules]
        if self.error is not None:
            payload["error"] = self.error
        return payload


#: Checks whose findings are context for the verdict, not findings of their
#: rule: the replication check lists every slot, healthy or not.
VERDICT_ONLY_CHECKS = frozenset({"Replication health"})


def _with_rule(check: str, finding: dict[str, Any]) -> dict[str, Any]:
    rule = health_rule(check, finding)
    return finding if rule is None else {"rule": rule.id, "docs": rule.docs, **finding}


__all__ = ["VERDICT_ONLY_CHECKS", "HealthCheckResult"]

"""The vocabulary an audit is reported in: what each check examined, and what it found.

An audit is a list of questions with yes-or-no answers, and the answer people
skip past is the dangerous one: a check that could not look reads, in a list
of findings, exactly like a check that looked and found nothing. So a report
carries one :class:`CheckOutcome` per check as well as the findings, and an
outcome that could not tell says why, in terms of the privilege or setting it
was missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any

from ..severity import SEVERITY_ORDER
from ..severity import Confidence
from ..severity import Severity


class CheckStatus(str, Enum):
    """What happened when a check ran."""

    #: It looked, and found nothing.
    CLEAR = "clear"
    #: It looked, and found something; the findings say what.
    FOUND = "found"
    #: It could not look, or could not judge what it saw. ``reason`` says why.
    UNKNOWN = "unknown"
    #: The question does not arise here: the version, the target or the
    #: platform rules it out. ``reason`` says which.
    NOT_APPLICABLE = "not_applicable"


@dataclass
class CheckOutcome:
    """One check's answer.

    Attributes:
        check: The check's id, as findings name it.
        status: What happened.
        examined: What it looked at, with the figure: ``"4 login roles"``.
        reason: Why it could not tell, or why it did not apply.
    """

    check: str
    status: CheckStatus
    examined: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"check": self.check, "status": self.status.value, "examined": self.examined}
        if self.reason is not None:
            result["reason"] = self.reason
        return result


@dataclass
class AuditFinding:
    """One thing an audit found.

    Attributes:
        check: The id of the check that found it.
        subject: What it is about, stable from run to run: ``public.t.col``,
            ``role alice``, ``slot sub1``. A baseline knows a finding by its
            check and subject.
        severity: How much it matters.
        confidence: ``proven`` when read from the catalog or a setting,
            ``candidate`` when inferred from a pattern, such as the other
            tables of a schema having row-level security.
        summary: One sentence.
        detail: The argument, with this server's figures.
        remediation: Statements or steps that fix it.
        evidence: The rows and settings behind it.
        verify: For a candidate, how to confirm it.
    """

    check: str
    subject: str
    severity: Severity
    confidence: Confidence
    summary: str
    detail: str
    remediation: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    verify: str | None = None

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (SEVERITY_ORDER[self.severity], self.check, self.subject)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "check": self.check,
            "subject": self.subject,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "summary": self.summary,
            "detail": self.detail,
            "remediation": self.remediation,
            "evidence": self.evidence,
        }
        if self.verify is not None:
            result["verify"] = self.verify
        return result


@dataclass
class AuditReport:
    """Everything an audit examined and found.

    Attributes:
        command: ``upgrade-check`` or ``security``.
        server_version: The version the server reports, ``16.4``.
        findings: Most severe first.
        checks: One outcome per check, in the order they ran.
        target_version: For an upgrade check, the major version upgraded to.
        notes: What the reader should know about the run as a whole, such as
            which database the per-database checks looked at.
    """

    command: str
    server_version: str
    findings: list[AuditFinding] = field(default_factory=list)
    checks: list[CheckOutcome] = field(default_factory=list)
    target_version: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def unknown(self) -> list[CheckOutcome]:
        """The checks that could not tell."""
        return [outcome for outcome in self.checks if outcome.status is CheckStatus.UNKNOWN]

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"command": self.command, "server_version": self.server_version}
        if self.target_version is not None:
            result["target_version"] = self.target_version
        result["findings"] = [finding.to_dict() for finding in self.findings]
        result["checks"] = [outcome.to_dict() for outcome in self.checks]
        result["notes"] = list(self.notes)
        return result


def outcome_of(check: str, examined: str, findings: list[AuditFinding]) -> CheckOutcome:
    """The outcome of a check that looked: found, or clear."""
    return CheckOutcome(check=check, status=CheckStatus.FOUND if findings else CheckStatus.CLEAR, examined=examined)


def unknown(check: str, examined: str, reason: str) -> CheckOutcome:
    return CheckOutcome(check=check, status=CheckStatus.UNKNOWN, examined=examined, reason=reason)


def not_applicable(check: str, reason: str) -> CheckOutcome:
    return CheckOutcome(check=check, status=CheckStatus.NOT_APPLICABLE, examined="nothing", reason=reason)


def plural(count: int, word: str, words: str | None = None) -> str:
    """``1 role``, ``3 roles``."""
    return f"{count} {word if count == 1 else (words or word + 's')}"


__all__ = [
    "AuditFinding",
    "AuditReport",
    "CheckOutcome",
    "CheckStatus",
    "Confidence",
    "Severity",
    "not_applicable",
    "outcome_of",
    "plural",
    "unknown",
]

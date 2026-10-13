"""The structured result every health check returns."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any


@dataclass
class HealthCheckResult:
    """The outcome of a single health check.

    Attributes:
        name: Human-readable check name, e.g. "Invalid index check".
        healthy: False when the check found a problem. A check that cannot
            measure anything (no statistics collected yet, for example) reports
            True — it found no problem, it just had nothing to look at.
        summary: One-line (occasionally multi-line) description of the outcome,
            suitable for a text report.
        findings: The objects the check flagged, one dict each. Empty for a
            healthy check.
        metrics: Scalar measurements and the thresholds they were compared
            against.
    """

    name: str
    healthy: bool
    summary: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the result as a JSON-friendly dict."""
        return {
            "name": self.name,
            "healthy": self.healthy,
            "summary": self.summary,
            "findings": self.findings,
            "metrics": self.metrics,
        }


__all__ = ["HealthCheckResult"]

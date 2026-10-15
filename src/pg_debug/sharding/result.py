"""What the shard readiness assessment concluded, with the figures it rests on."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any

from ..severity import Confidence
from ..severity import Severity


class Verdict(str, Enum):
    """The answer to "should we shard?".

    There is no "shard": no authoritative threshold exists, and the figures
    are inputs to a decision the user makes. :attr:`NOT_YET` is the default.
    """

    #: At least one reason not to shard stands, or nothing presses on the
    #: single primary.
    NOT_YET = "not yet"
    #: The write path is under pressure, no refusal stands and a routing key
    #: covers most of the workload: worth evaluating, with the figures below.
    WORTH_EVALUATING = "worth evaluating"


class RefusalReason(str, Enum):
    """Why sharding is not the next step."""

    #: Reads take most of the recorded time: add replicas.
    READ_LOAD = "read_load"
    #: A few statements take most of the recorded time: tune them.
    FEW_QUERIES_DOMINATE = "few_queries_dominate"
    #: Old data is deleted by a time key: partition and drop partitions.
    RETENTION = "retention"
    #: The write path shows no pressure; size alone is no reason.
    HEALTHY_WRITE_RATE = "healthy_write_rate"
    #: No column carries most of the workload's time.
    NO_ROUTING_KEY = "no_routing_key"
    #: There is no recorded workload to judge a routing key or the load by.
    NO_WORKLOAD = "no_workload"


@dataclass
class Refusal:
    """One reason not to shard, and the figure it rests on.

    Attributes:
        reason: Which refusal.
        summary: The refusal in a sentence, with its figure.
        instead: What to do instead.
        changes_if: What figure would lift it.
        figures: The measurements it rests on.
    """

    reason: RefusalReason
    summary: str
    instead: str
    changes_if: str
    figures: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "summary": self.summary,
            "instead": self.instead,
            "changes_if": self.changes_if,
            "figures": self.figures,
        }


@dataclass
class CitusFinding:
    """Something a Citus cluster's own views show about how it is sharded.

    Attributes:
        check: ``shard_size_skew``, ``hot_shard``, ``tenant_cpu_share``,
            ``tenant_tracking_off``, ``multi_shard_calls``,
            ``timestamp_distribution_column``,
            ``low_cardinality_distribution_column``,
            ``unique_key_without_distribution_column`` or
            ``foreign_key_without_distribution_column``.
        severity: How much it matters.
        confidence: ``proven`` when read from the catalog or Citus's counters,
            ``candidate`` when inferred from sampled statistics or from an
            approximation Citus's documentation calls one.
        summary: The finding, with its figure.
        evidence: What it was read from.
        verify: A statement that confirms a candidate, or None.
    """

    check: str
    severity: Severity
    confidence: Confidence
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    verify: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "verify": self.verify,
        }


@dataclass
class ShardReadiness:
    """The verdict, every refusal behind it, and the measurements.

    Attributes:
        verdict: :attr:`Verdict.NOT_YET` unless nothing refuses and the write
            path is under pressure.
        summary: The verdict in a sentence.
        refusals: Each reason not to shard, with its figure.
        pressure: What presses on the single primary, if anything, each with
            its figure.
        would_change: What would change the verdict.
        routing_keys: Candidate routing keys, best first: the column, the
            share of recorded statement time that filters or joins on it by
            equality, and the tables it was seen on.
        measurements: The sampled and cumulative figures: ``interval``
            (seconds), ``wal``, ``io``, ``checkpoints``, ``autovacuum``,
            ``workload``, ``database_bytes``, and the counting windows.
        citus: For a Citus cluster, its findings and what they were read
            from; None where the citus extension is not installed.
        notes: What the assessment could not see, in sentences.
    """

    verdict: Verdict
    summary: str
    refusals: list[Refusal] = field(default_factory=list)
    pressure: list[str] = field(default_factory=list)
    would_change: list[str] = field(default_factory=list)
    routing_keys: list[dict[str, Any]] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    citus: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        citus = None
        if self.citus is not None:
            citus = {**self.citus, "findings": [finding.to_dict() for finding in self.citus.get("findings", [])]}
        return {
            "verdict": self.verdict.value,
            "summary": self.summary,
            "refusals": [refusal.to_dict() for refusal in self.refusals],
            "pressure": self.pressure,
            "would_change": self.would_change,
            "routing_keys": self.routing_keys,
            "measurements": self.measurements,
            "citus": citus,
            "notes": self.notes,
        }


__all__ = ["CitusFinding", "Refusal", "RefusalReason", "ShardReadiness", "Verdict"]

"""The vocabulary a data-model review is expressed in.

A finding has to survive being read by someone who did not run the tool, so it
carries four things beyond its description: how much of it is fact
(:class:`Confidence`), how much it matters (:class:`Severity`), the numbers it
was derived from (``evidence``), and the statements that fix it
(``remediation``). A candidate additionally carries the query that settles it,
because an inference drawn from sampled statistics is not something anyone
should apply DDL to on trust.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple

from ..errors import InvalidArgumentError

#: Ignore tables smaller than this when a rule reasons from sampled statistics.
#: Below it a sample is most of the table, and "no NULLs in 200 rows" says
#: nothing about the column.
DEFAULT_MIN_ROWS = 1000


class Confidence(str, Enum):
    """Whether a finding is a fact or an inference."""

    #: Read straight from the catalog. It cannot be wrong.
    PROVEN = "proven"
    #: Inferred from sampled planner statistics. Confirm before acting; every
    #: candidate carries a ``verify`` query that does exactly that.
    CANDIDATE = "candidate"


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


class RuleId(str, Enum):
    """The checks a review can run."""

    UNINDEXED_FOREIGN_KEY = "unindexed_foreign_key"
    FOREIGN_KEY_TYPE_MISMATCH = "foreign_key_type_mismatch"
    MISSING_PRIMARY_KEY = "missing_primary_key"
    COLUMN_ORDER_PADDING = "column_order_padding"
    NAIVE_TIMESTAMP = "naive_timestamp"
    BLANK_PADDED_CHAR = "blank_padded_char"
    NULLABLE_NEVER_NULL = "nullable_never_null"
    LOW_CARDINALITY_TEXT = "low_cardinality_text"
    ALL = "all"

    @classmethod
    def parse(cls, rule: str | RuleId | Iterable[str | RuleId]) -> Set[RuleId]:
        """Expand a rule selector into the set of rules to run.

        Accepts a single value, a comma-separated string, or any iterable of
        values. ``all`` expands to every individual rule.
        """
        if isinstance(rule, (str, RuleId)):
            values = str(rule.value if isinstance(rule, RuleId) else rule).split(",")
        else:
            values = [str(v.value if isinstance(v, RuleId) else v) for v in rule]

        try:
            rules = {RuleId(v.strip()) for v in values if v.strip()}
        except ValueError as e:
            valid = ", ".join(sorted(r.value for r in RuleId))
            raise InvalidArgumentError(f"Invalid model rules provided: '{rule}'. Valid values are: {valid}.") from e

        if not rules:
            raise InvalidArgumentError("No model rules selected.")

        if RuleId.ALL in rules:
            return {r for r in RuleId if r is not RuleId.ALL}
        return rules


@dataclass
class ModelFinding:
    """One thing worth changing about the data model.

    Attributes:
        rule: The :class:`RuleId` that produced it.
        confidence: Whether it is read from the catalog or inferred.
        severity: How much it matters, after the rule has weighed its evidence.
        schema: Schema of the table the finding is about.
        table: The table itself.
        columns: The columns involved, in the order that matters to the rule.
        summary: One sentence, enough on its own.
        detail: Why it matters here, in terms of this table's numbers.
        remediation: SQL statements that fix it, ready to run.
        evidence: The measurements behind the verdict.
        verify: For a candidate, the query that confirms it. ``None`` for a
            proven finding, which needs no confirming.
        impact_bytes: Bytes the fix saves, where the rule can measure that.
            ``None`` where the cost is not a size — an unindexed foreign key
            costs scan time and lock duration, not space.
        rank: How this finding orders against others *from the same rule*. Not
            comparable across rules and not serialized: a byte count and a lock
            risk have no common scale, and inventing one would be false
            precision.
    """

    rule: RuleId
    confidence: Confidence
    severity: Severity
    schema: str
    table: str
    columns: List[str]
    summary: str
    detail: str
    remediation: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)
    verify: Optional[str] = None
    impact_bytes: Optional[int] = None
    rank: float = 0.0

    @property
    def qualified_table(self) -> str:
        """``schema.table``, as it would be written in a statement."""
        return f"{self.schema}.{self.table}"

    @property
    def sort_key(self) -> Tuple[int, float, str, str, str]:
        """Severity first, then the rule's own measure of impact."""
        return (_SEVERITY_ORDER[self.severity], -self.rank, self.rule.value, self.schema, self.table)

    def to_dict(self) -> Dict[str, Any]:
        """Return the finding as a JSON-friendly dict."""
        result: Dict[str, Any] = {
            "rule": self.rule.value,
            "confidence": self.confidence.value,
            "severity": self.severity.value,
            "schema": self.schema,
            "table": self.table,
            "columns": self.columns,
            "summary": self.summary,
            "detail": self.detail,
            "remediation": self.remediation,
            "evidence": self.evidence,
        }
        if self.verify is not None:
            result["verify"] = self.verify
        if self.impact_bytes is not None:
            result["impact_bytes"] = self.impact_bytes
        return result


@dataclass(frozen=True)
class ReviewOptions:
    """What to look at. Passed to every rule, whether or not it cares.

    Attributes:
        schema: Restrict the review to one schema. ``None`` reviews every user
            schema.
        min_rows: Rules that reason from sampled statistics ignore tables
            smaller than this.
    """

    schema: Optional[str] = None
    min_rows: int = DEFAULT_MIN_ROWS


__all__ = [
    "DEFAULT_MIN_ROWS",
    "Confidence",
    "ModelFinding",
    "ReviewOptions",
    "RuleId",
    "Severity",
]

"""What the index-use analysis says, and the words it says it in.

One :class:`IndexUse` per index on a table the query reads: either the plan
uses it, or it does not and the findings say why. A reason is either read
straight from the catalog or the plan — the index is invalid, the query names
no column of it, the planner cast the column before comparing it — or
inferred by comparing the query's text against the index's, which is where
two expressions that mean the same thing can look different. Each finding
says which.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any

from ..severity import Confidence


class IndexUseReason(str, Enum):
    """Why an index was not used for a query."""

    #: ``indisvalid`` or ``indisready`` is false: a CREATE INDEX CONCURRENTLY
    #: that failed, or one still being built. The planner never considers it.
    INVALID = "invalid"
    #: The query restricts no column of the index.
    NO_PREDICATE = "no_predicate"
    #: The query restricts a column of the index, but not its first one, and
    #: a btree can only be entered through its first column.
    NOT_LEADING_COLUMN = "not_leading_column"
    #: The query wraps the column in a function, a cast or arithmetic that the
    #: index is not on — or the index is on an expression the query does not
    #: use. ``lower(email)`` and ``email`` are different keys.
    EXPRESSION_MISMATCH = "expression_mismatch"
    #: The operator cannot be answered by walking this kind of index: ``<>``,
    #: ``NOT IN``, a leading-wildcard ``LIKE``, ``ILIKE``, a regular expression.
    OPERATOR_NOT_INDEXABLE = "operator_not_indexable"
    #: ``LIKE 'prefix%'`` on a btree needs the C collation or a pattern
    #: operator class, and this index has neither.
    PATTERN_NEEDS_OPCLASS = "pattern_needs_opclass"
    #: The planner had to cast the column to compare it with the value, and
    #: the index is on the uncast column.
    TYPE_MISMATCH = "type_mismatch"
    #: The index covers only rows matching its WHERE clause, and the query does
    #: not restrict those columns, so the planner cannot prove the rows it
    #: wants are in the index.
    PARTIAL_PREDICATE_NOT_MATCHED = "partial_predicate_not_matched"
    #: The restriction sits inside an OR with an arm on another table, which
    #: no single-table index can serve.
    OR_ACROSS_TABLES = "or_across_tables"
    #: Nothing stops the planner using it; it judged another plan cheaper.
    #: Forcing it shows by how much, and the row estimate says why.
    PLANNER_PREFERRED_OTHER = "planner_preferred_other"
    #: The table's statistics no longer describe it, so the estimate the
    #: choice rests on may be wrong.
    STALE_STATISTICS = "stale_statistics"
    #: With sequential scans disabled the planner still did not use it, and
    #: none of the checks above explains why.
    UNEXPLAINED = "unexplained"


@dataclass
class IndexUseFinding:
    """One reason an index was not used.

    Attributes:
        reason: Which :class:`IndexUseReason`.
        confidence: :attr:`Confidence.PROVEN` for a fact read from the catalog
            or the plan; :attr:`Confidence.CANDIDATE` for a comparison of the
            query's text with the index's.
        summary: One sentence, enough on its own.
        detail: Why, in this query's and this index's terms.
        evidence: What the verdict rests on.
        remediation: Statements or rewrites that address it. Empty where the
            answer is a decision rather than a change.
    """

    reason: IndexUseReason
    confidence: Confidence
    summary: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the finding as a JSON-friendly dict."""
        return {
            "reason": self.reason.value,
            "confidence": self.confidence.value,
            "summary": self.summary,
            "detail": self.detail,
            "evidence": self.evidence,
            "remediation": self.remediation,
        }


@dataclass
class IndexUse:
    """Whether one index serves one query, and if not, why not.

    Attributes:
        index: The index name.
        table: The table it is on, schema-qualified.
        definition: The CREATE INDEX statement, as the catalog renders it.
        method: The access method — ``btree``, ``gin``, ...
        columns: Each index column as the catalog renders it: a column name,
            or an expression such as ``lower(email)``. Key columns first.
        key_columns: How many of ``columns`` are keys; the rest are INCLUDE.
        predicate: The WHERE clause of a partial index, else ``None``.
        valid: Whether the planner may use it at all.
        size_bytes: What it occupies.
        used: Whether the query's plan reads it.
        node_type: The plan node that reads it when ``used``, such as
            ``Index Scan`` or ``Bitmap Index Scan``.
        findings: Why it is not used. Empty when it is.
        notes: What could not be checked and why, such as the forced plan in
            restricted mode.
    """

    index: str
    table: str
    definition: str
    method: str
    columns: list[str]
    key_columns: int
    predicate: str | None
    valid: bool
    size_bytes: int
    used: bool
    node_type: str | None = None
    findings: list[IndexUseFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def description(self) -> str:
        """``btree (status, created_at) WHERE ...`` — the index in one line."""
        keys = ", ".join(self.columns[: self.key_columns])
        included = self.columns[self.key_columns :]
        text = f"{self.method} ({keys})"
        if included:
            text += f" INCLUDE ({', '.join(included)})"
        if self.predicate:
            text += f" WHERE {self.predicate}"
        return text

    def to_dict(self) -> dict[str, Any]:
        """Return the verdict as a JSON-friendly dict."""
        return {
            "index": self.index,
            "table": self.table,
            "definition": self.definition,
            "method": self.method,
            "columns": self.columns,
            "key_columns": self.key_columns,
            "predicate": self.predicate,
            "valid": self.valid,
            "size_bytes": self.size_bytes,
            "used": self.used,
            "node_type": self.node_type,
            "findings": [finding.to_dict() for finding in self.findings],
            "notes": self.notes,
        }


__all__ = ["IndexUse", "IndexUseFinding", "IndexUseReason"]

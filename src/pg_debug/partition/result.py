"""What the advisor concluded about one table.

A candidate is produced for every table examined, including the ones the
advisor decided against. That is deliberate: "this table is 400 GB and only 31%
of the query time against it would still prune, so partitioning would make more
of the workload slower than faster" is the answer people need more often than a
recommendation, and it is only available if the negative verdict is carried
rather than filtered out.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any


class Strategy(str, Enum):
    """How a table would be divided.

    The three differ in what they prune on, which is the whole decision:
    :attr:`RANGE` and :attr:`LIST` prune on ``<``/``>`` as well as ``=``,
    :attr:`HASH` only on ``=``. RANGE is additionally the one that makes old
    data droppable, so it wins ties.
    """

    #: Ordered boundaries, usually by time. The only strategy that turns
    #: "delete last year's rows" into DROP TABLE.
    RANGE = "range"
    #: One partition per value of a small, stable set.
    LIST = "list"
    #: Even spread across a fixed modulus. Prunes on equality alone, and
    #: nothing about it makes retention cheaper.
    HASH = "hash"


@dataclass
class PartitionCandidate:
    """The verdict on one table, with the numbers it was reached from.

    Attributes:
        schema: Schema of the table assessed.
        table: The table itself.
        table_bytes: Total size including indexes and TOAST.
        row_count: ``reltuples``, so as of the last ANALYZE.
        column: The best partition key found — the one the largest share of the
            recorded query time restricts on.
        column_type: Its type, as ``format_type`` renders it.
        strategy: The strategy that column could support.
        pruning_share: The share of recorded query time against this table that
            restricts on ``column``, so would still prune after partitioning.
            The number the verdict turns on.
        recommended: Whether ``pruning_share`` cleared the threshold. False is a
            real answer, not an absence of one.
        reasons: Why, in terms of this table's numbers.
        blockers: Unique constraints and primary keys that would have to change
            to include the partition key. Reported whether or not the table is
            recommended, because they are what makes the conversion expensive.
        ddl: The shape of the target table. Empty when not recommended —
            sketching DDL for a change the tool advises against would be read
            as endorsement.
        evidence: The measurements behind the verdict, including the columns
            that came second.
    """

    schema: str
    table: str
    table_bytes: int
    row_count: int
    column: str
    column_type: str
    strategy: Strategy
    pruning_share: float
    recommended: bool
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    ddl: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified_table(self) -> str:
        """``schema.table``, as it would be written in a statement."""
        return f"{self.schema}.{self.table}"

    def to_dict(self) -> dict[str, Any]:
        """Return the candidate as a JSON-friendly dict."""
        return {
            "schema": self.schema,
            "table": self.table,
            "table_bytes": self.table_bytes,
            "row_count": self.row_count,
            "column": self.column,
            "column_type": self.column_type,
            "strategy": self.strategy.value,
            "pruning_share": self.pruning_share,
            "recommended": self.recommended,
            "reasons": self.reasons,
            "blockers": self.blockers,
            "ddl": self.ddl,
            "evidence": self.evidence,
        }


__all__ = ["PartitionCandidate", "Strategy"]

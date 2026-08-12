"""Whether a table is worth partitioning, decided by what the workload does."""

from .advisor import DEFAULT_MIN_PRUNING_SHARE
from .advisor import DEFAULT_MIN_TABLE_BYTES
from .advisor import PartitionAdvisor
from .predicates import Predicates
from .predicates import extract_predicates
from .result import PartitionCandidate
from .result import Strategy

__all__ = [
    "DEFAULT_MIN_PRUNING_SHARE",
    "DEFAULT_MIN_TABLE_BYTES",
    "PartitionAdvisor",
    "PartitionCandidate",
    "Predicates",
    "Strategy",
    "extract_predicates",
]

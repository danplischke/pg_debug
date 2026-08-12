"""Reviewing the data model itself, rather than what the server is doing now.

Every other diagnostic in pg_debug answers "why is this slow" — the health
checks, the top queries, the index advisor. This one answers a different
question: is the schema built the way the data actually behaves. It reads the
catalog for what can be proven and the planner's statistics for what can only
be suggested, and it says which of the two each finding is.
"""

from .result import DEFAULT_MIN_ROWS
from .result import Confidence
from .result import ModelFinding
from .result import ReviewOptions
from .result import RuleId
from .result import Severity
from .reviewer import RULES
from .reviewer import ModelReviewer

__all__ = [
    "DEFAULT_MIN_ROWS",
    "RULES",
    "Confidence",
    "ModelFinding",
    "ModelReviewer",
    "ReviewOptions",
    "RuleId",
    "Severity",
]

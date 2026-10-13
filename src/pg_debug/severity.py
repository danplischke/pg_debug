"""How much a finding matters, and how sure it is, for everything that reports findings."""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: Sort order, most severe first. An enum has no useful ordering of its own.
SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


class Confidence(str, Enum):
    """Whether a finding was read from the server or inferred.

    Carried by the findings that can be either, the data model review's and
    the index-use analysis's; each says what its two levels rest on. A finding
    that is only ever a measurement carries its numbers instead.
    """

    #: Read from something the server recorded: a catalog row, or the plan
    #: the planner chose. It describes the database as it stands.
    PROVEN = "proven"
    #: Inferred, from sampled planner statistics or from comparing a query's
    #: text with an index's. Confirm it before acting on it; a finding that has
    #: a way to confirm it says how.
    CANDIDATE = "candidate"


__all__ = ["SEVERITY_ORDER", "Confidence", "Severity"]

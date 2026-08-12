"""How much a finding matters, shared by everything that reports findings."""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: Sort order, most severe first. An enum has no useful ordering of its own.
SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


__all__ = ["SEVERITY_ORDER", "Severity"]

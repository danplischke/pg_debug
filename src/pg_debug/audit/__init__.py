"""Audits: questions about the cluster that have a right answer, asked of its catalog.

Two of them. ``upgrade-check`` reads what stands between this cluster and a
``pg_upgrade`` to a newer major version, and what the upgrade will leave
undone. ``security`` reads who can get in and what they can do once there.
They are commands of their own rather than health checks because neither is
about how the server is running: one depends on where it is going (``--to``),
and both answer to a different reader, on a different schedule, than the
health checks do.

Each check reports what it examined, and a check that could not read what it
needed (the authentication rules and password hashes are a superuser's to
read) says so instead of passing.
"""

from ..severity import Confidence
from ..severity import Severity
from .result import AuditFinding
from .result import AuditReport
from .result import CheckOutcome
from .result import CheckStatus
from .security import SECURITY
from .security import SecurityAuditor
from .upgrade import REFUSED_REG_TYPES
from .upgrade import UPGRADE_CHECK
from .upgrade import UpgradeChecker
from .versions import KNOWN_AS_OF

__all__ = [
    "KNOWN_AS_OF",
    "REFUSED_REG_TYPES",
    "SECURITY",
    "UPGRADE_CHECK",
    "AuditFinding",
    "AuditReport",
    "CheckOutcome",
    "CheckStatus",
    "Confidence",
    "SecurityAuditor",
    "Severity",
    "UpgradeChecker",
]

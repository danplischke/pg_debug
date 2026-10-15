"""Pinning a plan: which mechanism this server offers, named as advice.

A finding about a choice the planner made — a misestimate that chose the
join, a generic plan, an index walked for a LIMIT — is fixed at its cause:
the statistics, the query, an index. A user may still want to hold a good
plan in place while that fix is made, and how depends on where the server
runs and what it has installed. This names what is there, from the catalog
and the platform, and never applies any of it.

What is read: whether ``pg_hint_plan`` is installed, loaded, or only
available; whether Aurora's ``apg_plan_mgmt`` is; the server's major version,
for PostgreSQL 19's ``pg_plan_advice``; and the platform
:func:`~pg_debug.platform.detect_platform` recognised. One statement, which
restricted mode allows, plus the version every client already asks for.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..db import get_postgres_version
from ..errors import QueryError
from ..platform import Platform
from ..platform import PlatformDetection
from ..platform import detect_platform
from .result import PLAN_CHOICE_ISSUES
from .result import PlanFinding

if TYPE_CHECKING:
    from ..db import SafeSqlDriver
    from ..db import SqlDriver

logger = logging.getLogger(__name__)

#: The first major version with ``pg_plan_advice``.
PLAN_ADVICE_VERSION = 19

PLAN_CONTROL_SQL = """
SELECT
    EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_hint_plan') AS hint_plan_installed,
    EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_hint_plan') AS hint_plan_available,
    current_setting('pg_hint_plan.enable_hint', true) IS NOT NULL AS hint_plan_loaded,
    EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'apg_plan_mgmt') AS apg_plan_mgmt_installed,
    EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'apg_plan_mgmt') AS apg_plan_mgmt_available
"""

#: What a plan diagnosed with no server behind it can say.
OFFLINE_ADVICE = "Which mechanism can pin this plan depends on the server: diagnose the plan with a connection to have it named."


@dataclass(frozen=True)
class PlanControl:
    """What this server offers for pinning a plan.

    Attributes:
        version: The server's major version, or None when it could not be read.
        platform: The managed platform, as detected.
        hint_plan_installed: ``pg_hint_plan`` is in ``pg_extension``.
        hint_plan_available: It is in ``pg_available_extensions``.
        hint_plan_loaded: Its ``pg_hint_plan.enable_hint`` setting exists,
            which it only does once the library is loaded.
        apg_plan_mgmt_installed: Aurora's Query Plan Management is installed.
        apg_plan_mgmt_available: It is available to install.
    """

    version: int | None = None
    platform: PlatformDetection | None = None
    hint_plan_installed: bool = False
    hint_plan_available: bool = False
    hint_plan_loaded: bool = False
    apg_plan_mgmt_installed: bool = False
    apg_plan_mgmt_available: bool = False


def plan_control_advice(control: PlanControl) -> list[str]:
    """The mechanisms to name, one sentence each; one that says there are none when there are none."""
    advice: list[str] = []
    platform = control.platform.platform if control.platform else Platform.UNKNOWN

    if control.hint_plan_installed or control.hint_plan_loaded:
        line = (
            "pg_hint_plan is installed: a comment at the head of the statement, such as /*+ HashJoin(a b) */ or "
            "/*+ IndexScan(t index_name) */, pins a join method, an access path or a join order, and its hint table "
            "pins one by the statement's text without changing the application."
        )
        if not control.hint_plan_loaded:
            line += " It only takes effect once loaded: shared_preload_libraries or session_preload_libraries, or LOAD 'pg_hint_plan'."
        advice.append(line)
    elif control.hint_plan_available:
        advice.append(
            "pg_hint_plan is available here but not installed: CREATE EXTENSION pg_hint_plan, loaded through "
            "shared_preload_libraries or session_preload_libraries"
            + (" in the parameter group" if platform in (Platform.RDS, Platform.AURORA) else "")
            + ", lets a comment such as /*+ HashJoin(a b) */ pin a join method, an access path or a join order."
        )

    if control.version is not None and control.version >= PLAN_ADVICE_VERSION:
        advice.append(
            f"PostgreSQL {control.version} has pg_plan_advice, which records the choices a plan made as advice and holds "
            "later plans of the statement to them. It was in beta as of October 2026: check it is in your release before "
            "relying on it."
        )

    if platform is Platform.AURORA:
        if control.apg_plan_mgmt_installed:
            advice.append(
                "Aurora Query Plan Management (apg_plan_mgmt) is installed: capture this statement's plan, approve the good "
                "one, and Aurora uses the approved plan whatever the planner would choose."
            )
        elif control.apg_plan_mgmt_available:
            advice.append(
                "Aurora Query Plan Management is available: CREATE EXTENSION apg_plan_mgmt, with rds.enable_plan_management "
                "= 1 and apg_plan_mgmt in shared_preload_libraries in the cluster parameter group, captures plans and "
                "enforces an approved one."
            )
        else:
            advice.append("This is Aurora, and Aurora Query Plan Management (apg_plan_mgmt) is not available to this server.")
    elif platform is Platform.ALLOYDB:
        advice.append(
            "AlloyDB documents query plan management of its own, which pg-debug does not detect: see AlloyDB's documentation "
            "for this server's version."
        )

    if not advice:
        version = f"PostgreSQL {control.version}" if control.version is not None else "This server"
        advice.append(
            f"Nothing here pins a plan: pg_hint_plan is neither installed nor available, and {version} has no built-in "
            f"mechanism before {PLAN_ADVICE_VERSION}. Correcting the estimate is the fix that lasts anyway."
        )
    return advice


async def read_plan_control(
    driver: SqlDriver | SafeSqlDriver,
    platform: Callable[[], Awaitable[PlatformDetection]] | None = None,
) -> PlanControl:
    """Read what this server offers for pinning a plan.

    Args:
        driver: Reads as the connecting user; restricted mode allows it.
        platform: How to learn the platform: the client's detection, which
            knows the connection URL. Without it the server alone is asked.
    """
    try:
        detection = await platform() if platform is not None else await detect_platform(driver)
    except QueryError as e:
        logger.info("Could not detect the platform: %s", e)
        detection = None
    try:
        version: int | None = await get_postgres_version(driver)
    except QueryError as e:
        logger.info("Could not read the server version: %s", e)
        version = None
    try:
        rows = await driver.execute_query(PLAN_CONTROL_SQL)
    except QueryError as e:
        logger.info("Could not read which plan-control extensions exist: %s", e)
        rows = None
    cells = rows[0].cells if rows else {}
    return PlanControl(
        version=version,
        platform=detection,
        hint_plan_installed=cells.get("hint_plan_installed") is True,
        hint_plan_available=cells.get("hint_plan_available") is True,
        hint_plan_loaded=cells.get("hint_plan_loaded") is True,
        apg_plan_mgmt_installed=cells.get("apg_plan_mgmt_installed") is True,
        apg_plan_mgmt_available=cells.get("apg_plan_mgmt_available") is True,
    )


def needs_plan_control(findings: Sequence[PlanFinding]) -> bool:
    """Whether any finding is about a choice the planner made."""
    return any(finding.issue in PLAN_CHOICE_ISSUES for finding in findings)


def attach_plan_control(findings: Sequence[PlanFinding], advice: Sequence[str]) -> None:
    """Give each finding about a planner choice the plan-control advice."""
    for finding in findings:
        if finding.issue in PLAN_CHOICE_ISSUES:
            finding.plan_control = list(advice)


__all__ = [
    "OFFLINE_ADVICE",
    "PLAN_ADVICE_VERSION",
    "PLAN_CONTROL_SQL",
    "PlanControl",
    "attach_plan_control",
    "needs_plan_control",
    "plan_control_advice",
    "read_plan_control",
]

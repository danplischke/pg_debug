"""What each replication slot keeps the server from throwing away.

A slot promises its consumer that the WAL it has not yet confirmed will still
be there when it comes back. The promise has no expiry unless one is set: an
inactive slot keeps every WAL segment written since its consumer left, and
with ``max_slot_wal_keep_size = -1``, the default, it keeps them until the
disk holding ``pg_wal`` fills and the server stops. A slot can also hold a
transaction horizon (``xmin``, from a standby's ``hot_standby_feedback``) or
a catalog horizon (``catalog_xmin``, a logical slot's), which keeps vacuum
from freezing past it.

Everything here is read from ``pg_replication_slots`` and the WAL position,
so every finding is proven: the bytes are the distance between the slot's
``restart_lsn`` and where the server's WAL is now. What a slot will retain
tomorrow depends on its consumer, which the server does not know.
"""

from __future__ import annotations

from typing import Any
from typing import LiteralString

import humanize

from ...db import SafeSqlDriver
from ...db import SqlDriver
from ...db import get_postgres_version
from ...severity import Confidence
from ..result import HealthCheckResult

SLOT_RETENTION = "Replication slot retention"

#: ``wal_status`` and ``safe_wal_size`` arrived with ``max_slot_wal_keep_size``.
WAL_STATUS_MIN_VERSION = 13
#: ``inactive_since`` and ``invalidation_reason``.
INACTIVE_SINCE_MIN_VERSION = 17

#: The order a slot's problems are named in, worst first: a slot is reported
#: once, under the worst.
KINDS = ("lost", "unbounded", "unreserved", "inactive", "extended", "horizon")

#: Where the server's WAL is now. On a standby, which writes no WAL of its own,
#: the position it has replayed to: a slot on a standby retains WAL from there.
CURRENT_LSN = "CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() ELSE pg_current_wal_lsn() END"

SETTINGS_SQL = """
SELECT
    pg_is_in_recovery() AS is_replica,
    (SELECT setting::bigint FROM pg_settings WHERE name = 'max_wal_size') AS max_wal_size_mb,
    (SELECT setting::bigint FROM pg_settings WHERE name = 'max_slot_wal_keep_size') AS max_slot_wal_keep_size_mb,
    current_setting('autovacuum_freeze_max_age')::bigint AS autovacuum_freeze_max_age
"""


def slots_sql(version: int) -> LiteralString:
    """The slots, with what each retains, for this server's version."""
    wal_status = "s.wal_status, s.safe_wal_size" if version >= WAL_STATUS_MIN_VERSION else "NULL::text AS wal_status, NULL::bigint AS safe_wal_size"
    since = (
        "s.inactive_since, s.invalidation_reason"
        if version >= INACTIVE_SINCE_MIN_VERSION
        else "NULL::timestamptz AS inactive_since, NULL::text AS invalidation_reason"
    )
    return f"""
SELECT
    s.slot_name,
    s.slot_type,
    s.database,
    s.plugin,
    s.active,
    s.temporary,
    {wal_status},
    {since},
    s.restart_lsn::text AS restart_lsn,
    pg_wal_lsn_diff({CURRENT_LSN}, s.restart_lsn)::bigint AS retained_bytes,
    age(s.xmin)::bigint AS xmin_age,
    age(s.catalog_xmin)::bigint AS catalog_xmin_age
FROM pg_replication_slots s
ORDER BY pg_wal_lsn_diff({CURRENT_LSN}, s.restart_lsn) DESC NULLS LAST, s.slot_name
"""


class SlotRetentionCalc:
    """Reports the WAL and the horizons each replication slot holds."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def slot_retention_check(self) -> HealthCheckResult:
        """Find slots that retain WAL without bound, have lost theirs, or hold a horizon past freezing.

        A slot is reported under the worst of these, as ``kind``:

        - ``lost``: its WAL is gone (``wal_status = 'lost'``); the consumer
          cannot resume from it.
        - ``unbounded``: inactive, with ``max_slot_wal_keep_size = -1`` (or a
          server before 13, which has no bound), so what it retains grows
          until the disk fills.
        - ``unreserved``: past ``max_slot_wal_keep_size``; the next
          checkpoint removes WAL it needs.
        - ``inactive``: no consumer connected, retaining WAL within a bound;
          it will be invalidated once the bound is reached.
        - ``extended``: connected but behind by more than ``max_wal_size``.
        - ``horizon``: its ``xmin`` or ``catalog_xmin`` is older than
          ``autovacuum_freeze_max_age``, so vacuum cannot freeze past it.

        Every other slot is listed in ``metrics["examined"]`` with what it
        retains, so a clean result says what it looked at.
        """
        version = await get_postgres_version(self.sql_driver)
        settings_rows = await SafeSqlDriver.execute_param_query(self.sql_driver, SETTINGS_SQL)
        settings = dict(settings_rows[0].cells) if settings_rows else {}
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, slots_sql(version))
        slots = [dict(row.cells) for row in rows] if rows else []

        keep_mb = settings.get("max_slot_wal_keep_size_mb") if version >= WAL_STATUS_MIN_VERSION else None
        bounded = keep_mb is not None and int(keep_mb) >= 0
        freeze_max_age = int(settings.get("autovacuum_freeze_max_age") or 0)
        metrics: dict[str, Any] = {
            "is_replica": bool(settings.get("is_replica")),
            "wal_position": "pg_last_wal_replay_lsn()" if settings.get("is_replica") else "pg_current_wal_lsn()",
            "max_wal_size_bytes": _mb(settings.get("max_wal_size_mb")),
            "max_slot_wal_keep_size_mb": None if keep_mb is None else int(keep_mb),
            "max_slot_wal_keep_size_bytes": _mb(keep_mb) if bounded else None,
            "retention_bounded": bounded,
            "autovacuum_freeze_max_age": freeze_max_age,
            "slots_examined": len(slots),
        }

        findings: list[dict[str, Any]] = []
        examined: list[dict[str, Any]] = []
        for slot in slots:
            kind = _kind(slot, bounded=bounded, freeze_max_age=freeze_max_age, max_wal_bytes=metrics["max_wal_size_bytes"])
            if kind is None:
                examined.append(
                    {key: slot[key] for key in ("slot_name", "slot_type", "active", "wal_status", "retained_bytes", "xmin_age", "catalog_xmin_age")}
                )
            else:
                findings.append({**slot, "kind": kind, "confidence": Confidence.PROVEN.value})
        metrics["examined"] = examined

        if not slots:
            return HealthCheckResult(name=SLOT_RETENTION, healthy=True, summary="No replication slots.", metrics=metrics)

        lines: list[str] = []
        if findings:
            lines.append("Replication slots holding what the server would otherwise remove:")
            lines += [_describe(finding, metrics) for finding in findings]
        if examined:
            lines.append(_describe_examined(examined, metrics))
        if not bounded and version >= WAL_STATUS_MIN_VERSION:
            lines.append("max_slot_wal_keep_size is -1: no slot is ever invalidated for the WAL it retains.")

        return HealthCheckResult(
            name=SLOT_RETENTION,
            healthy=not findings,
            summary="\n".join(lines),
            findings=findings,
            metrics=metrics,
        )


def _mb(value: Any) -> int | None:
    return int(value) * 1024 * 1024 if value is not None and int(value) >= 0 else None


def _kind(slot: dict[str, Any], *, bounded: bool, freeze_max_age: int, max_wal_bytes: int | None) -> str | None:
    """The worst thing a slot is doing, or None when it is doing nothing worth reporting."""
    status = slot.get("wal_status")
    active = bool(slot.get("active"))
    retained = slot.get("retained_bytes")
    held = max(int(slot.get("xmin_age") or 0), int(slot.get("catalog_xmin_age") or 0))
    if status == "lost":
        return "lost"
    if slot.get("restart_lsn") is None and not held:
        # Created without reserving WAL and holding no horizon: it keeps nothing.
        return None
    if not active and not bounded and retained is not None:
        return "unbounded"
    if status == "unreserved":
        return "unreserved"
    if not active:
        return "inactive"
    # Before 13 there is no wal_status; behind by more than max_wal_size is
    # what "extended" means, so the same comparison stands in for it.
    if status == "extended" or (status is None and max_wal_bytes is not None and retained is not None and int(retained) > max_wal_bytes):
        return "extended"
    if freeze_max_age and held > freeze_max_age:
        return "horizon"
    return None


def _size(value: Any) -> str:
    return "an unknown amount" if value is None else humanize.naturalsize(int(value), binary=True)


def _horizon(slot: dict[str, Any]) -> str:
    parts = []
    if slot.get("xmin_age") is not None:
        parts.append(f"its xmin is {int(slot['xmin_age']):,} transactions old")
    if slot.get("catalog_xmin_age") is not None:
        parts.append(f"its catalog_xmin holds catalog rows {int(slot['catalog_xmin_age']):,} transactions back")
    return "; ".join(parts)


def _describe(finding: dict[str, Any], metrics: dict[str, Any]) -> str:
    name = f"{finding['slot_type']} slot '{finding['slot_name']}'"
    retained = _size(finding.get("retained_bytes"))
    kind = finding["kind"]
    if kind == "lost":
        reason = f" ({finding['invalidation_reason']})" if finding.get("invalidation_reason") else ""
        line = f"- {name} has lost the WAL its consumer needs{reason}; that consumer cannot resume from it and has to be resynchronized"
    elif kind == "unbounded":
        since = f" since {finding['inactive_since']}" if finding.get("inactive_since") else ""
        bound = "PostgreSQL before 13 cannot bound it" if metrics.get("max_slot_wal_keep_size_mb") is None else "max_slot_wal_keep_size is -1"
        line = f"- {name} is inactive{since} and retains {retained} of WAL; {bound}, so it keeps retaining until the disk holding pg_wal fills"
    elif kind == "unreserved":
        line = f"- {name} retains {retained}, past max_slot_wal_keep_size; the next checkpoint removes WAL it needs and it will be lost"
    elif kind == "inactive":
        safe = f", {_size(finding['safe_wal_size'])} before it is invalidated" if finding.get("safe_wal_size") is not None else ""
        line = f"- {name} is inactive and retains {retained} of WAL{safe}"
    elif kind == "extended":
        limit = _size(metrics.get("max_wal_size_bytes"))
        line = f"- {name} is connected but retains {retained}, more than max_wal_size ({limit}): its consumer is behind"
    else:
        limit = f"{metrics['autovacuum_freeze_max_age']:,}"
        line = f"- {name}: {_horizon(finding)}, older than autovacuum_freeze_max_age ({limit}), so vacuum cannot freeze past it"
    horizon = _horizon(finding) if kind != "horizon" else ""
    return line + (f" ({horizon})" if horizon else "") + "."


def _describe_examined(examined: list[dict[str, Any]], metrics: dict[str, Any]) -> str:
    def one(slot: dict[str, Any]) -> str:
        if slot.get("retained_bytes") is None:
            return f"'{slot['slot_name']}' (reserves no WAL)"
        return f"'{slot['slot_name']}' (connected, retains {_size(slot['retained_bytes'])})"

    bound = _size(metrics.get("max_wal_size_bytes"))
    return f"Examined and within max_wal_size ({bound}): {', '.join(one(slot) for slot in examined)}."


__all__ = ["SLOT_RETENTION", "SlotRetentionCalc", "slots_sql"]

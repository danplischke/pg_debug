from dataclasses import asdict
from dataclasses import dataclass

from ...db import SqlDriver
from ..result import HealthCheckResult

REPLICATION_HEALTH = "Replication health"

#: The application_name of the connection a Neon compute's WAL proposer keeps
#: to its safekeepers. It is in pg_stat_replication, and it is not a replica.
NEON_WALPROPOSER = "walproposer"


@dataclass
class ReplicationSlot:
    slot_name: str
    database: str
    active: bool


@dataclass
class ReplicationMetrics:
    """What the check read.

    Attributes:
        is_replica: Whether this server is in recovery.
        replication_lag_seconds: On a replica, how far replay trails; None
            when it could not be read, and why is in ``lag_error``.
        is_replicating: On a replica, whether its WAL receiver is streaming
            from the primary; on a primary, whether any standby streams from
            it. None when it could not be told: a replica's receiver is
            hidden from a role without ``pg_read_all_stats``.
        replication_slots: The slots on this server.
        lag_error: Why the lag could not be read, when it could not.
        receiver: On a replica, its WAL receiver's status, primary host and
            the time of the last report it received, as far as visible.
    """

    is_replica: bool
    replication_lag_seconds: float | None
    is_replicating: bool | None
    replication_slots: list[ReplicationSlot]
    lag_error: str | None = None
    receiver: dict[str, str | None] | None = None


class ReplicationCalc:
    def __init__(self, sql_driver: SqlDriver):
        self.sql_driver = sql_driver
        self._server_version: int | None = None
        self._feature_support: dict[str, bool] = {}

    async def replication_health_check(self) -> HealthCheckResult:
        """Check replication health including lag and slots.

        A replica that is not receiving from its primary is the only condition
        reported as unhealthy; lag and inactive slots are reported as-is for
        the caller to judge against its own expectations.

        Whether a replica receives is read from its own WAL receiver. Its
        ``pg_stat_replication`` lists only the standbys that stream from it,
        so a replica with none of its own, which is most of them, would read
        as not replicating. What cannot be read is said, not left out.
        """
        metrics = await self._get_replication_metrics()
        result = []
        healthy = True

        if metrics.is_replica:
            result.append("This is a replica database.")
            receiver = metrics.receiver or {}
            if metrics.is_replicating is None:
                result.append(
                    "Whether it is receiving from its primary could not be read: pg_stat_wal_receiver is hidden from "
                    "a role without pg_read_all_stats, which pg_monitor includes."
                )
            elif not metrics.is_replicating:
                healthy = False
                status = receiver.get("status")
                why = (
                    f"its WAL receiver is {status}"
                    if status
                    else "it has no WAL receiver, so it is not streaming (it may be restoring from an archive)"
                )
                result.append(f"WARNING: Replica is not actively replicating from primary: {why}.")
            else:
                source = f" {receiver['sender_host']}" if receiver.get("sender_host") else ""
                result.append(f"Replica is actively replicating from primary{source}.")

            # Check replication lag
            if metrics.replication_lag_seconds is not None:
                if metrics.replication_lag_seconds == 0:
                    result.append("No replication lag detected.")
                else:
                    result.append(f"Replication lag: {metrics.replication_lag_seconds:.1f} seconds")
            elif metrics.lag_error:
                result.append(f"Replication lag could not be read: {metrics.lag_error}")
        else:
            result.append("This is a primary database.")
            if metrics.is_replicating:
                result.append("Has active replicas connected.")
            else:
                result.append("No active replicas connected.")

        # Check replication slots for both primary and replica
        if metrics.replication_slots:
            active_slots = [s for s in metrics.replication_slots if s.active]
            inactive_slots = [s for s in metrics.replication_slots if not s.active]

            if active_slots:
                result.append("\nActive replication slots:")
                for slot in active_slots:
                    result.append(f"- {slot.slot_name} (database: {slot.database})")

            if inactive_slots:
                result.append("\nInactive replication slots:")
                for slot in inactive_slots:
                    result.append(f"- {slot.slot_name} (database: {slot.database})")
        else:
            result.append("\nNo replication slots found.")

        return HealthCheckResult(
            name=REPLICATION_HEALTH,
            healthy=healthy,
            summary="\n".join(result),
            findings=[asdict(slot) for slot in metrics.replication_slots],
            metrics={
                "is_replica": metrics.is_replica,
                "is_replicating": metrics.is_replicating,
                "replication_lag_seconds": metrics.replication_lag_seconds,
                **({"lag_error": metrics.lag_error} if metrics.lag_error else {}),
                **({"receiver": metrics.receiver} if metrics.receiver is not None else {}),
            },
        )

    async def _get_replication_metrics(self) -> ReplicationMetrics:
        """Get comprehensive replication metrics."""
        is_replica = await self._is_replica()
        lag, lag_error = await self._get_replication_lag()
        receiver = await self._get_wal_receiver() if is_replica else None
        if is_replica:
            is_replicating = _receiving(receiver)
        else:
            is_replicating = await self._has_streaming_standbys()
        return ReplicationMetrics(
            is_replica=is_replica,
            replication_lag_seconds=lag,
            is_replicating=is_replicating,
            replication_slots=await self._get_replication_slots(),
            lag_error=lag_error,
            receiver=receiver,
        )

    async def _is_replica(self) -> bool:
        """Check if this database is a replica."""
        result = await self.sql_driver.execute_query("SELECT pg_is_in_recovery()")
        result_list = [dict(x.cells) for x in result] if result is not None else []
        return bool(result_list[0]["pg_is_in_recovery"]) if result_list else False

    async def _get_replication_lag(self) -> tuple[float | None, str | None]:
        """Replication lag in seconds, or why it could not be read."""
        if not self._feature_supported("replication_lag"):
            return None, None

        # Use appropriate functions based on PostgreSQL version
        if await self._get_server_version() >= 100000:
            lag_condition = "pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn()"
        else:
            lag_condition = "pg_last_xlog_receive_location() = pg_last_xlog_replay_location()"

        try:
            result = await self.sql_driver.execute_query(f"""
                SELECT
                    CASE
                        WHEN NOT pg_is_in_recovery() OR {lag_condition} THEN 0
                        ELSE EXTRACT (EPOCH FROM NOW() - pg_last_xact_replay_timestamp())
                    END
                AS replication_lag
            """)
            result_list = [dict(x.cells) for x in result] if result is not None else []
            return (float(result_list[0]["replication_lag"]) if result_list else None), None
        except Exception as e:
            self._feature_support["replication_lag"] = False
            return None, str(e)

    async def _get_replication_slots(self) -> list[ReplicationSlot]:
        """Get information about replication slots."""
        if await self._get_server_version() < 90400 or not self._feature_supported("replication_slots"):
            return []

        try:
            result = await self.sql_driver.execute_query("""
                SELECT
                    slot_name,
                    database,
                    active
                FROM pg_replication_slots
            """)
            if result is None:
                return []
            result_list = [dict(x.cells) for x in result]
            return [
                ReplicationSlot(
                    slot_name=row["slot_name"],
                    database=row["database"],
                    active=row["active"],
                )
                for row in result_list
            ]
        except Exception:
            self._feature_support["replication_slots"] = False
            return []

    async def _has_streaming_standbys(self) -> bool:
        """Whether any standby streams from this server.

        Neon's WAL proposer keeps a connection of its own in
        pg_stat_replication; it is the compute's link to its safekeepers, not
        a replica, and is not counted.
        """
        if not self._feature_supported("replicating"):
            return False

        try:
            result = await self.sql_driver.execute_query(
                f"SELECT state FROM pg_stat_replication WHERE application_name IS DISTINCT FROM '{NEON_WALPROPOSER}'"
            )
            result_list = [dict(x.cells) for x in result] if result is not None else []
            return bool(result_list and len(result_list) > 0)
        except Exception:
            self._feature_support["replicating"] = False
            return False

    async def _get_wal_receiver(self) -> dict[str, str | None]:
        """A replica's WAL receiver: an empty dict when it has none."""
        result = await self.sql_driver.execute_query("SELECT status, sender_host, latest_end_time::text AS latest_end_time FROM pg_stat_wal_receiver")
        rows = [dict(x.cells) for x in result] if result else []
        return {key: (None if value is None else str(value)) for key, value in rows[0].items()} if rows else {}

    async def _get_server_version(self) -> int:
        """Get PostgreSQL server version as a number (e.g. 100000 for version 10.0)."""
        if self._server_version is None:
            result = await self.sql_driver.execute_query("SHOW server_version_num")
            result_list = [dict(x.cells) for x in result] if result is not None else []
            self._server_version = int(result_list[0]["server_version_num"]) if result_list else 0
        return self._server_version

    def _feature_supported(self, feature: str) -> bool:
        """Check if a feature is supported and cache the result."""
        return self._feature_support.get(feature, True)


def _receiving(receiver: dict[str, str | None] | None) -> bool | None:
    """Whether a replica's WAL receiver is streaming, or None when its row is hidden.

    No row means no receiver: the replica is not streaming. A row whose status
    is NULL is one the role may not read; without pg_read_all_stats every
    column but the pid reads NULL.
    """
    if not receiver:
        return False
    if receiver.get("status") is None:
        return None
    return receiver["status"] == "streaming"

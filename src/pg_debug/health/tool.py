from __future__ import annotations

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterable
from enum import Enum

from ..db import obfuscate_password
from ..errors import ConnectionFailedError
from ..errors import InvalidArgumentError
from ..errors import NotConnectedError
from ..errors import PgDebugError
from ..rules import is_static_check
from .checks.autovacuum import AUTOVACUUM_BACKLOG
from .checks.autovacuum import VACUUM_HORIZON
from .checks.autovacuum import AutovacuumCalc
from .checks.buffer import INDEX_HIT_RATE
from .checks.buffer import TABLE_HIT_RATE
from .checks.buffer import BufferHealthCalc
from .checks.checksum import CHECKSUM_HEALTH
from .checks.checksum import ChecksumHealthCalc
from .checks.collation import COLLATION_HEALTH
from .checks.collation import CollationHealthCalc
from .checks.connection import CONNECTION_HEALTH
from .checks.connection import ConnectionHealthCalc
from .checks.constraint import CONSTRAINT_HEALTH
from .checks.constraint import ConstraintHealthCalc
from .checks.index import DUPLICATE_INDEXES
from .checks.index import INDEX_BLOAT
from .checks.index import INVALID_INDEXES
from .checks.index import MIN_WINDOW_DAYS
from .checks.index import UNUSED_INDEXES
from .checks.index import IndexHealthCalc
from .checks.multixact import MULTIXACT_HEALTH
from .checks.multixact import MultixactCalc
from .checks.partition import PARTITION_HEALTH
from .checks.partition import PartitionHealthCalc
from .checks.replication import REPLICATION_HEALTH
from .checks.replication import ReplicationCalc
from .checks.seq_scan import SEQ_SCAN_HEALTH
from .checks.seq_scan import SeqScanHealthCalc
from .checks.sequence import SEQUENCE_HEALTH
from .checks.sequence import SequenceHealthCalc
from .checks.slots import SLOT_RETENTION
from .checks.slots import SlotRetentionCalc
from .checks.statistics import STATISTICS_HEALTH
from .checks.statistics import StatisticsHealthCalc
from .checks.table_bloat import TABLE_BLOAT
from .checks.table_bloat import TableBloatCalc
from .checks.vacuum import VACUUM_HEALTH
from .checks.vacuum import VacuumHealthCalc
from .result import HealthCheckResult

logger = logging.getLogger(__name__)


class HealthType(str, Enum):
    INDEX = "index"
    CONNECTION = "connection"
    VACUUM = "vacuum"
    SEQUENCE = "sequence"
    REPLICATION = "replication"
    BUFFER = "buffer"
    CONSTRAINT = "constraint"
    STATISTICS = "statistics"
    TABLE = "table"
    SCAN = "scan"
    COLLATION = "collation"
    CHECKSUM = "checksum"
    PARTITION = "partition"
    ALL = "all"

    @classmethod
    def parse(cls, health_type: str | HealthType | Iterable[str | HealthType]) -> set[HealthType]:
        """Expand a health type selector into the set of checks to run.

        Accepts a single value, a comma-separated string, or any iterable of
        values. ``all`` expands to every individual check.
        """
        if isinstance(health_type, (str, HealthType)):
            values = str(health_type.value if isinstance(health_type, HealthType) else health_type).split(",")
        else:
            values = [str(v.value if isinstance(v, HealthType) else v) for v in health_type]

        try:
            types_ = {HealthType(v.strip()) for v in values if v.strip()}
        except ValueError as e:
            valid = ", ".join(sorted(t.value for t in HealthType))
            raise InvalidArgumentError(f"Invalid health types provided: '{health_type}'. Valid values are: {valid}.") from e

        if not types_:
            raise InvalidArgumentError("No health checks selected.")

        if HealthType.ALL in types_:
            return {t for t in HealthType if t is not HealthType.ALL}
        return types_


class DatabaseHealthTool:
    """Tool for analyzing database health metrics."""

    def __init__(self, sql_driver, min_window_days: float = MIN_WINDOW_DAYS):
        self.sql_driver = sql_driver
        self.min_window_days = min_window_days

    async def run_checks(
        self,
        health_type: str | HealthType | Iterable[str | HealthType] = HealthType.ALL,
        static: bool = False,
    ) -> dict[str, HealthCheckResult]:
        """Run database health checks and return one result per check.

        Each check runs on its own. One that raises, because the server
        refused a statement, a statement timeout cancelled it behind a lock,
        or restricted mode declined it, is returned as a result that says it
        could not run and why (``error`` set, ``healthy`` False), and the
        checks after it still run: a lock on one table should not cost the
        report on every other check. Cancellation is not caught.

        A check that cannot reach the database at all is the exception. The
        checks still to come would each wait out the connection timeout to
        fail the same way, so they are reported as not attempted instead.

        Args:
            health_type: A single health type, a comma-separated string, or an
                iterable of health types. Valid values: index, connection,
                vacuum, sequence, replication, buffer, constraint, statistics,
                table, scan, collation, checksum, partition, all.
            static: Run only the checks that read nothing but the catalog
                (invalid and duplicate indexes, invalid constraints), which
                find on a freshly migrated, empty database what they would
                find on production. The others read what the tables hold or
                the counters a workload leaves, and an empty database has
                neither: they would pass there for want of anything to see.

        Returns:
            An ordered mapping of check name to its
            :class:`~pg_debug.health.result.HealthCheckResult`, which
            carries a ``healthy`` flag, a text ``summary``, the flagged
            ``findings``, the ``metrics`` behind the verdict, and an
            ``error`` when the check could not run.

        Raises:
            InvalidArgumentError: If an unknown health type is requested, or
                ``static`` leaves none of the selected checks to run.
        """
        results: dict[str, HealthCheckResult] = {}
        unreachable: str | None = None

        selected = self._selected_checks(HealthType.parse(health_type))
        if static:
            runtime = [name for name, _ in selected if not is_static_check(name)]
            selected = [(name, check) for name, check in selected if is_static_check(name)]
            if not selected:
                raise InvalidArgumentError(
                    f"None of the selected health checks reads only the catalog ({', '.join(runtime)} read the tables' contents or the "
                    "workload's counters), so --static leaves nothing to run. The static checks are in index and constraint."
                )

        for name, check in selected:
            if unreachable is not None:
                results[name] = HealthCheckResult.could_not_run(name, unreachable)
                continue
            try:
                result = await check()
            except Exception as e:
                logger.debug("Health check %r could not run", name, exc_info=True)
                reason = _reason(e)
                results[name] = HealthCheckResult.could_not_run(name, reason)
                if isinstance(e, (ConnectionFailedError, NotConnectedError)):
                    unreachable = f"not attempted, since the database could not be reached ({reason})"
            else:
                results[result.name] = result

        return results

    def _selected_checks(self, health_types: set[HealthType]) -> list[tuple[str, Callable[[], Awaitable[HealthCheckResult]]]]:
        """Each selected check's name, with the call that runs it, in report order.

        The name is known before the check runs, so that a check that raises
        can still be reported under it.
        """
        checks: list[tuple[str, Callable[[], Awaitable[HealthCheckResult]]]] = []

        if HealthType.INDEX in health_types:
            index_health = IndexHealthCalc(self.sql_driver, min_window_days=self.min_window_days)
            checks += [
                (INVALID_INDEXES, index_health.invalid_index_check),
                (DUPLICATE_INDEXES, index_health.duplicate_index_check),
                (INDEX_BLOAT, index_health.index_bloat),
                (UNUSED_INDEXES, index_health.unused_indexes),
            ]

        if HealthType.CONNECTION in health_types:
            checks.append((CONNECTION_HEALTH, ConnectionHealthCalc(self.sql_driver).connection_health_check))

        if HealthType.VACUUM in health_types:
            autovacuum = AutovacuumCalc(self.sql_driver)
            checks += [
                (VACUUM_HEALTH, VacuumHealthCalc(self.sql_driver).transaction_id_danger_check),
                (AUTOVACUUM_BACKLOG, autovacuum.autovacuum_backlog_check),
                (VACUUM_HORIZON, autovacuum.vacuum_horizon_check),
                (MULTIXACT_HEALTH, MultixactCalc(self.sql_driver).multixact_check),
            ]

        if HealthType.SEQUENCE in health_types:
            checks.append((SEQUENCE_HEALTH, SequenceHealthCalc(self.sql_driver).sequence_danger_check))

        if HealthType.REPLICATION in health_types:
            checks.append((REPLICATION_HEALTH, ReplicationCalc(self.sql_driver).replication_health_check))
            checks.append((SLOT_RETENTION, SlotRetentionCalc(self.sql_driver).slot_retention_check))

        if HealthType.BUFFER in health_types:
            buffer_health = BufferHealthCalc(self.sql_driver)
            checks += [
                (INDEX_HIT_RATE, buffer_health.index_hit_rate),
                (TABLE_HIT_RATE, buffer_health.table_hit_rate),
            ]

        if HealthType.CONSTRAINT in health_types:
            checks.append((CONSTRAINT_HEALTH, ConstraintHealthCalc(self.sql_driver).invalid_constraints_check))

        if HealthType.STATISTICS in health_types:
            checks.append((STATISTICS_HEALTH, StatisticsHealthCalc(self.sql_driver).stale_statistics_check))

        if HealthType.TABLE in health_types:
            checks.append((TABLE_BLOAT, TableBloatCalc(self.sql_driver).table_bloat_check))

        if HealthType.SCAN in health_types:
            checks.append((SEQ_SCAN_HEALTH, SeqScanHealthCalc(self.sql_driver).sequential_scan_check))

        if HealthType.COLLATION in health_types:
            checks.append((COLLATION_HEALTH, CollationHealthCalc(self.sql_driver).collation_version_check))

        if HealthType.CHECKSUM in health_types:
            checks.append((CHECKSUM_HEALTH, ChecksumHealthCalc(self.sql_driver).checksum_failure_check))
        if HealthType.PARTITION in health_types:
            checks.append((PARTITION_HEALTH, PartitionHealthCalc(self.sql_driver).partition_health_check))

        return checks

    async def health(
        self,
        health_type: str | HealthType | Iterable[str | HealthType] = HealthType.ALL,
    ) -> str:
        """Run health checks and render them as a plain-text report.

        See :meth:`run_checks` for the structured equivalent.
        """
        results = await self.run_checks(health_type)
        if not results:
            return "No health checks were performed."
        return "".join(f"{name}: {result.summary}\n" for name, result in results.items())


def _reason(error: Exception) -> str:
    """Why a check could not run, for its result.

    The library's own errors are written to be read as they are. Anything
    else carries its type too, since a bug's message alone can be as little
    as a key name. Passwords are masked, as the CLI masks them in any error it
    prints, because this message is printed too.
    """
    text = str(error) if isinstance(error, PgDebugError) else f"{type(error).__name__}: {error}"
    return obfuscate_password(text) or type(error).__name__

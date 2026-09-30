from __future__ import annotations

import logging
from enum import Enum
from typing import Dict
from typing import Iterable
from typing import List
from typing import Set

from ..errors import InvalidArgumentError
from .checks.autovacuum import AutovacuumCalc
from .checks.buffer import BufferHealthCalc
from .checks.connection import ConnectionHealthCalc
from .checks.constraint import ConstraintHealthCalc
from .checks.index import IndexHealthCalc
from .checks.replication import ReplicationCalc
from .checks.sequence import SequenceHealthCalc
from .checks.statistics import StatisticsHealthCalc
from .checks.table_bloat import TableBloatCalc
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
    ALL = "all"

    @classmethod
    def parse(cls, health_type: str | HealthType | Iterable[str | HealthType]) -> Set[HealthType]:
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

    def __init__(self, sql_driver):
        self.sql_driver = sql_driver

    async def run_checks(
        self,
        health_type: str | HealthType | Iterable[str | HealthType] = HealthType.ALL,
    ) -> Dict[str, HealthCheckResult]:
        """Run database health checks and return one result per check.

        Args:
            health_type: A single health type, a comma-separated string, or an
                iterable of health types. Valid values: index, connection,
                vacuum, sequence, replication, buffer, constraint, statistics,
                table, all.

        Returns:
            An ordered mapping of check name to its
            :class:`~pg_debug.health.result.HealthCheckResult`, which
            carries a ``healthy`` flag, a text ``summary``, the flagged
            ``findings`` and the ``metrics`` behind the verdict.

        Raises:
            InvalidArgumentError: If an unknown health type is requested.
        """
        health_types = HealthType.parse(health_type)
        results: List[HealthCheckResult] = []

        if HealthType.INDEX in health_types:
            index_health = IndexHealthCalc(self.sql_driver)
            results.append(await index_health.invalid_index_check())
            results.append(await index_health.duplicate_index_check())
            results.append(await index_health.index_bloat())
            results.append(await index_health.unused_indexes())

        if HealthType.CONNECTION in health_types:
            results.append(await ConnectionHealthCalc(self.sql_driver).connection_health_check())

        if HealthType.VACUUM in health_types:
            results.append(await VacuumHealthCalc(self.sql_driver).transaction_id_danger_check())
            autovacuum = AutovacuumCalc(self.sql_driver)
            results.append(await autovacuum.autovacuum_backlog_check())
            results.append(await autovacuum.vacuum_horizon_check())

        if HealthType.SEQUENCE in health_types:
            results.append(await SequenceHealthCalc(self.sql_driver).sequence_danger_check())

        if HealthType.REPLICATION in health_types:
            results.append(await ReplicationCalc(self.sql_driver).replication_health_check())

        if HealthType.BUFFER in health_types:
            buffer_health = BufferHealthCalc(self.sql_driver)
            results.append(await buffer_health.index_hit_rate())
            results.append(await buffer_health.table_hit_rate())

        if HealthType.CONSTRAINT in health_types:
            results.append(await ConstraintHealthCalc(self.sql_driver).invalid_constraints_check())

        if HealthType.STATISTICS in health_types:
            results.append(await StatisticsHealthCalc(self.sql_driver).stale_statistics_check())

        if HealthType.TABLE in health_types:
            results.append(await TableBloatCalc(self.sql_driver).table_bloat_check())

        return {result.name: result for result in results}

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

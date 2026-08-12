from __future__ import annotations

import logging
from enum import Enum
from typing import Dict
from typing import Iterable
from typing import Set

from ..errors import InvalidArgumentError
from .buffer_health_calc import BufferHealthCalc
from .connection_health_calc import ConnectionHealthCalc
from .constraint_health_calc import ConstraintHealthCalc
from .index_health_calc import IndexHealthCalc
from .replication_calc import ReplicationCalc
from .sequence_health_calc import SequenceHealthCalc
from .vacuum_health_calc import VacuumHealthCalc

logger = logging.getLogger(__name__)


class HealthType(str, Enum):
    INDEX = "index"
    CONNECTION = "connection"
    VACUUM = "vacuum"
    SEQUENCE = "sequence"
    REPLICATION = "replication"
    BUFFER = "buffer"
    CONSTRAINT = "constraint"
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
    ) -> Dict[str, str]:
        """Run database health checks and return one finding per check.

        Args:
            health_type: A single health type, a comma-separated string, or an
                iterable of health types. Valid values: index, connection,
                vacuum, sequence, replication, buffer, constraint, all.

        Returns:
            An ordered mapping of check name to its finding, e.g.
            ``{"Invalid index check": "No invalid indexes found."}``.

        Raises:
            InvalidArgumentError: If an unknown health type is requested.
        """
        health_types = HealthType.parse(health_type)
        results: Dict[str, str] = {}

        if HealthType.INDEX in health_types:
            index_health = IndexHealthCalc(self.sql_driver)
            results["Invalid index check"] = await index_health.invalid_index_check()
            results["Duplicate index check"] = await index_health.duplicate_index_check()
            results["Index bloat"] = await index_health.index_bloat()
            results["Unused index check"] = await index_health.unused_indexes()

        if HealthType.CONNECTION in health_types:
            connection_health = ConnectionHealthCalc(self.sql_driver)
            results["Connection health"] = await connection_health.connection_health_check()

        if HealthType.VACUUM in health_types:
            vacuum_health = VacuumHealthCalc(self.sql_driver)
            results["Vacuum health"] = await vacuum_health.transaction_id_danger_check()

        if HealthType.SEQUENCE in health_types:
            sequence_health = SequenceHealthCalc(self.sql_driver)
            results["Sequence health"] = await sequence_health.sequence_danger_check()

        if HealthType.REPLICATION in health_types:
            replication_health = ReplicationCalc(self.sql_driver)
            results["Replication health"] = await replication_health.replication_health_check()

        if HealthType.BUFFER in health_types:
            buffer_health = BufferHealthCalc(self.sql_driver)
            results["Buffer health for indexes"] = await buffer_health.index_hit_rate()
            results["Buffer health for tables"] = await buffer_health.table_hit_rate()

        if HealthType.CONSTRAINT in health_types:
            constraint_health = ConstraintHealthCalc(self.sql_driver)
            results["Constraint health"] = await constraint_health.invalid_constraints_check()

        return results

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
        return "".join(f"{name}: {finding}\n" for name, finding in results.items())

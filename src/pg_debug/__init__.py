"""pg_debug — inspect, diagnose and tune a PostgreSQL database.

The library is async. The usual entry point is :class:`PgDebug`, which owns a
connection pool and exposes the analysis tools on top of it:

    ```python
    import asyncio
    from pg_debug import PgDebug

    async def main():
        async with PgDebug("postgresql://localhost/mydb") as pg:
            print(await pg.analyze_db_health("index,vacuum"))

    asyncio.run(main())
    ```

The individual tools (:class:`SchemaInspector`, :class:`ExplainPlanTool`,
:class:`DatabaseHealthTool`, :class:`TopQueriesCalc`,
:class:`DatabaseTuningAdvisor`) can also be used directly with a
:class:`SqlDriver` if you already manage your own connection.
"""

from .activity import ActivityInspector
from .client import DATABASE_URL_ENV_VARS
from .client import AccessMode
from .client import PgDebug
from .client import resolve_database_url
from .configuration import ConfigFinding
from .configuration import ConfigurationReviewer
from .db import DbConnPool
from .db import SafeSqlDriver
from .db import SqlDriver
from .errors import ConnectionFailedError
from .errors import ExplainPlanError
from .errors import ExtensionRequiredError
from .errors import IndexTuningError
from .errors import InvalidArgumentError
from .errors import MissingDependencyError
from .errors import NotConnectedError
from .errors import PgDebugError
from .errors import QueryError
from .health import DatabaseHealthTool
from .health import HealthCheckResult
from .health import HealthType
from .index import MAX_NUM_INDEX_TUNING_QUERIES
from .index import DatabaseTuningAdvisor
from .index import IndexRecommendation
from .index import LLMOptimizerTool
from .index_use import IndexUse
from .index_use import IndexUseAnalyzer
from .index_use import IndexUseFinding
from .index_use import IndexUseReason
from .migration import LockLevel
from .migration import MigrationLinter
from .migration import StatementReview
from .model import Confidence
from .model import ModelFinding
from .model import ModelReviewer
from .model import RuleId
from .model import Severity
from .partition import PartitionAdvisor
from .partition import PartitionCandidate
from .partition import Strategy
from .plans import ExplainPlanTool
from .plans import PlanDiagnoser
from .plans import PlanFinding
from .plans import PlanIssue
from .plans import diagnose_plan
from .plans.hypothetical import IndexDefinition
from .plans.tree import ExplainPlanArtifact
from .plans.tree import PlanNode
from .schema import ObjectType
from .schema import SchemaInspector
from .statistics import StatisticsInspector
from .sync import SyncPgDebug
from .top_queries import TopQueriesCalc

__version__ = "0.2.0"

__all__ = [
    "DATABASE_URL_ENV_VARS",
    "MAX_NUM_INDEX_TUNING_QUERIES",
    "AccessMode",
    "ActivityInspector",
    "Confidence",
    "ConfigFinding",
    "ConfigurationReviewer",
    "ConnectionFailedError",
    "DatabaseHealthTool",
    "DatabaseTuningAdvisor",
    "DbConnPool",
    "ExplainPlanArtifact",
    "ExplainPlanError",
    "ExplainPlanTool",
    "ExtensionRequiredError",
    "HealthCheckResult",
    "HealthType",
    "IndexDefinition",
    "IndexRecommendation",
    "IndexTuningError",
    "IndexUse",
    "IndexUseAnalyzer",
    "IndexUseFinding",
    "IndexUseReason",
    "InvalidArgumentError",
    "LLMOptimizerTool",
    "LockLevel",
    "MigrationLinter",
    "MissingDependencyError",
    "ModelFinding",
    "ModelReviewer",
    "NotConnectedError",
    "ObjectType",
    "PartitionAdvisor",
    "PartitionCandidate",
    "PgDebug",
    "PgDebugError",
    "PlanDiagnoser",
    "PlanFinding",
    "PlanIssue",
    "PlanNode",
    "QueryError",
    "RuleId",
    "SafeSqlDriver",
    "SchemaInspector",
    "Severity",
    "SqlDriver",
    "StatementReview",
    "StatisticsInspector",
    "Strategy",
    "SyncPgDebug",
    "TopQueriesCalc",
    "__version__",
    "diagnose_plan",
    "resolve_database_url",
]

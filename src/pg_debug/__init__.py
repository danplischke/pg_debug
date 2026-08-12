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
from .artifacts import ErrorResult
from .artifacts import ExplainPlanArtifact
from .artifacts import PlanNode
from .client import DATABASE_URL_ENV_VARS
from .client import AccessMode
from .client import PgDebug
from .client import resolve_database_url
from .database_health import DatabaseHealthTool
from .database_health import HealthCheckResult
from .database_health import HealthType
from .errors import ConnectionFailedError
from .errors import ExplainPlanError
from .errors import ExtensionRequiredError
from .errors import InvalidArgumentError
from .errors import MissingDependencyError
from .errors import NotConnectedError
from .errors import PgDebugError
from .errors import QueryError
from .explain import ExplainPlanTool
from .index import MAX_NUM_INDEX_TUNING_QUERIES
from .index import DatabaseTuningAdvisor
from .index import IndexRecommendation
from .index import LLMOptimizerTool
from .schema import ObjectType
from .schema import SchemaInspector
from .sql import DbConnPool
from .sql import IndexDefinition
from .sql import SafeSqlDriver
from .sql import SqlDriver
from .statistics import StatisticsInspector
from .sync import SyncPgDebug
from .top_queries import TopQueriesCalc

__version__ = "0.1.0"

__all__ = [
    "DATABASE_URL_ENV_VARS",
    "MAX_NUM_INDEX_TUNING_QUERIES",
    "AccessMode",
    "ActivityInspector",
    "ConnectionFailedError",
    "DatabaseHealthTool",
    "DatabaseTuningAdvisor",
    "DbConnPool",
    "ErrorResult",
    "ExplainPlanArtifact",
    "ExplainPlanError",
    "ExplainPlanTool",
    "ExtensionRequiredError",
    "HealthCheckResult",
    "HealthType",
    "IndexDefinition",
    "IndexRecommendation",
    "InvalidArgumentError",
    "LLMOptimizerTool",
    "MissingDependencyError",
    "NotConnectedError",
    "ObjectType",
    "PgDebug",
    "PgDebugError",
    "PlanNode",
    "QueryError",
    "SafeSqlDriver",
    "SchemaInspector",
    "SqlDriver",
    "StatisticsInspector",
    "SyncPgDebug",
    "TopQueriesCalc",
    "__version__",
    "resolve_database_url",
]

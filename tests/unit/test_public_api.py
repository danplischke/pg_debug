"""The surface a user can reach, pinned so that moving modules cannot change it.

Modules are free to move around inside the package. What ``pg_debug`` exports,
the commands the CLI accepts and the imports the documentation tells people to
write are not — a change to any of them is an API change and should be made on
purpose, by editing the lists here.
"""

import argparse
import importlib
import importlib.metadata
import re
from pathlib import Path

import pg_debug
from pg_debug import cli

REPOSITORY = Path(__file__).resolve().parents[2]

#: Documents whose code samples import from pg_debug.
DOCUMENTS = (REPOSITORY / "README.md", REPOSITORY / ".claude" / "skills" / "pg-debug" / "SKILL.md")

PUBLIC_NAMES = [
    "AccessMode",
    "ActivityInspector",
    "Confidence",
    "ConfigFinding",
    "ConfigurationReviewer",
    "ConnectionFailedError",
    "DATABASE_URL_ENV_VARS",
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
    "LockLevel",
    "MAX_NUM_INDEX_TUNING_QUERIES",
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
    "resolve_database_url",
]

CLI_COMMANDS = [
    "activity",
    "check-migration",
    "column-stats",
    "config",
    "describe",
    "diagnose",
    "execute",
    "explain",
    "health",
    "model",
    "objects",
    "partitions",
    "schemas",
    "top-queries",
    "tune-queries",
    "tune-workload",
    "vacuum-progress",
]

_IMPORT_LINE = re.compile(r"^from (pg_debug[\w.]*) import (.+)$")


def _documented_imports():
    for document in DOCUMENTS:
        for line in document.read_text().splitlines():
            match = _IMPORT_LINE.match(line.strip())
            if match:
                yield document.name, match.group(1), [name.strip() for name in match.group(2).split(",")]


def test_top_level_exports_are_unchanged():
    assert sorted(pg_debug.__all__) == PUBLIC_NAMES


def test_every_export_resolves():
    assert [name for name in pg_debug.__all__ if not hasattr(pg_debug, name)] == []


def test_cli_commands_are_unchanged():
    parser = cli.build_parser()
    subcommands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    assert sorted(subcommands.choices) == CLI_COMMANDS


def test_documented_imports_work():
    imports = list(_documented_imports())
    assert imports, "found no pg_debug imports in the documentation"

    broken = []
    for document, module_name, names in imports:
        module = importlib.import_module(module_name)
        broken.extend(f"{document}: from {module_name} import {name}" for name in names if not hasattr(module, name))
    assert broken == []


def test_version_is_the_installed_version():
    """``__version__`` in the package is the one place the version is written;
    the installed distribution has to carry the same number."""
    assert pg_debug.__version__ == importlib.metadata.version("pg-debug")

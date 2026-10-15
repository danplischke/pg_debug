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
import subprocess
import sys
from pathlib import Path

import pg_debug
from pg_debug import cli

REPOSITORY = Path(__file__).resolve().parents[2]

#: Documents whose code samples import from pg_debug.
DOCUMENTS = (REPOSITORY / "README.md", *sorted((REPOSITORY / "plugins" / "pg-debug" / "skills").rglob("*.md")))

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
    "QueryContext",
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
    "index-use",
    "locks",
    "model",
    "objects",
    "partitions",
    "pooler",
    "schemas",
    "server-rates",
    "top-queries",
    "tune-queries",
    "tune-workload",
    "vacuum-progress",
]

_IMPORT_LINE = re.compile(r"^from (pg_debug[\w.]*) import (.+)$")


def _documented_imports():
    for document in DOCUMENTS:
        for line in document.read_text(encoding="utf-8").splitlines():
            match = _IMPORT_LINE.match(line.strip())
            if match:
                yield document.name, match.group(1), [name.strip() for name in match.group(2).split(",")]


def test_top_level_exports_are_unchanged():
    assert sorted(pg_debug.__all__) == PUBLIC_NAMES


def test_every_export_resolves():
    assert [name for name in pg_debug.__all__ if not hasattr(pg_debug, name)] == []


def test_the_llm_method_is_exported_where_it_was():
    from pg_debug.index import LLMOptimizerTool
    from pg_debug.index import llm

    assert LLMOptimizerTool is llm.LLMOptimizerTool is pg_debug.LLMOptimizerTool


def test_importing_the_package_does_not_load_the_llm_method():
    """It needs the llm extra. Loaded on import, it made pydantic a dependency
    of every install, and every run pay for importing it."""
    code = "import sys, pg_debug, pg_debug.cli, pg_debug.index; print(sorted(m for m in ('pydantic', 'pg_debug.index.llm') if m in sys.modules))"

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert result.stdout.strip() == "[]"


def test_the_llm_method_without_its_extra_says_how_to_install_it():
    code = (
        "import sys\n"
        "sys.modules['pydantic'] = None  # as though the llm extra were not installed\n"
        "import pg_debug\n"
        "try:\n"
        "    pg_debug.LLMOptimizerTool\n"
        "except pg_debug.MissingDependencyError as e:\n"
        "    print(e)\n"
    )

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert "pip install 'pg-debug[llm]'" in result.stdout


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

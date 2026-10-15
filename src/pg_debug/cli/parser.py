"""The pg-debug command line: its arguments, and which handler and renderer each command uses."""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterator
from typing import Any

from .. import __version__
from ..baseline import config_finding_key
from ..baseline import model_finding_key
from ..client import DATABASE_URL_ENV_VARS
from ..client import AccessMode
from ..context import CONFIG_FILE_NAME
from ..health import HealthType
from ..health.checks.index import MIN_WINDOW_DAYS
from ..index.base import MAX_NUM_INDEX_TUNING_QUERIES
from ..index.llm_settings import DEFAULT_LLM_MODEL
from ..logs import FORMATS as LOG_FORMATS
from ..model import DEFAULT_MIN_ROWS
from ..model import RuleId
from ..partition import DEFAULT_MIN_PRUNING_SHARE
from ..partition import DEFAULT_MIN_TABLE_BYTES
from ..plans import DEFAULT_MIN_RATIO
from ..schema import ObjectType
from .commands import cmd_activity
from .commands import cmd_check_migration
from .commands import cmd_column_stats
from .commands import cmd_config
from .commands import cmd_describe
from .commands import cmd_diagnose
from .commands import cmd_execute
from .commands import cmd_explain
from .commands import cmd_health
from .commands import cmd_index_use
from .commands import cmd_locks
from .commands import cmd_model
from .commands import cmd_objects
from .commands import cmd_partitions
from .commands import cmd_plans
from .commands import cmd_schemas
from .commands import cmd_top_queries
from .commands import cmd_tune_queries
from .commands import cmd_tune_workload
from .commands import cmd_vacuum_progress
from .render import as_is
from .render import health_checks_json
from .render import marked_against
from .render import plan_document
from .render import plan_shapes_json
from .render import render_activity
from .render import render_column_stats
from .render import render_configuration_review
from .render import render_health_checks
from .render import render_index_recommendations
from .render import render_index_use
from .render import render_lock_tree
from .render import render_log_report
from .render import render_migration_review
from .render import render_model_findings
from .render import render_object_details
from .render import render_partition_candidates
from .render import render_plan
from .render import render_plan_findings
from .render import render_plan_shapes
from .render import render_rows
from .render import render_vacuum_progress

PROG = "pg-debug"

#: Exit status for `health --exit-code` when a check reports a problem. Kept
#: distinct from 1 (error) and 2 (argparse usage error).
UNHEALTHY_EXIT_CODE = 3

INDEX_METHOD_RE = re.compile(r"^\w+$")

INDEX_SPEC_HELP = (
    "Expected 'table(column, ...)' with an optional ':method', for example "
    "'users(email)', 'orders(user_id, created_at):brin' or 'users(lower(email))'."
)


def _scan_sql(text: str) -> Iterator[tuple[int, str, int, bool]]:
    """Walk SQL text, tracking parenthesis depth and quoting.

    Yields ``(position, character, depth, quoted)``, where depth counts
    unquoted parentheses opened before this character and quoted says whether
    the character sits inside a '...' literal or a "..." identifier. Doubled
    quotes are the SQL escape and stay inside the quoted run.
    """
    depth = 0
    quote = ""
    index = 0

    while index < len(text):
        char = text[index]

        if quote:
            if char == quote:
                if text[index + 1 : index + 2] == quote:  # '' or "" escape
                    yield index, char, depth, True
                    index += 1
                    yield index, text[index], depth, True
                    index += 1
                    continue
                quote = ""
            yield index, char, depth, True
        elif char in "'\"":
            quote = char
            yield index, char, depth, True
        elif char == "(":
            yield index, char, depth, False
            depth += 1
        elif char == ")":
            depth -= 1
            yield index, char, depth, False
        else:
            yield index, char, depth, False

        index += 1


def split_index_columns(text: str) -> list[str]:
    """Split an index column list on the commas that separate its entries.

    Commas inside a function call or a quoted string belong to that entry, so
    ``date_trunc('day', created_at), status`` is two columns, not three.
    """
    columns: list[str] = []
    start = 0

    for position, char, depth, quoted in _scan_sql(text):
        if char == "," and depth == 0 and not quoted:
            columns.append(text[start:position])
            start = position + 1

    columns.append(text[start:])
    return [column.strip() for column in columns if column.strip()]


def parse_index_spec(spec: str) -> dict[str, Any]:
    """Parse a hypothetical index spec such as ``users(lower(email)):btree``.

    Columns may be plain names or expressions, matching what PostgreSQL accepts
    in a CREATE INDEX column list.

    Returns:
        A dict with ``table``, ``columns`` and ``using`` keys.

    Raises:
        argparse.ArgumentTypeError: If the spec is malformed.
    """

    def invalid(reason: str) -> argparse.ArgumentTypeError:
        return argparse.ArgumentTypeError(f"Invalid index spec: {spec!r}. {reason}")

    text = spec.strip()
    opened_at = closed_at = None

    for position, char, depth, quoted in _scan_sql(text):
        if quoted:
            continue
        if char == "(" and depth == 0:
            opened_at = position
        elif char == ")" and depth == 0:
            closed_at = position
            break

    if opened_at is None or closed_at is None:
        raise invalid(f"Unbalanced parentheses. {INDEX_SPEC_HELP}")

    table = text[:opened_at].strip()
    if not table:
        raise invalid(f"No table name before the column list. {INDEX_SPEC_HELP}")

    columns = split_index_columns(text[opened_at + 1 : closed_at])
    if not columns:
        raise invalid("At least one column is required.")

    using = "btree"
    trailing = text[closed_at + 1 :].strip()
    if trailing:
        if not trailing.startswith(":"):
            raise invalid(f"Unexpected text after the column list: {trailing!r}. {INDEX_SPEC_HELP}")
        using = trailing[1:].strip()
        if not INDEX_METHOD_RE.match(using):
            raise invalid(f"Invalid index method: {using!r}.")

    return {"table": table, "columns": columns, "using": using}


#: Suffixes accepted for a size argument, as PostgreSQL itself writes them.
SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def parse_size(value: str) -> int:
    """Return a byte count from ``500MB``, ``2GB`` or a bare number.

    Raises:
        argparse.ArgumentTypeError: If the value is not a size.
    """
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", value)
    if not match:
        raise argparse.ArgumentTypeError(f"'{value}' is not a size. Write it as a number of bytes, or with a unit: 500MB, 2GB.")

    number, unit = match.group(1), match.group(2).upper()
    if unit and unit not in SIZE_UNITS:
        raise argparse.ArgumentTypeError(f"'{match.group(2)}' is not a size unit. Use one of: {', '.join(SIZE_UNITS)}.")
    return int(float(number) * SIZE_UNITS.get(unit or "B", 1))


def parse_setting(text: str) -> tuple[str, str]:
    """Split ``--set NAME=VALUE`` at its first ``=``; the value may hold more.

    Raises:
        argparse.ArgumentTypeError: If there is no ``=``, or nothing before it.
    """
    name, separator, value = text.partition("=")
    if not separator or not name.strip():
        raise argparse.ArgumentTypeError(f"'{text}' is not a setting. Write it as NAME=VALUE, for example work_mem=256MB or app.tenant_id=7.")
    return name.strip(), value


def add_context_arguments(parser: argparse.ArgumentParser) -> None:
    """``--role``, ``--set``, ``--config`` and ``--no-config``, for the commands that run or plan the user's query."""
    group = parser.add_argument_group(
        "query context",
        f"The role and settings to run your query with, as your application's sessions would. They apply to the "
        f"statements that run or plan your query, inside each one's transaction; pg-debug's own reads of the catalog "
        f"run as the connecting user. Read from {CONFIG_FILE_NAME} in this directory or the nearest parent that has "
        f"one; --role and --set override it.",
    )
    group.add_argument("--role", help="Run your query as this role, as SET ROLE does. The connecting user must be a member of it.")
    group.add_argument(
        "--set",
        dest="settings",
        metavar="NAME=VALUE",
        action="append",
        type=parse_setting,
        help="Run your query with this setting, as set_config(NAME, VALUE, true) does: app.tenant_id=7, work_mem=256MB. Repeatable.",
    )
    source = group.add_mutually_exclusive_group()
    source.add_argument("--config", metavar="FILE", help=f"Read the query context from FILE instead of {CONFIG_FILE_NAME}.")
    source.add_argument("--no-config", action="store_true", help=f"Do not read {CONFIG_FILE_NAME}.")


def add_baseline_arguments(parser: argparse.ArgumentParser) -> None:
    """``--baseline`` and ``--save-baseline``, for the commands that gate a build."""
    parser.add_argument(
        "--baseline",
        metavar="FILE",
        help="Compare against a saved baseline: findings in it are reported as known and do not affect --exit-code.",
    )
    parser.add_argument(
        "--save-baseline",
        metavar="FILE",
        help="Save this run's findings as the baseline to compare later runs against. Do it from a run you have read and accept.",
    )


def add_suppress_arguments(parser: argparse.ArgumentParser) -> None:
    """``--suppress``, ``--config`` and ``--no-config``, for the commands that gate a build on rules."""
    parser.add_argument(
        "--suppress",
        metavar="RULE",
        action="append",
        help=f"Report this rule's findings as suppressed, so they never fail --exit-code. Repeatable. The [suppress] table of "
        f"{CONFIG_FILE_NAME} does the same with a reason for each; the rules are listed in the rules reference.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config", metavar="FILE", help=f"Read [suppress] from FILE instead of {CONFIG_FILE_NAME}.")
    source.add_argument("--no-config", action="store_true", help=f"Do not read {CONFIG_FILE_NAME}.")


def add_static_argument(parser: argparse.ArgumentParser, what: str) -> None:
    """``--static``: only what reads the catalog alone, for a CI database."""
    parser.add_argument(
        "--static",
        action="store_true",
        help=f"Run only the {what} that read nothing but the catalog, which a freshly migrated, empty database in CI "
        "answers as production would. The others need the tables' contents or the workload's counters.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Inspect, diagnose and tune a PostgreSQL database.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-d",
        "--database-url",
        help="PostgreSQL connection URL. Defaults to the " + " or ".join(DATABASE_URL_ENV_VARS) + " environment variable.",
    )
    parser.add_argument(
        "--access-mode",
        choices=[mode.value for mode in AccessMode],
        default=AccessMode.UNRESTRICTED.value,
        help="unrestricted (default) runs any statement; restricted allows read-only statements only.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help="Statement timeout in seconds, for every statement. Defaults to no timeout in unrestricted mode and 30 in restricted mode.",
    )
    parser.add_argument(
        "--search-path",
        help="Schema search path for every statement, e.g. 'reporting,public'. Index tuning resolves table names through it.",
    )
    parser.add_argument(
        "--llm-model",
        metavar="MODEL",
        help=f"Model for the 'llm' tuning method. Defaults to $PG_DEBUG_LLM_MODEL, then {DEFAULT_LLM_MODEL}.",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of formatted text.")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Log at INFO level, or DEBUG when repeated.")

    # How a command's result is shown: each subcommand below that needs more
    # than a table of rows, or than its result printed as JSON, says so where
    # it names its handler.
    parser.set_defaults(render=render_rows, json_payload=as_is)

    subparsers = parser.add_subparsers(dest="command", metavar="command", required=True)

    schemas = subparsers.add_parser("schemas", help="List database schemas")
    schemas.set_defaults(func=cmd_schemas)

    objects = subparsers.add_parser("objects", help="List objects in a schema")
    objects.add_argument("schema", help="Schema name")
    objects.add_argument(
        "-t",
        "--type",
        dest="object_type",
        choices=[t.value for t in ObjectType],
        default=ObjectType.TABLE.value,
        help="Object type to list (default: table).",
    )
    objects.set_defaults(func=cmd_objects)

    describe = subparsers.add_parser("describe", help="Show columns, constraints and indexes of an object")
    describe.add_argument("name", help="Object name, optionally schema-qualified (schema.name)")
    describe.add_argument("-s", "--schema", default="public", help="Schema name, when not part of the object name (default: public).")
    describe.add_argument(
        "-t",
        "--type",
        dest="object_type",
        choices=[t.value for t in ObjectType],
        default=ObjectType.TABLE.value,
        help="Object type (default: table).",
    )
    describe.set_defaults(func=cmd_describe, render=render_object_details)

    execute = subparsers.add_parser("execute", help="Run a SQL statement")
    execute.add_argument("sql", help="SQL to run, or - to read it from stdin")
    add_context_arguments(execute)
    execute.set_defaults(func=cmd_execute)

    explain = subparsers.add_parser("explain", help="Show the execution plan for a query")
    explain.add_argument("sql", help="Query to explain, or - to read it from stdin")
    explain.add_argument("--analyze", action="store_true", help="Run the query and report real execution statistics.")
    explain.add_argument(
        "-i",
        "--hypothetical-index",
        dest="hypothetical_indexes",
        metavar="SPEC",
        action="append",
        type=parse_index_spec,
        help="Simulate an index without creating it, e.g. 'users(email)' or 'orders(user_id, created_at):btree'. Repeatable; requires HypoPG.",
    )
    add_context_arguments(explain)
    explain.set_defaults(func=cmd_explain, render=render_plan, json_payload=plan_document)

    diagnose = subparsers.add_parser("diagnose", help="Run a query, or read a plan, and report what is wrong with it")
    diagnose.add_argument("sql", help="Query to diagnose, a file holding it, or - to read it from stdin. With --plan, the plan instead.")
    diagnose.add_argument(
        "--plan",
        action="store_true",
        help="Read a plan instead of running a query: EXPLAIN (ANALYZE, FORMAT JSON) output, as a file, inline, or - for stdin. "
        "Runs nothing, so it works in restricted mode. Connects only to check the tables' statistics, and only if a "
        "database URL is given; without one the findings say what could not be checked.",
    )
    diagnose.add_argument("--offline", action="store_true", help="With --plan, do not connect even if a database URL is set.")
    diagnose.add_argument(
        "--min-ratio",
        type=float,
        default=DEFAULT_MIN_RATIO,
        help=(f"How far a row estimate has to be from what the query found before it is reported, as a factor (default: {DEFAULT_MIN_RATIO:g})."),
    )
    add_context_arguments(diagnose)
    diagnose.set_defaults(func=cmd_diagnose, render=render_plan_findings)

    index_use = subparsers.add_parser("index-use", help="Say whether a query uses each index on its tables, and if not, why")
    index_use.add_argument("sql", help="Query to plan, a file holding it, or - to read it from stdin")
    index_use.add_argument("-i", "--index", help="Report only this index.")
    add_context_arguments(index_use)
    index_use.set_defaults(func=cmd_index_use, render=render_index_use)

    column_stats = subparsers.add_parser("column-stats", help="Show the planner's statistics for a table")
    column_stats.add_argument("table", help="Table name, optionally schema-qualified")
    column_stats.set_defaults(func=cmd_column_stats, render=render_column_stats)

    model = subparsers.add_parser("model", help="Review the data model and report what to change")
    model.add_argument("-s", "--schema", help="Restrict the review to one schema (default: every user schema).")
    model.add_argument(
        "-r",
        "--rule",
        dest="rules",
        default=RuleId.ALL.value,
        help="Comma-separated rules to run: " + ", ".join(sorted(r.value for r in RuleId)) + " (default: all).",
    )
    model.add_argument(
        "--proven-only",
        action="store_true",
        help="Report only what the catalog proves, dropping the candidates inferred from sampled statistics.",
    )
    model.add_argument(
        "--min-rows",
        type=int,
        default=DEFAULT_MIN_ROWS,
        help=f"Rules that reason from sampled statistics ignore tables smaller than this (default: {DEFAULT_MIN_ROWS}).",
    )
    model.add_argument(
        "--exit-code",
        action="store_true",
        help=f"Exit with status {UNHEALTHY_EXIT_CODE} when any high-severity finding is reported, for use in CI.",
    )
    add_static_argument(model, "rules")
    add_baseline_arguments(model)
    add_suppress_arguments(model)
    model.set_defaults(func=cmd_model, render=render_model_findings, json_payload=marked_against(model_finding_key))

    config = subparsers.add_parser("config", help="Review the server's settings against what it is actually doing")
    config.add_argument(
        "--exit-code",
        action="store_true",
        help=f"Exit with status {UNHEALTHY_EXIT_CODE} when any high-severity finding is reported, for use in CI.",
    )
    add_baseline_arguments(config)
    add_suppress_arguments(config)
    config.set_defaults(func=cmd_config, render=render_configuration_review, json_payload=marked_against(config_finding_key))

    partitions = subparsers.add_parser("partitions", help="Say whether a table is worth partitioning, from what the workload does")
    partitions.add_argument(
        "--min-size",
        dest="min_table_bytes",
        type=parse_size,
        default=DEFAULT_MIN_TABLE_BYTES,
        help=f"Ignore tables smaller than this, e.g. 500MB or 2GB (default: {DEFAULT_MIN_TABLE_BYTES // 1024**3}GB).",
    )
    partitions.add_argument(
        "--min-pruning-share",
        type=float,
        default=DEFAULT_MIN_PRUNING_SHARE,
        help=f"The share of recorded query time that has to keep pruning for a table to be recommended (default: {DEFAULT_MIN_PRUNING_SHARE}).",
    )
    partitions.add_argument(
        "--limit",
        type=int,
        default=200,
        help="How many recorded queries to judge by (default: 200).",
    )
    partitions.set_defaults(func=cmd_partitions, render=render_partition_candidates)

    check_migration = subparsers.add_parser("check-migration", help="Review a migration for the locks it takes before running it")
    check_migration.add_argument("sql", help="Migration file, SQL, or - to read it from stdin")
    check_migration.add_argument(
        "--offline",
        action="store_true",
        help="Do not connect. Severities are not softened by table sizes, which is what you want when the database "
        "reachable from CI is not the one the migration will run against.",
    )
    check_migration.add_argument(
        "--exit-code",
        action="store_true",
        help=f"Exit with status {UNHEALTHY_EXIT_CODE} when any statement is high severity, for use in CI.",
    )
    check_migration.set_defaults(func=cmd_check_migration, render=render_migration_review)

    activity = subparsers.add_parser("activity", help="Show what the server is doing right now")
    activity.add_argument("-a", "--all", dest="include_idle", action="store_true", help="Include idle backends and idle auxiliary processes.")
    activity.add_argument(
        "-m",
        "--min-duration",
        dest="min_duration",
        type=float,
        metavar="SECONDS",
        help="Only show backends that have held their current state this long, e.g. 30 for queries running over 30s.",
    )
    activity.add_argument("-b", "--blocked", action="store_true", help="Only show backends waiting on a lock another backend holds.")
    activity.set_defaults(func=cmd_activity, render=render_activity)

    locks = subparsers.add_parser("locks", help="Show who is blocking whom, the root blockers first")
    locks.set_defaults(func=cmd_locks, render=render_lock_tree)

    vacuum_progress = subparsers.add_parser("vacuum-progress", help="Show running vacuums and how far along they are")
    vacuum_progress.set_defaults(func=cmd_vacuum_progress, render=render_vacuum_progress)

    health = subparsers.add_parser("health", help="Run database health checks")
    health.add_argument(
        "-t",
        "--type",
        dest="health_type",
        default=HealthType.ALL.value,
        help="Comma-separated checks to run: " + ", ".join(sorted(t.value for t in HealthType)) + " (default: all).",
    )
    health.add_argument(
        "--exit-code",
        action="store_true",
        help=(
            f"Exit with status {UNHEALTHY_EXIT_CODE} when a check that ran finds a problem, for use in CI. "
            "A check that could not run makes the exit status 1, unless a check that ran found a problem; "
            "with --save-baseline such a run saves nothing and exits 1."
        ),
    )
    health.add_argument(
        "--min-window-days",
        type=float,
        default=MIN_WINDOW_DAYS,
        metavar="DAYS",
        help=(
            "Call an index unused only once the index counters have been counting this many days "
            f"(default: {MIN_WINDOW_DAYS:g}). A crash, a failover, a restore or pg_stat_reset() starts them again."
        ),
    )
    add_static_argument(health, "checks")
    add_baseline_arguments(health)
    add_suppress_arguments(health)
    health.set_defaults(func=cmd_health, render=render_health_checks, json_payload=health_checks_json)

    plans = subparsers.add_parser("plans", help="Record each query's plan shape, and fail a build when one changes")
    plans.add_argument(
        "-q",
        "--queries",
        metavar="FILE",
        action="append",
        required=True,
        help="Queries to plan, separated by semicolons: a file, inline SQL, or - for stdin. Repeatable. A '-- name: ...' "
        "line above a query names it; otherwise it is known by its fingerprint, which ignores constants.",
    )
    plans.add_argument(
        "--exit-code",
        action="store_true",
        help=f"Exit with status {UNHEALTHY_EXIT_CODE} when a plan's shape differs from the one --baseline holds, for use in CI.",
    )
    add_baseline_arguments(plans)
    add_context_arguments(plans)
    plans.set_defaults(func=cmd_plans, render=render_plan_shapes, json_payload=plan_shapes_json)

    logs = subparsers.add_parser("logs", help="Read server logs for deadlocks, lock waits, temp files, vacuums and checkpoints")
    logs.add_argument("files", nargs="+", metavar="FILE", help="Log files: stderr, csvlog or jsonlog, plain or .gz; - for stdin.")
    logs.add_argument(
        "--prefix",
        metavar="PREFIX",
        help="The server's log_line_prefix, for a stderr log, e.g. '%%m [%%p] %%q%%u@%%d '. Without it the timestamp, "
        "[pid] and user@db are found where the common prefixes put them.",
    )
    logs.add_argument(
        "--format",
        dest="log_format",
        choices=list(LOG_FORMATS),
        help="The log's format, as log_destination names it (default: detected from each file).",
    )
    logs.set_defaults(render=render_log_report)

    top_queries = subparsers.add_parser("top-queries", help="Report the most expensive queries (needs pg_stat_statements)")
    top_queries.add_argument(
        "-s",
        "--sort-by",
        choices=["resources", "mean_time", "total_time"],
        default="resources",
        help="Ranking criteria (default: resources).",
    )
    top_queries.add_argument("-n", "--limit", type=int, default=10, help="Number of queries to report when ranking by time (default: 10).")
    top_queries.add_argument(
        "-i",
        "--interval",
        type=float,
        metavar="SECONDS",
        help="Sample over this many seconds instead of reporting totals since the last statistics reset.",
    )
    top_queries.set_defaults(func=cmd_top_queries)

    tune_workload = subparsers.add_parser(
        "tune-workload",
        help="Recommend indexes for the recorded workload (needs pg_stat_statements and HypoPG)",
    )
    _add_tuning_arguments(tune_workload)
    tune_workload.set_defaults(func=cmd_tune_workload, render=render_index_recommendations)

    tune_queries = subparsers.add_parser("tune-queries", help="Recommend indexes for specific queries (needs HypoPG)")
    tune_queries.add_argument(
        "queries",
        nargs="+",
        metavar="QUERY",
        help=f"Queries to tune for (up to {MAX_NUM_INDEX_TUNING_QUERIES}), or - to read newline-separated queries from stdin.",
    )
    _add_tuning_arguments(tune_queries)
    tune_queries.set_defaults(func=cmd_tune_queries, render=render_index_recommendations)

    return parser


def _add_tuning_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-index-size-mb", type=int, default=10000, help="Total size budget for recommended indexes (default: 10000).")
    parser.add_argument(
        "--method",
        choices=["dta", "llm"],
        default="dta",
        help="dta (default) searches index combinations against the planner; llm asks a language model and needs the llm extra.",
    )
    add_context_arguments(parser)

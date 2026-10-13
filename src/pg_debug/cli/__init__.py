"""Command line interface for pg_debug."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import Sequence
from typing import Tuple

from .. import __version__
from ..baseline import Baseline
from ..baseline import Partition
from ..baseline import config_finding_key
from ..baseline import describe_key
from ..baseline import health_finding_key
from ..baseline import model_finding_key
from ..baseline import partition
from ..client import DATABASE_URL_ENV_VARS
from ..client import AccessMode
from ..client import PgDebug
from ..db import obfuscate_password
from ..errors import InvalidArgumentError
from ..errors import PgDebugError
from ..health import HealthType
from ..index.base import MAX_NUM_INDEX_TUNING_QUERIES
from ..index.llm import DEFAULT_LLM_MODEL
from ..migration import MigrationLinter
from ..model import DEFAULT_MIN_ROWS
from ..model import RuleId
from ..model import Severity
from ..partition import DEFAULT_MIN_PRUNING_SHARE
from ..partition import DEFAULT_MIN_TABLE_BYTES
from ..plans import DEFAULT_MIN_RATIO
from ..plans import diagnose_plan
from ..schema import ObjectType
from .render import render_activity
from .render import render_column_stats
from .render import render_configuration_review
from .render import render_health_checks
from .render import render_index_recommendations
from .render import render_index_use
from .render import render_lock_tree
from .render import render_migration_review
from .render import render_model_findings
from .render import render_object_details
from .render import render_partition_candidates
from .render import render_plan_findings
from .render import render_rows
from .render import render_vacuum_progress
from .render import to_json

logger = logging.getLogger(__name__)

PROG = "pg-debug"

#: Exit status for `health --exit-code` when a check reports a problem. Kept
#: distinct from 1 (error) and 2 (argparse usage error).
UNHEALTHY_EXIT_CODE = 3

INDEX_METHOD_RE = re.compile(r"^\w+$")

INDEX_SPEC_HELP = (
    "Expected 'table(column, ...)' with an optional ':method', for example "
    "'users(email)', 'orders(user_id, created_at):brin' or 'users(lower(email))'."
)


def _scan_sql(text: str) -> Iterator[Tuple[int, str, int, bool]]:
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


def split_index_columns(text: str) -> List[str]:
    """Split an index column list on the commas that separate its entries.

    Commas inside a function call or a quoted string belong to that entry, so
    ``date_trunc('day', created_at), status`` is two columns, not three.
    """
    columns: List[str] = []
    start = 0

    for position, char, depth, quoted in _scan_sql(text):
        if char == "," and depth == 0 and not quoted:
            columns.append(text[start:position])
            start = position + 1

    columns.append(text[start:])
    return [column.strip() for column in columns if column.strip()]


def parse_index_spec(spec: str) -> Dict[str, Any]:
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


def read_sql(value: str) -> str:
    """Return SQL from an argument, reading stdin when the argument is ``-``."""
    if value == "-":
        return read_stdin()
    return value


def read_sql_or_file(value: str) -> str:
    """Return text from a file path, stdin (``-``) or the argument itself."""
    if value == "-":
        return read_stdin()
    path = Path(value)
    # A path is the usual case; inline SQL is accepted so the command is easy
    # to try without writing a file first.
    if path.exists():
        return read_text_file(path)
    return value


# SQL and plans are read as UTF-8 whatever the platform's code page, which on
# Windows is cp1252 or similar; "-sig" drops the byte order mark some Windows
# editors write at the start of a UTF-8 file.
INPUT_ENCODING = "utf-8-sig"


def read_text_file(path: Path) -> str:
    """Return a file's text, decoded as UTF-8."""
    try:
        return path.read_text(encoding=INPUT_ENCODING)
    except UnicodeDecodeError as e:
        raise InvalidArgumentError(f"{path} is not UTF-8 text ({e.reason}); save it as UTF-8.") from None


def read_stdin() -> str:
    """Return standard input, decoded as UTF-8."""
    stdin = sys.stdin
    if isinstance(stdin, io.TextIOWrapper):
        stdin.reconfigure(encoding=INPUT_ENCODING)
    try:
        return stdin.read()
    except UnicodeDecodeError as e:
        raise InvalidArgumentError(f"Standard input is not UTF-8 text ({e.reason}).") from None


def use_utf8_output() -> None:
    """Write standard output and error as UTF-8.

    On Windows, output that goes to a file or a pipe is encoded in the ANSI
    code page, cp1252 or similar, which has no arrow to draw a plan with:
    ``pg-debug explain ... > plan.txt`` stopped with a codec error. A console
    is unaffected, as Python writes to it in Unicode either way.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            # Naming an encoding resets the error handler to strict unless it
            # is passed too, and stderr's backslashreplace is worth keeping.
            stream.reconfigure(encoding="utf-8", errors=stream.errors)


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
        help="Statement timeout in seconds. Defaults to no timeout in unrestricted mode and 30 in restricted mode.",
    )
    parser.add_argument(
        "--search-path",
        help="Schema search path to set on the connection, e.g. 'reporting,public'. Index tuning resolves table names through it.",
    )
    parser.add_argument(
        "--llm-model",
        metavar="MODEL",
        help=f"Model for the 'llm' tuning method. Defaults to $PG_DEBUG_LLM_MODEL, then {DEFAULT_LLM_MODEL}.",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of formatted text.")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Log at INFO level, or DEBUG when repeated.")

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
    describe.set_defaults(func=cmd_describe)

    execute = subparsers.add_parser("execute", help="Run a SQL statement")
    execute.add_argument("sql", help="SQL to run, or - to read it from stdin")
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
    explain.set_defaults(func=cmd_explain)

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
    diagnose.set_defaults(func=cmd_diagnose)

    index_use = subparsers.add_parser("index-use", help="Say whether a query uses each index on its tables, and if not, why")
    index_use.add_argument("sql", help="Query to plan, a file holding it, or - to read it from stdin")
    index_use.add_argument("-i", "--index", help="Report only this index.")
    index_use.set_defaults(func=cmd_index_use)

    column_stats = subparsers.add_parser("column-stats", help="Show the planner's statistics for a table")
    column_stats.add_argument("table", help="Table name, optionally schema-qualified")
    column_stats.set_defaults(func=cmd_column_stats)

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
    add_baseline_arguments(model)
    model.set_defaults(func=cmd_model)

    config = subparsers.add_parser("config", help="Review the server's settings against what it is actually doing")
    config.add_argument(
        "--exit-code",
        action="store_true",
        help=f"Exit with status {UNHEALTHY_EXIT_CODE} when any high-severity finding is reported, for use in CI.",
    )
    add_baseline_arguments(config)
    config.set_defaults(func=cmd_config)

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
    partitions.set_defaults(func=cmd_partitions)

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
    check_migration.set_defaults(func=cmd_check_migration)

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
    activity.set_defaults(func=cmd_activity)

    locks = subparsers.add_parser("locks", help="Show who is blocking whom, the root blockers first")
    locks.set_defaults(func=cmd_locks)

    vacuum_progress = subparsers.add_parser("vacuum-progress", help="Show running vacuums and how far along they are")
    vacuum_progress.set_defaults(func=cmd_vacuum_progress)

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
        help=f"Exit with status {UNHEALTHY_EXIT_CODE} when any check reports a problem, for use in CI.",
    )
    add_baseline_arguments(health)
    health.set_defaults(func=cmd_health)

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
    tune_workload.set_defaults(func=cmd_tune_workload)

    tune_queries = subparsers.add_parser("tune-queries", help="Recommend indexes for specific queries (needs HypoPG)")
    tune_queries.add_argument(
        "queries",
        nargs="+",
        metavar="QUERY",
        help=f"Queries to tune for (up to {MAX_NUM_INDEX_TUNING_QUERIES}), or - to read newline-separated queries from stdin.",
    )
    _add_tuning_arguments(tune_queries)
    tune_queries.set_defaults(func=cmd_tune_queries)

    return parser


def _add_tuning_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-index-size-mb", type=int, default=10000, help="Total size budget for recommended indexes (default: 10000).")
    parser.add_argument(
        "--method",
        choices=["dta", "llm"],
        default="dta",
        help="dta (default) searches index combinations against the planner; llm asks a language model and needs the llm extra.",
    )


# -- Commands ---------------------------------------------------------------


async def cmd_schemas(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.list_schemas()


async def cmd_objects(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.list_objects(args.schema, args.object_type)


async def cmd_describe(pg: PgDebug, args: argparse.Namespace) -> Any:
    schema, _, name = args.name.rpartition(".")
    return await pg.get_object_details(schema or args.schema, name, args.object_type)


async def cmd_execute(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.execute_sql(read_sql(args.sql))


async def cmd_explain(pg: PgDebug, args: argparse.Namespace) -> Any:
    plan = await pg.explain_query(
        read_sql(args.sql),
        analyze=args.analyze,
        hypothetical_indexes=args.hypothetical_indexes,
    )
    return plan


async def cmd_diagnose(pg: PgDebug, args: argparse.Namespace) -> Any:
    if args.plan:
        return await pg.diagnose_plan(read_sql_or_file(args.sql), min_ratio=args.min_ratio)
    return await pg.diagnose_query(read_sql_or_file(args.sql), min_ratio=args.min_ratio)


async def cmd_index_use(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.explain_index_use(read_sql_or_file(args.sql), index=args.index)


async def cmd_column_stats(pg: PgDebug, args: argparse.Namespace) -> Any:
    return {
        "columns": await pg.column_stats(args.table),
        "extended_statistics": await pg.extended_statistics(args.table),
    }


async def cmd_activity(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.list_activity(
        include_idle=args.include_idle,
        min_duration_seconds=args.min_duration,
        blocked_only=args.blocked,
    )


async def cmd_locks(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.lock_tree()


async def cmd_vacuum_progress(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.vacuum_progress()


async def cmd_health(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.analyze_db_health(args.health_type)


async def cmd_config(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.review_configuration()


async def cmd_partitions(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.suggest_partitions(
        min_table_bytes=args.min_table_bytes,
        min_pruning_share=args.min_pruning_share,
        limit=args.limit,
    )


async def cmd_check_migration(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.check_migration(read_sql_or_file(args.sql))


async def cmd_model(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.review_data_model(
        rules=args.rules,
        schema=args.schema,
        min_rows=args.min_rows,
        proven_only=args.proven_only,
    )


async def cmd_top_queries(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.get_top_queries(sort_by=args.sort_by, limit=args.limit, interval_seconds=args.interval)


async def cmd_tune_workload(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.analyze_workload_indexes(max_index_size_mb=args.max_index_size_mb, method=args.method)


async def cmd_tune_queries(pg: PgDebug, args: argparse.Namespace) -> Any:
    queries: List[str] = []
    for query in args.queries:
        if query == "-":
            queries.extend(line.strip() for line in sys.stdin.read().splitlines() if line.strip())
        else:
            queries.append(query)
    return await pg.analyze_query_indexes(queries, max_index_size_mb=args.max_index_size_mb, method=args.method)


# -- Output -----------------------------------------------------------------


def _with_baseline(payload: Dict[str, Any], key: Dict[str, Any], baseline: Optional[Baseline]) -> Dict[str, Any]:
    if baseline is not None:
        payload["baseline"] = "known" if key in baseline else "new"
    return payload


def _health_check_json(name: str, check: Any, baseline: Optional[Baseline]) -> Dict[str, Any]:
    payload = check.to_dict()
    if baseline is None or check.healthy:
        return payload
    if not check.findings:
        payload["baseline"] = "known" if {"check": name} in baseline else "new"
        return payload
    payload["findings"] = [_with_baseline(dict(finding), health_finding_key(name, finding), baseline) for finding in check.findings]
    return payload


def render_baseline_trailer(path: str, split: Partition) -> str:
    """The line after the report that says what the baseline changed."""
    if not split.new:
        return f"Baseline {path}: {len(split.known)} known finding(s), nothing new."
    lines = [f"Baseline {path}: {len(split.known)} known finding(s) ignored, {len(split.new)} new:"]
    lines.extend(f"  {describe_key(key)}" for key in split.new)
    return "\n".join(lines)


def format_result(command: str, result: Any, as_json: bool, baseline: Optional[Baseline] = None) -> str:
    """Render a command result as JSON or as text."""
    if command == "explain":
        if not as_json:
            return result.to_text()
        # The artifact holds the plan as a JSON document; emit it as JSON
        # rather than as a JSON-encoded string.
        try:
            return to_json(json.loads(result.value))
        except ValueError:
            return to_json(result.value)

    if as_json:
        if command == "health":
            return to_json({name: _health_check_json(name, check, baseline) for name, check in result.items()})
        if command == "model":
            return to_json([_with_baseline(finding.to_dict(), model_finding_key(finding), baseline) for finding in result])
        if command == "check-migration":
            return to_json([review.to_dict() for review in result])
        if command == "config":
            return to_json([_with_baseline(finding.to_dict(), config_finding_key(finding), baseline) for finding in result])
        if command == "partitions":
            return to_json([candidate.to_dict() for candidate in result])
        if command == "diagnose":
            return to_json([finding.to_dict() for finding in result])
        if command == "index-use":
            return to_json([verdict.to_dict() for verdict in result])
        return to_json(result)

    if command == "activity":
        return render_activity(result)
    if command == "locks":
        return render_lock_tree(result)
    if command == "index-use":
        return render_index_use(result)
    if command == "column-stats":
        return render_column_stats(result)
    if command == "vacuum-progress":
        return render_vacuum_progress(result)
    if command == "describe":
        return render_object_details(result)
    if command == "health":
        return render_health_checks(result)
    if command == "model":
        return render_model_findings(result)
    if command == "check-migration":
        return render_migration_review(result)
    if command == "config":
        return render_configuration_review(result)
    if command == "partitions":
        return render_partition_candidates(result)
    if command == "diagnose":
        return render_plan_findings(result)
    if command in ("tune-workload", "tune-queries"):
        return render_index_recommendations(result)
    return render_rows(result)


def configure_logging(verbosity: int) -> None:
    """Send library logs to stderr at a level matching ``-v`` repetition."""
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")


def _has_database_url(explicit: Optional[str]) -> bool:
    return bool(explicit) or any(os.environ.get(var) for var in DATABASE_URL_ENV_VARS)


async def run(args: argparse.Namespace) -> int:
    """Run a parsed command, returning the process exit code."""
    if args.command == "check-migration" and args.offline:
        # Reviewing a migration against a database that is not the one it will
        # run on is worse than reviewing it against none: a table that is small
        # in staging softens every verdict about production.
        result = await MigrationLinter().review(read_sql_or_file(args.sql))
        print(format_result(args.command, result, args.json))
        return UNHEALTHY_EXIT_CODE if args.exit_code and any(r.severity is Severity.HIGH for r in result) else 0

    if args.command == "diagnose" and args.plan and (args.offline or not _has_database_url(args.database_url)):
        # A plan can be read with no database at all. With a URL in hand the
        # catalog is worth asking, so that path goes through the client below.
        print(format_result(args.command, diagnose_plan(read_sql_or_file(args.sql), min_ratio=args.min_ratio), args.json))
        return 0

    async with PgDebug(
        database_url=args.database_url,
        access_mode=AccessMode(args.access_mode),
        timeout=args.timeout,
        search_path=args.search_path,
        llm_model=args.llm_model,
    ) as pg:
        result = await args.func(pg, args)

    if getattr(args, "save_baseline", None):
        saved = Baseline.of(args.command, result)
        saved.save(args.save_baseline)
        print(f"Saved a baseline of {len(saved)} finding(s) to {args.save_baseline}.", file=sys.stderr)

    baseline: Optional[Baseline] = None
    split: Optional[Partition] = None
    if getattr(args, "baseline", None):
        baseline = Baseline.load(args.baseline, args.command)
        split = partition(args.command, result, baseline)

    print(format_result(args.command, result, args.json, baseline))
    if split is not None and not args.json:
        print(render_baseline_trailer(args.baseline, split))

    if split is not None and getattr(args, "exit_code", False):
        # Against a baseline, only what is new can fail the build.
        return UNHEALTHY_EXIT_CODE if split.new_failures else 0

    if args.command == "health" and getattr(args, "exit_code", False):
        return UNHEALTHY_EXIT_CODE if any(not check.healthy for check in result.values()) else 0

    if args.command == "config" and getattr(args, "exit_code", False):
        return UNHEALTHY_EXIT_CODE if any(finding.severity is Severity.HIGH for finding in result) else 0

    if args.command == "check-migration" and getattr(args, "exit_code", False):
        return UNHEALTHY_EXIT_CODE if any(review.severity is Severity.HIGH for review in result) else 0

    if args.command == "model" and getattr(args, "exit_code", False):
        # Only high severity fails a build. The rest is advice, and a gate that
        # fires on advice gets turned off.
        return UNHEALTHY_EXIT_CODE if any(finding.severity is Severity.HIGH for finding in result) else 0

    # Index tuning reports a failure in its payload rather than raising, so the
    # exit status has to be derived from the result.
    return 1 if isinstance(result, dict) and result.get("error") else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``pg-debug`` command."""
    if sys.platform == "win32":
        use_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    # Psycopg on Windows is not compatible with the default ProactorEventLoop.
    # See: https://www.psycopg.org/psycopg3/docs/advanced/async.html#async
    # A loop factory picks the loop for this run alone; setting the event loop
    # policy changed it for the whole process and is deprecated since 3.14.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None

    try:
        return asyncio.run(run(args), loop_factory=loop_factory)
    except PgDebugError as e:
        print(f"{PROG}: {obfuscate_password(str(e))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        logger.debug("Unhandled error", exc_info=True)
        print(f"{PROG}: {obfuscate_password(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

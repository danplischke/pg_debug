"""Command line interface for pg_debug."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Sequence

from . import __version__
from .client import DATABASE_URL_ENV_VARS
from .client import AccessMode
from .client import PgDebug
from .database_health import HealthType
from .errors import PgDebugError
from .formatting import render_activity
from .formatting import render_column_stats
from .formatting import render_health_checks
from .formatting import render_index_recommendations
from .formatting import render_object_details
from .formatting import render_rows
from .formatting import render_vacuum_progress
from .formatting import to_json
from .index.index_opt_base import MAX_NUM_INDEX_TUNING_QUERIES
from .schema import ObjectType
from .sql import obfuscate_password

logger = logging.getLogger(__name__)

PROG = "pg-debug"

#: Exit status for `health --exit-code` when a check reports a problem. Kept
#: distinct from 1 (error) and 2 (argparse usage error).
UNHEALTHY_EXIT_CODE = 3

# table(col1, col2) with an optional index method, e.g. users(email):hash
INDEX_SPEC_RE = re.compile(r"^\s*(?P<table>[^\s(]+)\s*\(\s*(?P<columns>[^)]+?)\s*\)\s*(?::\s*(?P<using>\w+)\s*)?$")


def parse_index_spec(spec: str) -> Dict[str, Any]:
    """Parse a hypothetical index spec such as ``users(email, name):btree``.

    Returns:
        A dict with ``table``, ``columns`` and ``using`` keys.

    Raises:
        argparse.ArgumentTypeError: If the spec is malformed.
    """
    match = INDEX_SPEC_RE.match(spec)
    if not match:
        raise argparse.ArgumentTypeError(f"Invalid index spec: {spec!r}. Expected the form 'table(column, ...)' or 'table(column, ...):method'.")

    columns = [c.strip() for c in match.group("columns").split(",") if c.strip()]
    if not columns:
        raise argparse.ArgumentTypeError(f"Invalid index spec: {spec!r}. At least one column is required.")

    return {"table": match.group("table"), "columns": columns, "using": match.group("using") or "btree"}


def read_sql(value: str) -> str:
    """Return SQL from an argument, reading stdin when the argument is ``-``."""
    if value == "-":
        return sys.stdin.read()
    return value


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

    column_stats = subparsers.add_parser("column-stats", help="Show the planner's statistics for a table")
    column_stats.add_argument("table", help="Table name, optionally schema-qualified")
    column_stats.set_defaults(func=cmd_column_stats)

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


async def cmd_vacuum_progress(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.vacuum_progress()


async def cmd_health(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.analyze_db_health(args.health_type)


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


def format_result(command: str, result: Any, as_json: bool) -> str:
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
            return to_json({name: check.to_dict() for name, check in result.items()})
        return to_json(result)

    if command == "activity":
        return render_activity(result)
    if command == "column-stats":
        return render_column_stats(result)
    if command == "vacuum-progress":
        return render_vacuum_progress(result)
    if command == "describe":
        return render_object_details(result)
    if command == "health":
        return render_health_checks(result)
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


async def run(args: argparse.Namespace) -> int:
    """Run a parsed command, returning the process exit code."""
    async with PgDebug(
        database_url=args.database_url,
        access_mode=AccessMode(args.access_mode),
        timeout=args.timeout,
        search_path=args.search_path,
    ) as pg:
        result = await args.func(pg, args)

    print(format_result(args.command, result, args.json))

    if args.command == "health" and getattr(args, "exit_code", False):
        return UNHEALTHY_EXIT_CODE if any(not check.healthy for check in result.values()) else 0

    # Index tuning reports a failure in its payload rather than raising, so the
    # exit status has to be derived from the result.
    return 1 if isinstance(result, dict) and result.get("error") else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``pg-debug`` command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    if sys.platform == "win32":
        # Psycopg on Windows is not compatible with the default ProactorEventLoop.
        # See: https://www.psycopg.org/psycopg3/docs/advanced/async.html#async
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]

    try:
        return asyncio.run(run(args))
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

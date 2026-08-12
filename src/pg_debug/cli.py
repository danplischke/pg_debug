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
from .formatting import render_index_recommendations
from .formatting import render_mapping
from .formatting import render_object_details
from .formatting import render_rows
from .formatting import to_json
from .index.index_opt_base import MAX_NUM_INDEX_TUNING_QUERIES
from .schema import ObjectType
from .sql import obfuscate_password

logger = logging.getLogger(__name__)

PROG = "pg-debug"

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
    parser.add_argument("--timeout", type=int, default=30, help="Statement timeout in seconds, applied in restricted mode (default: 30).")
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

    health = subparsers.add_parser("health", help="Run database health checks")
    health.add_argument(
        "-t",
        "--type",
        dest="health_type",
        default=HealthType.ALL.value,
        help="Comma-separated checks to run: " + ", ".join(sorted(t.value for t in HealthType)) + " (default: all).",
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


async def cmd_health(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.analyze_db_health(args.health_type)


async def cmd_top_queries(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.get_top_queries(sort_by=args.sort_by, limit=args.limit)


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
        return to_json(result)

    if command == "describe":
        return render_object_details(result)
    if command == "health":
        return render_mapping(result, empty="No health checks were performed.")
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
    ) as pg:
        result = await args.func(pg, args)

    print(format_result(args.command, result, args.json))

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

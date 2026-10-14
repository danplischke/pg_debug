"""Command line interface for pg_debug.

``parser`` defines the commands and their arguments, ``commands`` what each
one does, ``render`` how its result is printed, and ``streams`` how input is
read and output written. This module runs them.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from typing import Any

from ..baseline import Baseline
from ..baseline import Partition
from ..baseline import partition
from ..client import DATABASE_URL_ENV_VARS
from ..client import AccessMode
from ..client import PgDebug
from ..db import obfuscate_password
from ..errors import PgDebugError
from ..health import HealthCheckResult
from ..migration import MigrationLinter
from ..plans import diagnose_plan
from ..severity import Severity
from .config_file import resolve_context
from .parser import PROG
from .parser import UNHEALTHY_EXIT_CODE
from .parser import build_parser
from .parser import parse_index_spec
from .parser import parse_size
from .parser import split_index_columns
from .render import render_baseline_trailer
from .render import render_incomplete_health_run
from .render import to_json
from .streams import read_sql_or_file
from .streams import use_utf8_output

logger = logging.getLogger(__name__)


def format_result(args: argparse.Namespace, result: Any, baseline: Baseline | None = None) -> str:
    """Render a command's result as JSON or as text, the way its subcommand says."""
    if args.json:
        return to_json(args.json_payload(result, baseline))
    return args.render(result)


def configure_logging(verbosity: int) -> None:
    """Send library logs to stderr at a level matching ``-v`` repetition."""
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")


def _has_database_url(explicit: str | None) -> bool:
    return bool(explicit) or any(os.environ.get(var) for var in DATABASE_URL_ENV_VARS)


async def run(args: argparse.Namespace) -> int:
    """Run a parsed command, returning the process exit code."""
    if args.command == "check-migration" and args.offline:
        # Reviewing a migration against a database that is not the one it will
        # run on is worse than reviewing it against none: a table that is small
        # in staging softens every verdict about production.
        result = await MigrationLinter().review(read_sql_or_file(args.sql))
        print(format_result(args, result))
        return UNHEALTHY_EXIT_CODE if args.exit_code and any(r.severity is Severity.HIGH for r in result) else 0

    if args.command == "diagnose" and args.plan and (args.offline or not _has_database_url(args.database_url)):
        # A plan can be read with no database at all. With a URL in hand the
        # catalog is worth asking, so that path goes through the client below.
        print(format_result(args, diagnose_plan(read_sql_or_file(args.sql), min_ratio=args.min_ratio)))
        return 0

    context, source = resolve_context(args)
    if context is not None:
        # A role or a variable changes what the query sees, and a file found
        # in a parent directory is easy to forget, so every run says so.
        print(f"{PROG}: query context: {context.describe()} (from {source})", file=sys.stderr)

    async with PgDebug(
        database_url=args.database_url,
        access_mode=AccessMode(args.access_mode),
        timeout=args.timeout,
        search_path=args.search_path,
        llm_model=args.llm_model,
        context=context,
    ) as pg:
        result = await args.func(pg, args)

    if getattr(args, "save_baseline", None):
        saved = Baseline.of(args.command, result)
        saved.save(args.save_baseline)
        print(f"Saved a baseline of {len(saved)} finding(s) to {args.save_baseline}.", file=sys.stderr)

    baseline: Baseline | None = None
    split: Partition | None = None
    if getattr(args, "baseline", None):
        baseline = Baseline.load(args.baseline, args.command)
        split = partition(args.command, result, baseline)

    print(format_result(args, result, baseline))
    if split is not None and not args.json:
        print(render_baseline_trailer(args.baseline, split))

    if args.command == "health":
        return _health_exit_code(args, result, split)

    if split is not None and getattr(args, "exit_code", False):
        # Against a baseline, only what is new can fail the build.
        return UNHEALTHY_EXIT_CODE if split.new_failures else 0

    if args.command == "config" and getattr(args, "exit_code", False):
        return UNHEALTHY_EXIT_CODE if any(finding.severity is Severity.HIGH for finding in result) else 0

    if args.command == "check-migration" and getattr(args, "exit_code", False):
        return UNHEALTHY_EXIT_CODE if any(review.severity is Severity.HIGH for review in result) else 0

    if args.command == "model" and getattr(args, "exit_code", False):
        # Only high severity fails a build. The rest is advice, and a gate that
        # fires on advice gets turned off.
        return UNHEALTHY_EXIT_CODE if any(finding.severity is Severity.HIGH for finding in result) else 0

    return 0


def _health_exit_code(args: argparse.Namespace, results: dict[str, HealthCheckResult], split: Partition | None) -> int:
    """The exit status of a health run, once every check's result is printed.

    In this order:

    - ``UNHEALTHY_EXIT_CODE``, with ``--exit-code``, when a check that ran
      found a problem; against a baseline, a problem the baseline does not
      hold.
    - 1 when any check could not run, with or without ``--exit-code``: the
      run is incomplete, and a run that did not look everywhere cannot pass.
    - 0 otherwise.

    A problem found outranks an incomplete run. It stands whatever the checks
    that could not run would have said, and running again will not make it go
    away, while 1 reads as an error worth retrying. Which checks could not
    run is said on stderr either way.
    """
    incomplete = render_incomplete_health_run(results)
    if incomplete:
        if getattr(args, "save_baseline", None):
            incomplete += f" The baseline saved to {args.save_baseline} does not cover them."
        print(f"{PROG}: {incomplete}", file=sys.stderr)

    if getattr(args, "exit_code", False):
        if split is not None:
            # Against a baseline, only what is new can fail the build.
            found = split.new_failures > 0
        else:
            found = any(not check.healthy and check.error is None for check in results.values())
        if found:
            return UNHEALTHY_EXIT_CODE

    return 1 if incomplete else 0


def main(argv: Sequence[str] | None = None) -> int:
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


__all__ = [
    "PROG",
    "UNHEALTHY_EXIT_CODE",
    "build_parser",
    "format_result",
    "main",
    "parse_index_spec",
    "parse_size",
    "run",
    "split_index_columns",
]


if __name__ == "__main__":
    sys.exit(main())

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

from ..baseline import BASELINE_COMMANDS
from ..baseline import Baseline
from ..baseline import Key
from ..baseline import Partition
from ..baseline import keys_of
from ..baseline import partition
from ..client import DATABASE_URL_ENV_VARS
from ..client import AccessMode
from ..client import PgDebug
from ..db import obfuscate_password
from ..errors import InvalidArgumentError
from ..errors import PgDebugError
from ..health import HealthCheckResult
from ..logs import analyze_logs
from ..migration import MigrationLinter
from ..plans import diagnose_plan
from ..severity import Severity
from .config_file import resolve_context
from .config_file import resolve_suppressions
from .parser import PROG
from .parser import UNHEALTHY_EXIT_CODE
from .parser import build_parser
from .parser import parse_index_spec
from .parser import parse_size
from .parser import split_index_columns
from .render import Shown
from .render import render_baseline_trailer
from .render import render_incomplete_health_run
from .render import to_json
from .streams import read_sql_or_file
from .streams import use_utf8_output

logger = logging.getLogger(__name__)

#: The commands whose findings name objects with a definition to fingerprint.
FINGERPRINTED_COMMANDS = ("health", "model", "config")


def format_result(args: argparse.Namespace, result: Any, split: Partition | None = None) -> str:
    """Render a command's result as JSON or as text, the way its subcommand says.

    ``split`` is what a gate made of the result, for the commands that gate
    a build: JSON marks each finding with it.
    """
    if args.json:
        return to_json(args.json_payload(result, split))
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

    if args.command == "logs":
        # A log is read as it is; there is nothing to connect to.
        print(format_result(args, analyze_logs(args.files, prefix=args.prefix, log_format=args.log_format)))
        return 0

    if args.command == "plans" and args.exit_code and not args.baseline:
        raise InvalidArgumentError("plans --exit-code needs --baseline FILE: without one there is no accepted shape for a plan to differ from.")

    context, source = resolve_context(args)
    if context is not None:
        # A role or a variable changes what the query sees, and a file found
        # in a parent directory is easy to forget, so every run says so.
        print(f"{PROG}: query context: {context.describe()} (from {source})", file=sys.stderr)

    # What a gate compares against, read before connecting, so that a missing
    # or mistaken file fails before the run rather than after it.
    gated = args.command in BASELINE_COMMANDS
    suppressions, suppressed_from = resolve_suppressions(args)
    if suppressions:
        print(f"{PROG}: suppressing {', '.join(sorted(suppressions))} (from {suppressed_from})", file=sys.stderr)
    baseline = Baseline.load(args.baseline, args.command) if getattr(args, "baseline", None) else None

    async with PgDebug(
        database_url=args.database_url,
        access_mode=AccessMode(args.access_mode),
        timeout=args.timeout,
        search_path=args.search_path,
        llm_model=args.llm_model,
        context=context,
    ) as pg:
        result = await args.func(pg, args)
        keys = None
        if args.command in FINGERPRINTED_COMMANDS and (baseline is not None or getattr(args, "save_baseline", None)):
            keys = await _fingerprinted(pg, args.command, Shown.of(result).rows)

    # What PostgreSQL withheld from the role goes to standard error in both
    # outputs: --json keeps printing the rows the library returned, as the
    # array scripts read.
    shown = Shown.of(result)
    result = shown.rows

    # Health checks that could not run, and queries that could not be
    # planned. A baseline of such a run would hold nothing for them, so it is
    # not saved: the refusal is said, with them, once the report is printed.
    could_not_run: list[str] = []
    if args.command == "health":
        could_not_run = [name for name, check in result.items() if check.error is not None]
    elif args.command == "plans":
        could_not_run = [shape.name for shape in result if shape.error is not None]

    if getattr(args, "save_baseline", None) and not could_not_run:
        saved = Baseline.of(args.command, result, keys)
        saved.save(args.save_baseline)
        print(f"Saved a baseline of {len(saved)} finding(s) to {args.save_baseline}.", file=sys.stderr)

    split = partition(args.command, result, baseline, suppressions, keys) if gated else None

    print(format_result(args, result if args.json or shown.text is None else shown.text, split))
    if shown.note:
        print(f"{PROG}: {shown.note}", file=sys.stderr)
    if split is not None and not args.json and (baseline is not None or split.suppressed):
        print(render_baseline_trailer(args.baseline if baseline is not None else None, split, could_not_run=len(could_not_run)))

    if args.command == "health":
        return _health_exit_code(args, result, split)

    if args.command == "plans":
        return _plans_exit_code(args, could_not_run, split)

    if split is not None and getattr(args, "exit_code", False):
        # Against a baseline, only what is new or changed can fail the build,
        # and a suppressed rule never does.
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


async def _fingerprinted(pg: PgDebug, command: str, result: Any) -> list[Key]:
    """The findings' keys, each with the fingerprint of what it names where there is one."""
    keys = keys_of(command, result)
    fingerprints = await pg.definition_fingerprints(command, keys)
    return [{**key, "fingerprint": fingerprint} if fingerprint is not None else key for key, fingerprint in zip(keys, fingerprints, strict=True)]


def _plans_exit_code(args: argparse.Namespace, could_not_plan: list[str], split: Partition | None) -> int:
    """``UNHEALTHY_EXIT_CODE`` when a plan's shape changed, else 1 when a query could not be planned."""
    if could_not_plan:
        print(
            f"{PROG}: {len(could_not_plan)} {'query' if len(could_not_plan) == 1 else 'queries'} could not be planned: {', '.join(could_not_plan)}.",
            file=sys.stderr,
        )
    if args.exit_code and split is not None and split.new_failures:
        return UNHEALTHY_EXIT_CODE
    return 1 if could_not_plan else 0


def _health_exit_code(args: argparse.Namespace, results: dict[str, HealthCheckResult], split: Partition | None) -> int:
    """The exit status of a health run, once every check's result is printed.

    In this order:

    - 1 when ``--save-baseline`` was asked for and a check could not run, so
      the baseline was not saved: what was asked for was not done.
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
            # A baseline without them would fail the gate on whatever they
            # find once they run, and saved over an accepted one it would
            # throw away what was accepted, so the file is left as it was.
            incomplete += (
                f" Nothing was written to {args.save_baseline}: a baseline of this run would hold nothing for them, "
                "and whatever they find once they run would fail as new."
            )
        print(f"{PROG}: {incomplete}", file=sys.stderr)
        if getattr(args, "save_baseline", None):
            return 1

    if getattr(args, "exit_code", False):
        if split is not None:
            # Against a baseline, only what is new or changed can fail the
            # build, and a suppressed rule never does.
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

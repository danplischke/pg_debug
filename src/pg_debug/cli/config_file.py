"""Where a command finds the query context it runs the user's query with.

The commands that run or plan the user's SQL read ``pg-debug.toml`` from the
working directory, or from the nearest parent directory that has one, unless
``--config`` names another file or ``--no-config`` says to read none.
``--role`` and ``--set`` stand over whatever the file says.

The commands that gate a build read the same file for the rules it
suppresses, its ``[suppress]`` table, and take ``--suppress`` for more.
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

from ..context import CONFIG_FILE_NAME
from ..context import QueryContext
from ..errors import InvalidArgumentError
from ..rules import parse_rule_ids


def find_config_file(start: Path) -> Path | None:
    """The ``pg-debug.toml`` in ``start`` or the nearest parent that has one."""
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_FILE_NAME
        if candidate.is_file():
            return candidate
    return None


def resolve_context(args: argparse.Namespace) -> tuple[QueryContext | None, str | None]:
    """The query context a command runs with, and where it came from.

    Returns:
        The context and a description of its source — the file, the command
        line or both — or ``(None, None)`` when the command takes no context
        or there is nothing to apply.

    Raises:
        InvalidArgumentError: If the file cannot be read, or the context it
            and the command line make up is not one pg-debug applies.
    """
    if not hasattr(args, "settings"):
        return None, None

    path: Path | None = None
    if args.config:
        path = Path(args.config)
    elif not args.no_config:
        path = find_config_file(Path.cwd())
    from_file = QueryContext.from_file(path) if path is not None else QueryContext()

    overrides = dict(args.settings or [])
    context = from_file.with_overrides(role=args.role, settings=overrides)
    if not context:
        return None, None

    sources = [str(path)] if from_file else []
    if args.role is not None or overrides:
        sources.append("the command line")
    return context, " and ".join(sources)


def read_suppressions(path: Path) -> dict[str, str]:
    """The ``[suppress]`` table of a ``pg-debug.toml``: rule ids, each with why.

    ::

        [suppress]
        naive_timestamp = "Every timestamp is UTC by convention; the application never stores local time."

    A reason is required. A suppressed rule stops failing the build for good,
    and whoever reads the build log, or this file, a year later deserves to
    know why.

    Raises:
        InvalidArgumentError: If the file cannot be read, the table is not one
            of rule ids and reasons, or an id is not a rule. A misspelt id
            would otherwise suppress nothing, silently.
    """
    try:
        document = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except OSError as e:
        raise InvalidArgumentError(f"Could not read {path}: {e.strerror or e}.") from e
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise InvalidArgumentError(f"{path} is not valid TOML: {e}.") from e
    table = document.get("suppress", {})
    if not isinstance(table, dict):
        raise InvalidArgumentError(f'{path}: suppress must be a table of rule = "reason": [suppress].')
    suppressions: dict[str, str] = {}
    for rule_id, reason in table.items():
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidArgumentError(f'{path}: give the reason {rule_id} is suppressed, as {rule_id} = "why".')
        suppressions[rule_id] = reason.strip()
    parse_rule_ids(suppressions, f"{path} [suppress]")
    return suppressions


def resolve_suppressions(args: argparse.Namespace) -> tuple[dict[str, str], str | None]:
    """The rules a gate command suppresses, and where they came from.

    The ``[suppress]`` table of ``pg-debug.toml``, found as the query context's
    file is, unless ``--no-config``; ``--config FILE`` names another file;
    ``--suppress RULE`` adds to it from the command line.

    Returns:
        Each suppressed rule with its reason, and a description of the
        source, or ``({}, None)`` when nothing is suppressed.

    Raises:
        InvalidArgumentError: If the file cannot be read, or a rule is unknown.
    """
    if not hasattr(args, "suppress"):
        return {}, None
    path: Path | None = None
    if args.config:
        path = Path(args.config)
    elif not args.no_config:
        path = find_config_file(Path.cwd())

    suppressions = read_suppressions(path) if path is not None else {}
    sources = [str(path)] if suppressions else []
    given = parse_rule_ids(args.suppress or [], "--suppress")
    for rule_id in sorted(given):
        suppressions.setdefault(rule_id, "--suppress on the command line")
    if given:
        sources.append("the command line")
    return suppressions, " and ".join(sources) or None


__all__ = ["find_config_file", "read_suppressions", "resolve_context", "resolve_suppressions"]

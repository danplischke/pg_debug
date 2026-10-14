"""Where a command finds the query context it runs the user's query with.

The commands that run or plan the user's SQL read ``pg-debug.toml`` from the
working directory, or from the nearest parent directory that has one, unless
``--config`` names another file or ``--no-config`` says to read none.
``--role`` and ``--set`` stand over whatever the file says.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..context import CONFIG_FILE_NAME
from ..context import QueryContext


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


__all__ = ["find_config_file", "resolve_context"]

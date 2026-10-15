"""The role and settings the user's queries run with.

pg-debug connects as whoever the connection URL names, often the owner of the
tables. An application seldom does: it connects as a role of its own and sets
variables for its session, such as a tenant id that a row-level security
policy reads, or a ``work_mem``, and the planner sees all of it. Run as the
owner, a query the application gets an index scan for plans as a sequential
scan, because owners bypass row-level security; and ``EXPLAIN ANALYZE`` of a
query whose policy reads a variable nobody set fails outright.

A :class:`QueryContext` declares that role and those settings. The driver
applies it inside the transaction of each statement that runs or plans the
user's query, with ``set_config(..., true)``, so it ends with that transaction:
nothing outlives it on a pooled connection, and pg-debug's own reads of the
catalog still see the database as the connecting user. A ``pg-debug.toml``
holds one, so that it is declared once:

    [context]
    role = "app_user"

    [context.settings]
    "app.tenant_id" = "t7"
    work_mem = "64MB"
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .errors import InvalidArgumentError

#: The file the command line reads a query context from.
CONFIG_FILE_NAME = "pg-debug.toml"

#: Settings pg-debug sets from options of their own. A context that set them
#: too would quietly undo the option: a statement_timeout of 0 would lift the
#: limit restricted mode promises.
_OWNED_SETTINGS = {
    "statement_timeout": "it comes from --timeout (timeout= in the library), so that restricted mode's limit holds for every statement",
    "search_path": "it comes from --search-path (search_path= in the library), which pg-debug also resolves table names through",
    "role": "give the context a role instead",
    "session_authorization": "give the context a role instead",
    "transaction_read_only": "whether a statement may write is decided by --access-mode",
    "default_transaction_read_only": "whether a statement may write is decided by --access-mode",
}

#: A value shown bare in :meth:`QueryContext.describe` reads unambiguously.
_BARE_VALUE = re.compile(r"[^\s,']+")


@dataclass(frozen=True)
class QueryContext:
    """The role and settings to run the user's queries with.

    Applied to the statements that run or plan SQL the caller handed over —
    executing it, explaining it, diagnosing it, checking its index use and
    tuning indexes for it — and to nothing pg-debug asks the catalog for
    itself.

    Attributes:
        role: Run as this role, as ``SET ROLE`` would. The connecting user has
            to be a member of it. None runs as the connecting user.
        settings: Values by setting name, as ``set_config`` takes them: text,
            in the units the setting reads (``"64MB"``). Custom variables such
            as ``app.tenant_id`` and server settings such as ``work_mem``
            alike. ``statement_timeout``, ``search_path``, the role and the
            read-only settings have options of their own and are refused.

    Raises:
        InvalidArgumentError: If the role is empty, a value is not text, a
            setting is given twice, or a setting is one pg-debug sets itself.
    """

    role: str | None = None
    settings: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.role is not None and (not isinstance(self.role, str) or not self.role):
            raise InvalidArgumentError("The query context's role must be a role name. Leave it out to run as the connecting user.")

        settings = dict(self.settings)
        seen: dict[str, str] = {}
        for name, value in settings.items():
            if not isinstance(name, str) or not name:
                raise InvalidArgumentError(f"A setting in the query context has no name: {name!r}.")
            if not isinstance(value, str):
                raise InvalidArgumentError(f"The value of {name} must be text, as set_config takes it, not {type(value).__name__}.")
            key = name.lower()
            if key in _OWNED_SETTINGS:
                raise InvalidArgumentError(f"{name} cannot be set in the query context: {_OWNED_SETTINGS[key]}.")
            # PostgreSQL's setting names ignore case, so these would be one
            # setting with two values and no saying which wins.
            if key in seen:
                raise InvalidArgumentError(f"{seen[key]} and {name} are the same setting. Give it once.")
            seen[key] = name

        # A copy nobody can change, so that what was checked is what runs.
        object.__setattr__(self, "settings", MappingProxyType(settings))

    def __hash__(self) -> int:
        return hash((self.role, tuple(self.settings.items())))

    def __bool__(self) -> bool:
        """Whether there is anything to apply."""
        return self.role is not None or bool(self.settings)

    def describe(self) -> str:
        """The role and settings in one line, to say what a result was produced under.

        ``role app_user, app.tenant_id=t7, work_mem=64MB``
        """
        parts = [f"role {self.role}"] if self.role is not None else []
        parts.extend(f"{name}={_shown(value)}" for name, value in self.settings.items())
        return ", ".join(parts)

    def with_overrides(self, role: str | None = None, settings: Mapping[str, str] | None = None) -> QueryContext:
        """This context with a role and settings given later in place of its own.

        That is how the command line's ``--role`` and ``--set`` stand over a
        file's. A setting replaces the one of the same name, whatever the case
        either is written in.
        """
        overrides = dict(settings or {})
        replaced = {name.lower() for name in overrides}
        merged = {name: value for name, value in self.settings.items() if name.lower() not in replaced}
        merged.update(overrides)
        return QueryContext(role=self.role if role is None else role, settings=merged)

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> QueryContext:
        """Read the ``[context]`` table of a ``pg-debug.toml``.

        A setting's name can be written quoted, ``"app.tenant_id" = "t7"``, or
        as TOML's dotted key, ``app.tenant_id = "t7"``; both name the same
        setting. A number or a boolean becomes the text ``set_config`` takes:
        ``true`` becomes ``"true"``. Anything the file holds besides a context
        is an error, so that a misspelt key is not silently ignored.

        Returns:
            The context, empty if the file declares none.

        Raises:
            InvalidArgumentError: If the file cannot be read or is not TOML, if
                it holds anything but a context, or if the context is not one
                pg-debug applies. The message names the file.
        """
        path = Path(path)
        try:
            # TOML is UTF-8. The -sig is for the byte order mark some Windows
            # editors put first, which tomllib would read as a syntax error.
            text = path.read_bytes().decode("utf-8-sig")
        except OSError as e:
            raise InvalidArgumentError(f"Could not read {path}: {e.strerror or e}.") from e
        except UnicodeDecodeError as e:
            raise InvalidArgumentError(f"{path} is not UTF-8, which TOML has to be: {e}.") from e
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise InvalidArgumentError(f"{path} is not valid TOML: {e}.") from e

        try:
            return cls._from_document(document)
        except InvalidArgumentError as e:
            raise InvalidArgumentError(f"{path}: {e}") from e

    @classmethod
    def _from_document(cls, document: dict[str, Any]) -> QueryContext:
        # [suppress] is the gate's, read by the commands that gate a build.
        unknown = sorted(set(document) - {"context", "suppress"})
        if unknown:
            raise InvalidArgumentError(
                f"Unknown key {', '.join(unknown)}. The file holds a [context] table, with a role and settings, and a [suppress] table of rules."
            )

        context = document.get("context", {})
        if not isinstance(context, dict):
            raise InvalidArgumentError("context must be a table: [context].")
        unknown = sorted(set(context) - {"role", "settings"})
        if unknown:
            raise InvalidArgumentError(f"Unknown key {', '.join(unknown)} in [context]. It takes role and settings.")

        role = context.get("role")
        if role is not None and not isinstance(role, str):
            raise InvalidArgumentError(f"role must be a string, not {type(role).__name__}.")

        table = context.get("settings", {})
        if not isinstance(table, dict):
            raise InvalidArgumentError("settings must be a table of name = value: [context.settings].")
        settings: dict[str, str] = {}
        for name, value in _flatten(table):
            if name in settings:
                raise InvalidArgumentError(f"{name} is set twice in [context.settings].")
            settings[name] = _setting_text(name, value)

        return cls(role=role, settings=settings)


def _flatten(table: dict[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Setting names and values, with TOML's dotted keys joined back into names.

    ``app.tenant_id = "t7"`` is, to TOML, a table ``app`` holding
    ``tenant_id``, and ``app.tenant_id`` is the setting it means.
    """
    for key, value in table.items():
        if isinstance(value, dict):
            yield from _flatten(value, f"{prefix}{key}.")
        else:
            yield f"{prefix}{key}", value


def _setting_text(name: str, value: object) -> str:
    """A TOML value as the text ``set_config`` takes."""
    # Before int: a bool is an int, and str(True) is not how PostgreSQL or an
    # application comparing the text would write it.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    raise InvalidArgumentError(f"{name} must be a string, a number or a boolean, not {type(value).__name__}.")


def _shown(value: str) -> str:
    """A value as :meth:`QueryContext.describe` shows it, quoted where bare would mislead."""
    if _BARE_VALUE.fullmatch(value):
        return value
    return "'" + value.replace("'", "''") + "'"


__all__ = ["CONFIG_FILE_NAME", "QueryContext"]

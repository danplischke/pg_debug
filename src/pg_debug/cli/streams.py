"""Reading the CLI's input and writing its output as UTF-8, on every platform."""

from __future__ import annotations

import io
import sys
from pathlib import Path

from ..errors import InvalidArgumentError


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

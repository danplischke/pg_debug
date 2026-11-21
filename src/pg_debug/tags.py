"""Tags an application put in a statement's text, as sqlcommenter writes them.

sqlcommenter (https://google.github.io/sqlcommenter/) appends one comment to
each statement: ``SELECT ... /*action='list',controller='orders',
traceparent='00-...-01'*/``. Each key and value is URL-encoded, each value is
in single quotes with a quote inside it escaped by a backslash, and the pairs
are separated by commas. Django, Rails (marginalia, and Rails' own query
logs), Spring, Sequelize, Knex and the OpenTelemetry instrumentations write it,
and so can anything else by hand.

The tags say who sent a statement: the route, the controller, the trace. They
are only as good as the text that carries them, and the statistics views
group statements with their comments ignored: ``pg_stat_statements`` keeps the
text of whichever run it saw first, so its tags are that caller's, not every
caller's. ``pg_stat_activity`` and a log line hold the text each run was sent
with.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

#: The comment at the end of a statement: whatever follows the last ``/*`` that
#: is closed, with only a semicolon and space after it.
_TRAILING_COMMENT = re.compile(r"/\*(?P<body>(?:(?!\*/).)*)\*/\s*;?\s*$", re.DOTALL)

#: One ``key='value'`` pair: a value is single-quoted, and a quote inside it is
#: escaped with a backslash.
_PAIR = re.compile(r"\s*(?P<key>[^=,'\s]+)\s*=\s*'(?P<value>(?:\\.|[^'\\])*)'\s*(?:,|$)")


def parse_sql_comment(sql: str | None) -> dict[str, str]:
    """The sqlcommenter tags at the end of a statement, decoded; empty when there are none.

    A trailing comment that is not entirely ``key='value'`` pairs (a hint, a
    note, pg-debug's own ``/* pg_debug */``) is not tags, and reads as none.
    """
    if not sql:
        return {}
    match = _TRAILING_COMMENT.search(sql)
    if match is None:
        return {}
    body = match.group("body").strip()
    if not body:
        return {}
    tags: dict[str, str] = {}
    position = 0
    while position < len(body):
        pair = _PAIR.match(body, position)
        if pair is None or pair.end() == position:
            return {}
        value = re.sub(r"\\(.)", r"\1", pair.group("value"))
        tags[unquote(pair.group("key"))] = unquote(value)
        position = pair.end()
    return tags


def describe_tags(tags: dict[str, str]) -> str:
    """The tags in one line, the ones that name a caller first: ``route=/orders controller=orders``."""
    order = ("application", "route", "controller", "action", "framework", "db_driver", "traceparent", "tracestate")
    keys = [key for key in order if key in tags] + sorted(key for key in tags if key not in order)
    return " ".join(f"{key}={tags[key]}" for key in keys)


__all__ = ["describe_tags", "parse_sql_comment"]

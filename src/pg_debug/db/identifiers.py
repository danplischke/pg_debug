"""Table references and identifiers, as PostgreSQL reads them."""

import re

from psycopg.sql import Identifier

#: PostgreSQL truncates identifiers longer than NAMEDATALEN - 1 bytes.
MAX_IDENTIFIER_BYTES = 63

_IDENTIFIER_UNSAFE = re.compile(r"[^A-Za-z0-9_]+")


def identifier_part(text: str) -> str:
    """Reduce arbitrary SQL text to something usable inside an identifier."""
    return _IDENTIFIER_UNSAFE.sub("_", text).strip("_")


def table_identifier(table: str) -> Identifier:
    """Return a table reference as a quoted SQL identifier.

    ``"reporting.orders"`` becomes ``"reporting"."orders"``, so the name can be
    interpolated into a statement without risking injection.
    """
    schema, name = split_table_reference(table)
    return Identifier(schema, name) if schema else Identifier(name)


def split_table_reference(table: str) -> tuple[str | None, str]:
    """Split a table reference into its schema and table name.

    Args:
        table: Either ``"orders"`` or ``"reporting.orders"``.

    Returns:
        ``(schema, table)``, with schema None for an unqualified reference. An
        unquoted identifier that itself contains a dot cannot be represented
        and is treated as schema-qualified.
    """
    schema, _, name = table.rpartition(".")
    return (schema or None), name

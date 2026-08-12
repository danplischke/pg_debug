import re
from dataclasses import dataclass
from hashlib import sha1
from typing import Any
from typing import Optional
from typing import Tuple

from psycopg.sql import Identifier

#: PostgreSQL truncates identifiers longer than NAMEDATALEN - 1 bytes.
MAX_IDENTIFIER_BYTES = 63

_IDENTIFIER_UNSAFE = re.compile(r"[^A-Za-z0-9_]+")


def _identifier_part(text: str) -> str:
    """Reduce arbitrary SQL text to something usable inside an identifier."""
    return _IDENTIFIER_UNSAFE.sub("_", text).strip("_")


def table_identifier(table: str) -> Identifier:
    """Return a table reference as a quoted SQL identifier.

    ``"reporting.orders"`` becomes ``"reporting"."orders"``, so the name can be
    interpolated into a statement without risking injection.
    """
    schema, name = split_table_reference(table)
    return Identifier(schema, name) if schema else Identifier(name)


def split_table_reference(table: str) -> Tuple[Optional[str], str]:
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


@dataclass(frozen=True)
class IndexDefinition:
    """Immutable index configuration for hashing."""

    table: str
    columns: tuple[str, ...]
    using: str = "btree"

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "columns": list(self.columns),
            "using": self.using,
            "definition": self.definition,
        }

    @property
    def definition(self) -> str:
        return f"CREATE INDEX {self.name} ON {self.table} USING {self.using} ({', '.join(self.columns)})"

    @property
    def name(self) -> str:
        # An index column may be an expression such as lower(email) or
        # date_trunc('day', created_at), so everything that is not valid in an
        # unquoted identifier becomes an underscore.
        cleaned_columns = [_identifier_part(col) for col in self.columns]

        column_part = "_".join(cleaned_columns)
        suffix = "" if self.using == "btree" else f"_{self.using}"
        # A schema-qualified table would otherwise put a dot in the index name,
        # which is not a valid identifier.
        table_part = _identifier_part(self.table)
        base = f"pg_debug_idx_{table_part}_{column_part}_{len(self.columns)}{suffix}"

        if len(base.encode()) <= MAX_IDENTIFIER_BYTES:
            return base

        # PostgreSQL truncates identifiers past NAMEDATALEN, which would let two
        # long expressions collapse onto one name. Keep the readable prefix and
        # end with a digest of the full name so they stay distinct.
        digest = sha1(base.encode(), usedforsecurity=False).hexdigest()[:8]
        keep = MAX_IDENTIFIER_BYTES - len(digest) - 1
        return f"{base.encode()[:keep].decode(errors='ignore')}_{digest}"

    def __str__(self) -> str:
        return self.definition

    def __repr__(self) -> str:
        return f"IndexConfig(table='{self.table}', columns={self.columns}, using='{self.using}')"

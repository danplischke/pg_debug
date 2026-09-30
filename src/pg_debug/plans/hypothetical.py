"""Index definitions, for planning against indexes that do not exist.

HypoPG takes a ``CREATE INDEX`` statement and makes the planner believe the
index is there. :class:`IndexDefinition` is how pg_debug describes such an
index, names it, and writes the statement for it.
"""

from dataclasses import dataclass
from hashlib import sha1
from typing import Any

from ..db.identifiers import MAX_IDENTIFIER_BYTES
from ..db.identifiers import identifier_part


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
        cleaned_columns = [identifier_part(col) for col in self.columns]

        column_part = "_".join(cleaned_columns)
        suffix = "" if self.using == "btree" else f"_{self.using}"
        # A schema-qualified table would otherwise put a dot in the index name,
        # which is not a valid identifier.
        table_part = identifier_part(self.table)
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

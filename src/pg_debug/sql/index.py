from dataclasses import dataclass
from typing import Any
from typing import Optional
from typing import Tuple

from psycopg.sql import Identifier


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
        # Clean column names for use in index naming
        # Replace special characters with underscores to avoid issues with
        # functional expressions
        cleaned_columns = []
        for col in self.columns:
            # Replace parentheses and other special characters with underscores
            # This ensures expressions like LOWER(column_name) work in
            # index names
            cleaned_col = col.replace("(", "_").replace(")", "_").replace(" ", "_").replace(",", "_")
            # Remove consecutive underscores
            while "__" in cleaned_col:
                cleaned_col = cleaned_col.replace("__", "_")
            # Remove trailing underscores
            cleaned_col = cleaned_col.rstrip("_")
            cleaned_columns.append(cleaned_col)

        column_part = "_".join(cleaned_columns)
        suffix = "" if self.using == "btree" else f"_{self.using}"
        # A schema-qualified table would otherwise put a dot in the index name,
        # which is not a valid identifier.
        table_part = self.table.replace(".", "_").replace('"', "")
        base = f"pg_debug_idx_{table_part}_{column_part}_{len(self.columns)}"
        return f"{base}{suffix}"

    def __str__(self) -> str:
        return self.definition

    def __repr__(self) -> str:
        return f"IndexConfig(table='{self.table}', columns={self.columns}, using='{self.using}')"

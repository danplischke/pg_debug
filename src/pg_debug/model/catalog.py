"""Catalog SQL every rule needs: which schemas count, and how to narrow to one."""

from __future__ import annotations

from typing import Any
from typing import LiteralString
from typing import cast

from .result import ReviewOptions


def user_schemas(alias: str = "n") -> str:
    """A predicate keeping only schemas that belong to somebody's data model.

    Args:
        alias: The alias ``pg_namespace`` is joined under in the query.
    """
    return f"{alias}.nspname <> 'information_schema' AND {alias}.nspname NOT LIKE 'pg\\_%'"


def scoped(sql: str, options: ReviewOptions, column: str) -> tuple[LiteralString, list[Any]]:
    """Narrow a rule's query to one schema, when one was asked for.

    Args:
        sql: The rule's query, ending in a WHERE clause it can be appended to.
        options: The review options, read for :attr:`ReviewOptions.schema`.
        column: The qualified schema-name column to compare against.

    Returns:
        The query and the parameters to pass with it.
    """
    # Every rule's query is assembled from module constants and fixed catalog
    # column names, which is what makes the cast sound. The one value that
    # comes from outside — the schema — goes through a bind parameter.
    if options.schema is None:
        return cast(LiteralString, sql), []
    return cast(LiteralString, sql + f"  AND {column} = {{}}\n"), [options.schema]


__all__ = ["scoped", "user_schemas"]

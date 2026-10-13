from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from typing import ClassVar
from typing import LiteralString

import pglast
from pglast.ast import A_Const
from pglast.ast import A_Expr
from pglast.ast import CreateExtensionStmt
from pglast.ast import DefElem
from pglast.ast import ExplainStmt
from pglast.ast import FuncCall
from pglast.ast import Node
from pglast.ast import RawStmt
from pglast.ast import SelectStmt
from pglast.enums import A_Expr_Kind
from psycopg.sql import SQL
from psycopg.sql import Composable
from psycopg.sql import Literal

from . import allowlist
from .driver import QUERY_MARKER
from .driver import SqlDriver

logger = logging.getLogger(__name__)

__all__ = ["QUERY_MARKER", "SafeSqlDriver"]


class SafeSqlDriver(SqlDriver):
    """A wrapper around any SqlDriver that only allows SELECT, ANALYZE, VACUUM, EXPLAIN SELECT, and
    SHOW queries.

    Uses pglast to parse and validate SQL statements before execution.
    All other statement types (DDL, DML etc) are rejected.
    Performs deep validation of the query tree to prevent unsafe operations.
    """

    # Pattern to match pg_catalog schema qualification
    PG_CATALOG_PATTERN = re.compile(r"^pg_catalog\.(.+)$")
    # Pattern to validate LIKE expressions - must either start with % or end with %, but not both
    LIKE_PATTERN = re.compile(r"^[^%]+%$")

    ALLOWED_STMT_TYPES: ClassVar[set[type]] = allowlist.ALLOWED_STMT_TYPES
    ALLOWED_FUNCTIONS: ClassVar[set[str]] = allowlist.ALLOWED_FUNCTIONS
    ALLOWED_NODE_TYPES: ClassVar[set[type]] = allowlist.ALLOWED_NODE_TYPES
    ALLOWED_EXTENSIONS: ClassVar[set[str]] = allowlist.ALLOWED_EXTENSIONS

    def __init__(self, sql_driver: SqlDriver, timeout: float | None = None):
        """Initialize with an underlying SQL driver and optional timeout.

        Args:
            sql_driver: The underlying SQL driver to wrap
            timeout: Optional timeout in seconds for query execution
        """
        self.sql_driver = sql_driver
        self.timeout = timeout

    @asynccontextmanager
    async def session(self) -> AsyncIterator[SqlDriver]:
        """Pin the wrapped driver's connection, so restricted mode behaves the same."""
        async with self.sql_driver.session():
            yield self

    def _validate_node(self, node: Node) -> None:
        """Recursively validate a node and all its children"""
        # Check if node type is allowed
        if not isinstance(node, tuple(self.ALLOWED_NODE_TYPES)):
            raise ValueError(f"Node type {type(node)} is not allowed")

        # Validate LIKE patterns
        if isinstance(node, A_Expr) and node.kind in (
            A_Expr_Kind.AEXPR_LIKE,
            A_Expr_Kind.AEXPR_ILIKE,
        ):
            # Get the right-hand side of the LIKE expression (the pattern)
            if isinstance(node.rexpr, A_Const) and node.rexpr.val is not None and hasattr(node.rexpr.val, "sval") and node.rexpr.val.sval is not None:
                # Nothing to do for now
                pass
            else:
                raise ValueError("LIKE pattern must be a constant string")

        # Validate function calls
        if isinstance(node, FuncCall):
            func_name = ".".join([str(n.sval) for n in node.funcname]).lower() if node.funcname else ""
            # Strip pg_catalog schema if present
            match = self.PG_CATALOG_PATTERN.match(func_name)
            unqualified_name = match.group(1) if match else func_name
            if unqualified_name not in self.ALLOWED_FUNCTIONS:
                raise ValueError(f"Function {func_name} is not allowed")

        # Reject SELECT statements with locking clauses
        if isinstance(node, SelectStmt) and getattr(node, "lockingClause", None):
            raise ValueError("Locking clause on select is prohibited")

        # Reject EXPLAIN ANALYZE statements
        if isinstance(node, ExplainStmt):
            for option in node.options or []:
                if isinstance(option, DefElem) and option.defname == "analyze":
                    raise ValueError("EXPLAIN ANALYZE is not supported")

        # Reject CREATE EXTENSION statements
        if isinstance(node, CreateExtensionStmt):
            if node.extname not in self.ALLOWED_EXTENSIONS:
                raise ValueError(f"CREATE EXTENSION {node.extname} is not supported")

        # Recursively validate all attributes that might be nodes
        for attr_name in node.__slots__:
            # Skip private attributes and methods
            if attr_name.startswith("_"):
                continue

            try:
                attr = getattr(node, attr_name)
            except AttributeError:
                # Skip attributes that don't exist (this is normal in pglast)
                continue

            # Handle lists of nodes
            if isinstance(attr, list):
                for item in attr:
                    if isinstance(item, Node):
                        self._validate_node(item)

            # Handle tuples of nodes
            elif isinstance(attr, tuple):
                for item in attr:
                    if isinstance(item, Node):
                        self._validate_node(item)

            # Handle single nodes
            elif isinstance(attr, Node):
                self._validate_node(attr)

    def _validate(self, query: str) -> None:
        """Validate query is safe to execute"""
        try:
            # Parse the SQL using pglast
            parsed = pglast.parse_sql(query)
            # Pretty print the parsed SQL for debugging
            # print("Parsed SQL:")
            # import pprint
            # pprint.pprint(parsed)

            # Validate each statement
            try:
                for stmt in parsed:
                    if isinstance(stmt, RawStmt):
                        # Check if the inner statement type is allowed
                        if not isinstance(stmt.stmt, tuple(self.ALLOWED_STMT_TYPES)):
                            raise ValueError(
                                "Only SELECT, ANALYZE, VACUUM, EXPLAIN, SHOW and other read-only statements are allowed. Received raw statement: "
                                + str(stmt.stmt)
                            )
                    else:
                        if not isinstance(stmt, tuple(self.ALLOWED_STMT_TYPES)):
                            raise ValueError(
                                "Only SELECT, ANALYZE, VACUUM, EXPLAIN, SHOW and other read-only statements are allowed. Received: " + str(stmt)
                            )
                    self._validate_node(stmt)
            except Exception as e:
                raise ValueError(f"Error validating query: {query}") from e

        except pglast.parser.ParseError as e:
            raise ValueError("Failed to parse SQL statement") from e

    async def execute_query(
        self,
        query: LiteralString,
        params: list[Any] | None = None,
        force_readonly: bool = True,  # do not use value passed in
    ) -> list[SqlDriver.RowResult] | None:  # noqa: UP007
        """Execute a query after validating it is safe"""
        self._validate(query)

        # NOTE: Always force readonly=True in SafeSqlDriver regardless of what was passed
        if self.timeout:
            try:
                async with asyncio.timeout(self.timeout):
                    # The wrapped driver tags the statement; doing it here too
                    # would leave restricted mode with the marker twice.
                    return await self.sql_driver.execute_query(
                        query,
                        params=params,
                        force_readonly=True,
                    )
            except TimeoutError as e:
                logger.warning(f"Query execution timed out after {self.timeout} seconds: {query[:100]}...")
                raise ValueError(
                    f"Query execution timed out after {self.timeout} seconds in restricted mode. "
                    "Consider simplifying your query or increasing the timeout."
                ) from e
            except Exception as e:
                logger.error(f"Error executing query: {e}")
                raise
        else:
            return await self.sql_driver.execute_query(
                query,
                params=params,
                force_readonly=True,
            )

    async def execute_query_and_rollback(self, query: LiteralString, params: list[Any] | None = None) -> list[SqlDriver.RowResult] | None:
        """Always refuses. Restricted mode does not run statements to see what they do.

        Inherited unchanged this would be a hole rather than a feature: the base
        implementation talks to the connection directly and would bypass
        validation entirely. Rolling a statement back afterwards is not the same
        promise as never having run it, and restricted mode makes the second
        one.
        """
        raise ValueError(
            "Restricted mode cannot run a statement in order to observe it. "
            "Use --access-mode unrestricted, on a database where executing the statement is acceptable."
        )

    @staticmethod
    def sql_to_query(sql: Composable) -> str:
        """Convert a SQL string to a query string."""
        return sql.as_string()

    @staticmethod
    def param_sql_to_query(query: str, params: list[Any]) -> str:
        """Convert a SQL string to a query string."""
        # Convert each parameter to a Literal only if it's not already a Composable
        sql_params = [p if isinstance(p, Composable) else Literal(p) for p in params]

        return SafeSqlDriver.sql_to_query(
            SQL(query).format(*sql_params)  # type: ignore
        )

    @staticmethod
    async def execute_param_query(sql_driver: SqlDriver, query: LiteralString, params: list[Any] | None = None) -> list[SqlDriver.RowResult] | None:
        """Execute a query after validating it is safe"""
        if params:
            query_params = SafeSqlDriver.param_sql_to_query(query, params)
            return await sql_driver.execute_query(query_params)  # type: ignore
        else:
            return await sql_driver.execute_query(query)

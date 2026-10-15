from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
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

from ..errors import QueryError
from . import allowlist
from .driver import QUERY_MARKER
from .driver import SqlDriver

if TYPE_CHECKING:
    from ..context import QueryContext

logger = logging.getLogger(__name__)

__all__ = ["QUERY_MARKER", "SafeSqlDriver"]


def _refused(reason: str) -> QueryError:
    """The error for a query restricted mode will not run, with the reason it will not."""
    return QueryError(f"Restricted mode refused the query: {reason}.")


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

    @property
    def context(self) -> QueryContext | None:
        """The wrapped driver's query context, which is the one that is applied."""
        return self.sql_driver.context

    def with_context(self, context: QueryContext | None) -> SafeSqlDriver:
        """The same restrictions over the wrapped driver with another query context."""
        return SafeSqlDriver(self.sql_driver.with_context(context), timeout=self.timeout)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[SqlDriver]:
        """Pin the wrapped driver's connection, so restricted mode behaves the same."""
        async with self.sql_driver.session():
            yield self

    async def check_context(self) -> None:
        """Check the wrapped driver's query context, which is the one that is applied."""
        await self.sql_driver.check_context()

    def _validate_node(self, node: Node) -> None:
        """Recursively validate a node and all its children"""
        # Check if node type is allowed
        if not isinstance(node, tuple(self.ALLOWED_NODE_TYPES)):
            raise _refused(f"{type(node).__name__} is not allowed")

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
                raise _refused("a LIKE pattern must be a constant string")

        # Validate function calls
        if isinstance(node, FuncCall):
            func_name = ".".join([str(n.sval) for n in node.funcname]).lower() if node.funcname else ""
            # Strip pg_catalog schema if present
            match = self.PG_CATALOG_PATTERN.match(func_name)
            unqualified_name = match.group(1) if match else func_name
            if unqualified_name not in self.ALLOWED_FUNCTIONS:
                raise _refused(f"function {func_name} is not allowed")

        # Reject SELECT statements with locking clauses
        if isinstance(node, SelectStmt) and getattr(node, "lockingClause", None):
            raise _refused("FOR UPDATE and FOR SHARE take row locks")

        # Reject EXPLAIN ANALYZE statements
        if isinstance(node, ExplainStmt):
            for option in node.options or []:
                if isinstance(option, DefElem) and option.defname == "analyze":
                    raise _refused("EXPLAIN ANALYZE runs the statement it explains")

        # Reject CREATE EXTENSION statements
        if isinstance(node, CreateExtensionStmt):
            if node.extname not in self.ALLOWED_EXTENSIONS:
                raise _refused(f"CREATE EXTENSION {node.extname} is not an extension it may create")

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

            self._validate_children(attr)

    def _validate_children(self, value: Any) -> None:
        """Validate every node in an attribute, however deeply pglast nests it.

        Some attributes are sequences of sequences: a function in FROM is
        ``RangeFunction.functions = ((FuncCall, None),)``, and the rows of a
        ``VALUES`` list are ``SelectStmt.valuesLists``. A walk that looked one
        level into a sequence never reached the calls inside, so
        ``SELECT * FROM pg_terminate_backend(...)`` ran a function the
        allowlist refuses in the select list.
        """
        if isinstance(value, Node):
            self._validate_node(value)
        elif isinstance(value, list | tuple):
            for item in value:
                self._validate_children(item)

    def _validate(self, query: str) -> None:
        """Refuse the query unless every statement in it is read-only.

        Raises:
            QueryError: Naming the first thing restricted mode refuses. A query
                it cannot parse, or that the validator itself fails on, is
                refused too: restricted mode does not run what it could not check.
        """
        try:
            parsed = pglast.parse_sql(query)
        except pglast.parser.ParseError as e:
            raise _refused(f"it could not be parsed ({e})") from e
        try:
            for stmt in parsed:
                statement = stmt.stmt if isinstance(stmt, RawStmt) else stmt
                if not isinstance(statement, tuple(self.ALLOWED_STMT_TYPES)):
                    raise _refused(f"{type(statement).__name__} is not a read-only statement; it runs SELECT, EXPLAIN, SHOW, VACUUM and ANALYZE")
                self._validate_node(stmt)
        except QueryError:
            raise
        except Exception as e:
            raise _refused(f"it could not be validated ({e})") from e

    async def execute_query(
        self,
        query: LiteralString,
        params: list[Any] | None = None,
        force_readonly: bool = True,  # do not use value passed in
        *,
        in_context: bool = False,
        cleanup: LiteralString | None = None,
    ) -> list[SqlDriver.RowResult] | None:  # noqa: UP007
        """Execute a query after validating it is safe.

        The query context, with ``in_context``, is applied by the wrapped
        driver after the query has passed: it is pg-debug's own statement,
        and it sets a role and settings, which writes nothing. The settings
        restricted mode depends on are ones a context cannot hold. A
        ``cleanup`` is validated like the query.
        """
        self._validate(query)
        if cleanup is not None:
            self._validate(cleanup)

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
                        in_context=in_context,
                        cleanup=cleanup,
                    )
            except TimeoutError as e:
                # One line, with the statement only at debug, as the wrapped
                # driver logs one that failed. A failure in the wrapped
                # driver is logged there, so it is not logged here again.
                logger.warning("Query execution timed out after %s seconds", self.timeout)
                logger.debug("The statement that timed out: %s", query)
                raise QueryError(
                    f"Query execution timed out after {self.timeout} seconds in restricted mode. "
                    "Consider simplifying your query or increasing the timeout."
                ) from e
        else:
            return await self.sql_driver.execute_query(
                query,
                params=params,
                force_readonly=True,
                in_context=in_context,
                cleanup=cleanup,
            )

    async def execute_query_and_rollback(
        self,
        query: LiteralString,
        params: list[Any] | None = None,
        *,
        in_context: bool = False,
    ) -> list[SqlDriver.RowResult] | None:
        """Always refuses. Restricted mode does not run statements to see what they do.

        Inherited unchanged this would be a hole rather than a feature: the base
        implementation talks to the connection directly and would bypass
        validation entirely. Rolling a statement back afterwards is not the same
        promise as never having run it, and restricted mode makes the second
        one.
        """
        raise _refused(
            "it would have to run the statement to observe it, and a rollback afterwards is not the same as never running it. "
            "Use --access-mode unrestricted, on a database where executing the statement is acceptable"
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

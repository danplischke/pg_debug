"""SQL driver adapter for PostgreSQL connections."""

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from typing import LiteralString
from urllib.parse import urlparse
from urllib.parse import urlunparse

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL
from psycopg.sql import Composed
from psycopg.sql import Identifier
from psycopg.sql import Literal
from psycopg_pool import AsyncConnectionPool

from ..errors import ConnectionFailedError
from ..errors import InvalidArgumentError
from ..errors import NotConnectedError
from ..errors import QueryError

logger = logging.getLogger(__name__)

#: Prefixed to every statement this library issues, so its own bookkeeping can
#: be told apart from the workload it is reporting on. PostgreSQL keeps a
#: leading comment in the text ``pg_stat_statements`` records, which is what
#: makes this work at all.
#:
#: It identifies a statement; it does not partition one. ``pg_stat_statements``
#: groups by a hash of the parsed statement, and comments are no part of that,
#: so a query pg_debug and the application both run shares a single entry whose
#: recorded text is whichever of the two arrived first.
QUERY_MARKER = "/* pg_debug */"

#: Statement keywords that only ever describe transaction bookkeeping.
TRANSACTION_CONTROL = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "END", "START", "SAVEPOINT", "RELEASE"})


def workload_only() -> str:
    """SQL for "this row is the workload, not pg_debug's own bookkeeping".

    Belongs with :data:`QUERY_MARKER` because it is the other half of it: the
    marker is what the driver writes, this is how a reader gets rid of it.
    Applied inside the statement rather than after it, so that pg_debug's own
    queries cannot take up half of a ``LIMIT 10``. The marker covers everything
    the driver runs; transaction control has to be matched by keyword, because
    a bare BEGIN issued by the driver carries no comment of its own.
    """
    # An underscore is a LIKE wildcard, and the marker contains one.
    marker = QUERY_MARKER.replace("_", r"\_")
    keywords = ", ".join(f"'{keyword}'" for keyword in sorted(TRANSACTION_CONTROL))
    return f"query NOT LIKE '{marker}%' AND upper(split_part(btrim(query), ' ', 1)) NOT IN ({keywords})"


def obfuscate_password(text: str | None) -> str | None:
    """
    Obfuscate password in any text containing connection information.
    Works on connection URLs, error messages, and other strings.
    """
    if text is None:
        return None

    if not text:
        return text

    # Try first as a proper URL
    try:
        parsed = urlparse(text)
        if parsed.scheme and parsed.netloc and parsed.password:
            # Replace password with asterisks in proper URL
            netloc = parsed.netloc.replace(parsed.password, "****")
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass

    # Handle strings that contain connection strings but aren't proper URLs
    # Match postgres://user:password@host:port/dbname pattern
    url_pattern = re.compile(r"(postgres(?:ql)?:\/\/[^:]+:)([^@]+)(@[^\/\s]+)")
    text = re.sub(url_pattern, r"\1****\3", text)

    # Match connection string parameters (password=xxx)
    # This simpler pattern captures password without quotes
    param_pattern = re.compile(r'(password=)([^\s&;"\']+)', re.IGNORECASE)
    text = re.sub(param_pattern, r"\1****", text)

    # Match password in DSN format with single quotes
    dsn_single_quote = re.compile(r"(password\s*=\s*')([^']+)(')", re.IGNORECASE)
    text = re.sub(dsn_single_quote, r"\1****\3", text)

    # Match password in DSN format with double quotes
    dsn_double_quote = re.compile(r'(password\s*=\s*")([^"]+)(")', re.IGNORECASE)
    text = re.sub(dsn_double_quote, r"\1****\3", text)

    return text


def _rejected(error: psycopg.Error) -> QueryError:
    """PostgreSQL's refusal of a statement, as the error pg_debug raises.

    The psycopg exception stays attached as ``__cause__``, with its SQLSTATE
    and diagnostics, for a caller that needs more than the message.
    """
    return QueryError(str(error))


def build_session_setup(
    search_path: str | None = None,
    statement_timeout: int | None = None,
) -> list[Composed] | None:
    """Build the SET statements to run on every pooled connection.

    Args:
        search_path: Comma-separated schema names, e.g. ``"reporting, public"``.
        statement_timeout: Statement timeout in seconds. 0 disables it.

    Returns:
        The statements to run, or None when there is nothing to configure.
    """
    statements: list[Composed] = []

    if search_path:
        schemas = [s.strip() for s in search_path.split(",") if s.strip()]
        if schemas:
            # Identifier quotes each schema, which is also correct for the
            # special "$user" entry.
            statements.append(SQL(QUERY_MARKER + " SET search_path TO {}").format(SQL(", ").join(Identifier(s) for s in schemas)))

    if statement_timeout is not None:
        statements.append(SQL(QUERY_MARKER + " SET statement_timeout = {}").format(Literal(f"{int(statement_timeout)}s")))

    return statements or None


class DbConnPool:
    """Database connection manager using psycopg's connection pool."""

    def __init__(
        self,
        connection_url: str | None = None,
        search_path: str | None = None,
        statement_timeout: int | None = None,
    ):
        """
        Args:
            connection_url: PostgreSQL connection URL.
            search_path: Schema search path to set on every connection.
            statement_timeout: Statement timeout in seconds to set on every
                connection. None leaves the server default in place.
        """
        self.connection_url = connection_url
        self.search_path = search_path
        self.statement_timeout = statement_timeout
        self.pool: AsyncConnectionPool | None = None
        self._is_valid = False
        self._last_error = None

    async def _configure_connection(self, conn: Any) -> None:
        """Apply the session settings to a connection as the pool hands it out."""
        statements = build_session_setup(self.search_path, self.statement_timeout)
        if not statements:
            return
        for statement in statements:
            await conn.execute(statement)
        await conn.commit()

    async def pool_connect(self, connection_url: str | None = None) -> AsyncConnectionPool:
        """Initialize connection pool with retry logic."""
        # If we already have a valid pool, return it
        if self.pool and self._is_valid:
            return self.pool

        url = connection_url or self.connection_url
        self.connection_url = url
        if not url:
            self._is_valid = False
            self._last_error = "Database connection URL not provided"
            raise InvalidArgumentError(self._last_error)

        # Close any existing pool before creating a new one
        await self.close()

        try:
            # Configure connection pool with appropriate settings
            self.pool = AsyncConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=5,
                open=False,  # Don't connect immediately, let's do it explicitly
                configure=self._configure_connection,
            )

            # Open the pool explicitly
            await self.pool.open()

            # Test the connection pool by executing a simple query
            async with self.pool.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(f"{QUERY_MARKER} SELECT 1")

            self._is_valid = True
            self._last_error = None
            return self.pool
        except Exception as e:
            self._is_valid = False
            self._last_error = str(e)

            # Clean up failed pool
            await self.close()

            raise ConnectionFailedError(f"Could not connect to database: {obfuscate_password(str(e))}") from e

    async def close(self) -> None:
        """Close the connection pool."""
        if self.pool:
            try:
                # Close the pool
                await self.pool.close()
            except Exception as e:
                logger.warning("Error closing connection pool: %s", e)
            finally:
                self.pool = None
                self._is_valid = False

    @property
    def is_valid(self) -> bool:
        """Check if the connection pool is valid."""
        return self._is_valid

    @property
    def last_error(self) -> str | None:
        """Get the last error message."""
        return self._last_error


class SqlDriver:
    """Adapter class that wraps a PostgreSQL connection with the interface expected by DTA."""

    @dataclass
    class RowResult:
        """Simple class to match the Griptape RowResult interface."""

        cells: dict[str, Any]

    # Set by session(). Declared on the class so subclasses that do not call
    # this __init__ (SafeSqlDriver) still read a sane default.
    _pinned_connection: Any = None

    def __init__(
        self,
        conn: Any = None,
        engine_url: str | None = None,
    ):
        """
        Initialize with a PostgreSQL connection or pool.

        Args:
            conn: PostgreSQL connection object or pool
            engine_url: Connection URL string as an alternative to providing a connection
        """
        if conn:
            self.conn = conn
            # Check if this is a connection pool
            self.is_pool = isinstance(conn, DbConnPool)
        elif engine_url:
            # Don't connect here since we need async connection
            self.engine_url = engine_url
            self.conn = None
            self.is_pool = False
        else:
            raise InvalidArgumentError("Either conn or engine_url must be provided")

    def connect(self):
        if self.conn is not None:
            return self.conn
        if self.engine_url:
            self.conn = DbConnPool(self.engine_url)
            self.is_pool = True
            return self.conn
        else:
            raise NotConnectedError("Connection not established. Either conn or engine_url must be provided")

    @asynccontextmanager
    async def session(self) -> AsyncIterator["SqlDriver"]:
        """Pin one pooled connection for the duration of the block.

        Session state — hypothetical indexes, ``SET``, temp tables, advisory
        locks — lives in the connection that created it. Without a pin, the
        pool is free to hand the next statement a different connection and the
        state silently disappears.

        The pinned driver is the same object, so anything already holding a
        reference to it sees the pin too. That also means a pinned driver must
        not be shared across concurrent tasks: one connection cannot run two
        statements at once.
        """
        if self._pinned_connection is not None:
            # Nested session: reuse the outer pin rather than borrowing twice.
            yield self
            return

        if self.conn is None:
            self.connect()
            if self.conn is None:
                raise NotConnectedError("Connection not established")

        if not self.is_pool:
            # A direct connection is already a single session.
            yield self
            return

        pool = await self.conn.pool_connect()
        try:
            async with pool.connection() as connection:
                self._pinned_connection = connection
                try:
                    yield self
                finally:
                    self._pinned_connection = None
        except psycopg.Error as e:
            raise _rejected(e) from e

    async def execute_query(
        self,
        query: LiteralString,
        params: list[Any] | None = None,
        force_readonly: bool = False,
    ) -> list[RowResult] | None:
        """
        Execute a query and return results.

        Args:
            query: SQL query to execute
            params: Query parameters
            force_readonly: Whether to enforce read-only mode

        Returns:
            The rows of the last statement, or None when it returns none
            (DDL, for example).

        Raises:
            QueryError: If PostgreSQL rejected the statement or it failed to
                run. The psycopg exception is its ``__cause__``.
        """
        try:
            if self.conn is None:
                self.connect()
                if self.conn is None:
                    raise NotConnectedError("Connection not established")

            query = self.tagged(query)

            # Inside session(), every statement goes to the pinned connection.
            if self._pinned_connection is not None:
                try:
                    return await self._execute_with_connection(self._pinned_connection, query, params, force_readonly=force_readonly)
                except Exception:
                    # A failed statement leaves the connection's transaction
                    # aborted, and outside a session that connection would go
                    # back to the pool to be reset. Pinned, it stays — so every
                    # later statement in the session fails with "current
                    # transaction is aborted" and one recoverable error takes
                    # the whole session with it. Statements here commit
                    # individually, so clearing it loses nothing committed, and
                    # hypothetical indexes live in backend memory rather than in
                    # the transaction.
                    await self._reset_pinned_transaction()
                    raise

            # Handle connection pool vs direct connection
            if self.is_pool:
                # For pools, get a connection from the pool
                pool = await self.conn.pool_connect()
                async with pool.connection() as connection:
                    return await self._execute_with_connection(connection, query, params, force_readonly=force_readonly)
            else:
                # Direct connection approach
                return await self._execute_with_connection(self.conn, query, params, force_readonly=force_readonly)
        except Exception as e:
            # Mark pool as invalid if there was a connection issue
            if self.conn and self.is_pool:
                self.conn._is_valid = False  # type: ignore
                self.conn._last_error = str(e)  # type: ignore
            elif self.conn and not self.is_pool:
                self.conn = None

            if isinstance(e, psycopg.Error):
                raise _rejected(e) from e
            raise

    @staticmethod
    def tagged(query: LiteralString) -> LiteralString:
        """Mark a statement as this library's own.

        Applied to everything the driver issues, in both access modes: a
        statement pg_debug ran to answer a question about the workload is not
        part of that workload, and without the marker there is no way to tell
        the two apart afterwards.
        """
        if QUERY_MARKER in query:
            return query
        return f"{QUERY_MARKER} {query}"

    async def _reset_pinned_transaction(self) -> None:
        """Clear an aborted transaction so the pinned session stays usable."""
        if self._pinned_connection is None:
            return
        try:
            await self._pinned_connection.rollback()
        except Exception as rollback_error:
            # Never let this replace the error that caused it.
            logger.error("Error clearing the session's aborted transaction: %s", rollback_error)

    async def _execute_with_connection(self, connection, query, params, force_readonly) -> list[RowResult] | None:
        """Execute query with the given connection."""
        transaction_started = False
        try:
            async with connection.cursor(row_factory=dict_row) as cursor:
                # Start read-only transaction
                if force_readonly:
                    await cursor.execute("BEGIN TRANSACTION READ ONLY")
                    transaction_started = True

                if params:
                    await cursor.execute(query, params)
                else:
                    await cursor.execute(query)

                # For multiple statements, move to the last statement's results
                while cursor.nextset():
                    pass

                if cursor.description is None:  # No results (like DDL statements)
                    if not force_readonly:
                        await cursor.execute("COMMIT")
                    elif transaction_started:
                        await cursor.execute("ROLLBACK")
                        transaction_started = False
                    return None

                # Get results from the last statement only
                rows = await cursor.fetchall()

                # End the transaction appropriately
                if not force_readonly:
                    await cursor.execute("COMMIT")
                elif transaction_started:
                    await cursor.execute("ROLLBACK")
                    transaction_started = False

                return [SqlDriver.RowResult(cells=dict(row)) for row in rows]

        except Exception as e:
            # Try to roll back the transaction if it's still active
            if transaction_started:
                try:
                    await connection.rollback()
                except Exception as rollback_error:
                    logger.error("Error rolling back transaction: %s", rollback_error)

            logger.error("Error executing query (%s): %s", query, e)
            raise e

    async def execute_query_and_rollback(self, query: LiteralString, params: list[Any] | None = None) -> list[RowResult] | None:
        """Run a statement and undo it, keeping only what it returned.

        This exists for ``EXPLAIN ANALYZE``, which is the only way to learn what
        a plan really did and which gets there by running the query. For a
        SELECT that costs nothing; for an UPDATE or a DELETE it would not be
        acceptable, so the transaction is rolled back whether the statement
        succeeded or failed.

        What the rollback does *not* undo is anything that lives outside the
        transaction: a sequence consumed by ``nextval`` stays consumed, and a
        trigger that wrote to another system has already written. Callers that
        offer this to a user should say so.
        """
        if self.conn is None:
            self.connect()
            if self.conn is None:
                raise NotConnectedError("Connection not established")

        query = self.tagged(query)

        try:
            if self._pinned_connection is not None:
                return await self._execute_rolled_back(self._pinned_connection, query, params)
            if self.is_pool:
                pool = await self.conn.pool_connect()
                async with pool.connection() as connection:
                    return await self._execute_rolled_back(connection, query, params)
            return await self._execute_rolled_back(self.conn, query, params)
        except psycopg.Error as e:
            raise _rejected(e) from e

    async def _execute_rolled_back(self, connection, query, params) -> list[RowResult] | None:
        """Execute on a connection inside a transaction that is always undone."""
        try:
            async with connection.cursor(row_factory=dict_row) as cursor:
                # Without autocommit psycopg has already opened a transaction
                # for us; with it, nothing is open and there would be nothing
                # to roll back.
                if getattr(connection, "autocommit", False):
                    await cursor.execute("BEGIN")

                if params:
                    await cursor.execute(query, params)
                else:
                    await cursor.execute(query)

                while cursor.nextset():
                    pass

                if cursor.description is None:
                    return None
                rows = await cursor.fetchall()
                return [SqlDriver.RowResult(cells=dict(row)) for row in rows]
        finally:
            try:
                await connection.rollback()
            except Exception as rollback_error:
                # Never let this replace whatever the statement itself raised.
                logger.error("Error rolling back the diagnosed statement: %s", rollback_error)

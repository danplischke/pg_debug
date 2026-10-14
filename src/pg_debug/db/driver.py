"""SQL driver adapter for PostgreSQL connections."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import LiteralString
from urllib.parse import urlparse
from urllib.parse import urlunparse

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL
from psycopg.sql import Composed
from psycopg.sql import Literal
from psycopg_pool import AsyncConnectionPool

from ..errors import ConnectionFailedError
from ..errors import InvalidArgumentError
from ..errors import NotConnectedError
from ..errors import QueryError

if TYPE_CHECKING:
    from ..context import QueryContext

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


#: The savepoint a statement with a cleanup runs after, so that the cleanup can
#: run in the same transaction when the statement fails.
CLEANUP_SAVEPOINT = "pg_debug_cleanup"

#: The longest statement timeout PostgreSQL takes, in whole seconds: it counts
#: in milliseconds, up to the largest 32-bit integer.
MAX_STATEMENT_TIMEOUT = 2_147_483


def check_statement_timeout(seconds: int | None) -> None:
    """Refuse a timeout PostgreSQL would refuse, before any statement depends on it.

    Left to the server, it would fail in the setup of every transaction, and be
    reported as the failure of whatever that transaction was for: the query
    context, when there is one.

    Raises:
        InvalidArgumentError: If it is negative or more than PostgreSQL allows.
    """
    if seconds is not None and not 0 <= seconds <= MAX_STATEMENT_TIMEOUT:
        raise InvalidArgumentError(f"The statement timeout must be between 0 and {MAX_STATEMENT_TIMEOUT} seconds, PostgreSQL's limit, not {seconds}.")


def _quoted_identifier(name: str) -> str:
    """A name as PostgreSQL writes it double-quoted, which is how a search path holds it."""
    return '"' + name.replace('"', '""') + '"'


def build_transaction_setup(
    statement_timeout: int | None = None,
    search_path: str | None = None,
    context: QueryContext | None = None,
) -> Composed | None:
    """The statement that applies pg-debug's settings to the transaction it runs in.

    Everything goes through ``set_config(name, value, true)``, and the role
    through ``set_config('role', ...)``, which is ``SET LOCAL ROLE``, so all of
    it ends with the transaction. A session-level ``SET`` would not: behind a
    transaction pooler the session belongs to whichever client the pooler
    gives the backend next, and the setting would reach only the backend that
    ran it, and stay there.

    The role comes last: PostgreSQL evaluates the select list in order, so a
    setting only the connecting user may change is set before the switch to
    a role that may not. Names and values go in as literals psycopg quotes,
    never as text pasted into the statement.

    Args:
        statement_timeout: In seconds. 0 disables it.
        search_path: Comma-separated schema names, e.g. ``"reporting, public"``.
        context: The query context, for a statement that runs or plans the
            user's query.

    Returns:
        The statement, or None when there is nothing to apply.
    """
    settings: list[tuple[str, str]] = []
    if statement_timeout is not None:
        settings.append(("statement_timeout", f"{int(statement_timeout)}s"))
    schemas = [schema.strip() for schema in (search_path or "").split(",") if schema.strip()]
    if schemas:
        # Each schema quoted, which is also correct for the special "$user".
        settings.append(("search_path", ", ".join(_quoted_identifier(schema) for schema in schemas)))
    if context:
        settings.extend(context.settings.items())

    calls = [SQL("set_config({}, {}, true)").format(Literal(name), Literal(value)) for name, value in settings]
    if context and context.role is not None:
        calls.append(SQL("set_config('role', {}, true)").format(Literal(context.role)))
    if not calls:
        return None
    return SQL(QUERY_MARKER + " SELECT ") + SQL(", ").join(calls)


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
            search_path: Schema search path for every statement a
                :class:`SqlDriver` runs on this pool.
            statement_timeout: Statement timeout in seconds for every
                statement a :class:`SqlDriver` runs on this pool. None leaves
                the server default in place.

        The driver applies both inside each transaction, never to the
        connection: behind a transaction pooler a connection is not one
        session, and a setting made for the session would reach one backend
        and stay there for the pooler's next client.
        """
        check_statement_timeout(statement_timeout)
        self.connection_url = connection_url
        self.search_path = search_path
        self.statement_timeout = statement_timeout
        self.pool: AsyncConnectionPool | None = None
        self._is_valid = False
        self._last_error = None

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
                # psycopg prepares a statement on the server once it has run
                # five times on a connection. Behind a transaction pooler the
                # connection is not one session: the next execution reaches a
                # backend without the statement, or another client's
                # preparation one that already has it, and Supavisor and the
                # poolers Cloud SQL, AlloyDB and Azure offer do not support
                # prepared statements at all. pg-debug runs few statements
                # often enough for preparing them to save anything.
                kwargs={"prepare_threshold": None},
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
        context: QueryContext | None = None,
    ):
        """
        Initialize with a PostgreSQL connection or pool.

        Args:
            conn: PostgreSQL connection object or pool
            engine_url: Connection URL string as an alternative to providing a connection
            context: The role and settings for the statements that run or
                plan the user's query, the ones executed with
                ``in_context=True``. Every other statement runs as the
                connecting user, with its settings.
        """
        self.context = context
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

    def with_context(self, context: QueryContext | None) -> SqlDriver:
        """The same driver, on the same connection or pool, with another query context.

        For a statement that needs a setting of its own: the setting goes in
        the context, which applies it inside the transaction of each statement
        run with ``in_context=True``, rather than in a ``SET`` that a
        transaction pooler would leave on whichever backend ran it.

        The new driver does not share a pin made by :meth:`session`: it
        borrows its own connection, so it stays usable after the session ends.
        """
        if self.conn is None:
            # Connect first, so the two share one pool rather than each opening its own.
            self.connect()
        driver = copy.copy(self)
        driver.context = context
        driver._pinned_connection = None
        return driver

    @asynccontextmanager
    async def session(self) -> AsyncIterator[SqlDriver]:
        """Pin one pooled connection for the duration of the block.

        The statements in the block share a connection to the server, which
        saves borrowing one from the pool for each. They do not necessarily
        share a session: behind a transaction pooler (PgBouncer in transaction
        mode, Supavisor, the pooled endpoints of Neon and Supabase) each
        transaction can reach a different backend, so session state — a
        ``SET``, a hypothetical index, a temp table, an advisory lock — made in
        one statement may be missing from the next, and left behind for
        another client. Anything that has to live in the backend goes in the
        same transaction as what reads it: settings through the query context
        (:meth:`with_context`), hypothetical indexes in one batch with the
        ``EXPLAIN`` and a ``cleanup`` that resets them.

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
        *,
        in_context: bool = False,
        cleanup: LiteralString | None = None,
    ) -> list[RowResult] | None:
        """
        Execute a query and return results.

        Args:
            query: SQL query to execute
            params: Query parameters
            force_readonly: Whether to enforce read-only mode
            in_context: Run it with the driver's query context. For the
                statements that run or plan the user's own SQL, and the reads
                of settings that shaped them; never for pg-debug's reads of
                the catalog, which the context's role may be barred from.
            cleanup: A statement to run after the query, in the same
                transaction, whether the query succeeded, failed or was cancelled: for
                state that lives in the backend rather than the transaction,
                such as hypothetical indexes, which a rollback does not undo
                and a transaction pooler would leave for its next client. The
                query's rows are returned, not the cleanup's.

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
                    return await self._execute_with_connection(
                        self._pinned_connection, query, params, force_readonly=force_readonly, in_context=in_context, cleanup=cleanup
                    )
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
                    return await self._execute_with_connection(
                        connection, query, params, force_readonly=force_readonly, in_context=in_context, cleanup=cleanup
                    )
            else:
                # Direct connection approach
                return await self._execute_with_connection(
                    self.conn, query, params, force_readonly=force_readonly, in_context=in_context, cleanup=cleanup
                )
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

    async def check_context(self) -> None:
        """Apply the query context once, in a read-only transaction that is rolled back.

        A role the connecting user cannot switch to, or a setting the server
        does not know or will not take, fails here, once, with the reason.
        Left to the first statement that needed it, the failure would be
        reported as that statement's: the index advisor, for one, would count
        every query it planned as a query it could not plan.

        Raises:
            InvalidArgumentError: If PostgreSQL refused the context.
        """
        if self.context:
            await self.execute_query("SELECT 1", force_readonly=True, in_context=True)

    def _transaction_setup(self, in_context: bool) -> Composed | None:
        """The statement that sets up a transaction: the pool's timeout and search path, and the context if asked for."""
        pool = self.conn if self.is_pool else None
        return build_transaction_setup(
            statement_timeout=getattr(pool, "statement_timeout", None),
            search_path=getattr(pool, "search_path", None),
            context=self.context if in_context else None,
        )

    async def _apply_setup(self, cursor: Any, setup: Composed, in_context: bool) -> None:
        """Apply the setup to the transaction the cursor is in."""
        try:
            await cursor.execute(setup)
        except psycopg.Error as e:
            if in_context and self.context:
                raise InvalidArgumentError(f"Could not apply the query context ({self.context.describe()}): {e}") from e
            raise

    async def _clean_up_after_failure(self, cursor: Any, cleanup: LiteralString) -> None:
        """Run the cleanup in the transaction the failed statement aborted, from the savepoint before it.

        Also after a cancellation, such as restricted mode's own timeout or
        Ctrl-C: psycopg has cancelled the statement on the server by then, and
        the connection is waiting in the aborted transaction.
        """
        try:
            await cursor.execute(f"{QUERY_MARKER} ROLLBACK TO SAVEPOINT {CLEANUP_SAVEPOINT}")
            await cursor.execute(cleanup)
        except Exception as cleanup_error:
            # Never let this replace the error that caused it.
            logger.error("Error cleaning up after the failed statement: %s", cleanup_error)

    async def _reset_pinned_transaction(self) -> None:
        """Clear an aborted transaction so the pinned session stays usable."""
        if self._pinned_connection is None:
            return
        try:
            await self._pinned_connection.rollback()
        except Exception as rollback_error:
            # Never let this replace the error that caused it.
            logger.error("Error clearing the session's aborted transaction: %s", rollback_error)

    async def _execute_with_connection(
        self,
        connection,
        query,
        params,
        force_readonly,
        in_context=False,
        cleanup: LiteralString | None = None,
    ) -> list[RowResult] | None:
        """Execute query with the given connection."""
        transaction_started = False
        setup = self._transaction_setup(in_context)
        if cleanup is not None:
            cleanup = self.tagged(cleanup)
        try:
            async with connection.cursor(row_factory=dict_row) as cursor:
                # Start read-only transaction
                if force_readonly:
                    await cursor.execute("BEGIN TRANSACTION READ ONLY")
                    transaction_started = True

                if (setup is not None or cleanup is not None) and not transaction_started and getattr(connection, "autocommit", False):
                    # Outside a transaction block, a set_config(..., true)
                    # lasts only as long as the statement that made it, and
                    # there is nothing to take a savepoint in.
                    await cursor.execute("BEGIN")
                    transaction_started = True

                if setup is not None:
                    await self._apply_setup(cursor, setup, in_context)

                if cleanup is not None:
                    await cursor.execute(f"{QUERY_MARKER} SAVEPOINT {CLEANUP_SAVEPOINT}")

                try:
                    if params:
                        await cursor.execute(query, params)
                    else:
                        await cursor.execute(query)

                    # For multiple statements, move to the last statement's results
                    while cursor.nextset():
                        pass

                    # Results from the last statement only; none for DDL
                    rows = await cursor.fetchall() if cursor.description is not None else None

                    if cleanup is not None:
                        await cursor.execute(cleanup)
                except (psycopg.Error, asyncio.CancelledError):
                    # A cancellation is not a psycopg.Error, and is how a
                    # timeout on this side arrives.
                    if cleanup is not None:
                        await self._clean_up_after_failure(cursor, cleanup)
                    raise

                # End the transaction appropriately
                if not force_readonly:
                    await cursor.execute("COMMIT")
                elif transaction_started:
                    await cursor.execute("ROLLBACK")
                    transaction_started = False

                if rows is None:
                    return None
                return [SqlDriver.RowResult(cells=dict(row)) for row in rows]

        except (Exception, asyncio.CancelledError) as e:
            # Roll back the transaction this call began, or psycopg began for
            # it: left open, the context's role and settings would carry over
            # to the connection's next statement, which on a connection the
            # caller owns is the caller's.
            if transaction_started or not getattr(connection, "autocommit", False):
                try:
                    await connection.rollback()
                except Exception as rollback_error:
                    logger.error("Error rolling back transaction: %s", rollback_error)

            if not isinstance(e, asyncio.CancelledError):
                # The statement only at debug: pg-debug's own statements run
                # to dozens of lines, and a health run that carries on past a
                # failed check would otherwise print each one in full.
                logger.error("Error executing query: %s", e)
                logger.debug("The statement that failed: %s", query)
            raise

    async def execute_query_and_rollback(
        self,
        query: LiteralString,
        params: list[Any] | None = None,
        *,
        in_context: bool = False,
    ) -> list[RowResult] | None:
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

        ``in_context`` runs it with the query context, as for
        :meth:`execute_query`.
        """
        if self.conn is None:
            self.connect()
            if self.conn is None:
                raise NotConnectedError("Connection not established")

        query = self.tagged(query)

        try:
            if self._pinned_connection is not None:
                return await self._execute_rolled_back(self._pinned_connection, query, params, in_context=in_context)
            if self.is_pool:
                pool = await self.conn.pool_connect()
                async with pool.connection() as connection:
                    return await self._execute_rolled_back(connection, query, params, in_context=in_context)
            return await self._execute_rolled_back(self.conn, query, params, in_context=in_context)
        except psycopg.Error as e:
            raise _rejected(e) from e

    async def _execute_rolled_back(self, connection, query, params, in_context=False) -> list[RowResult] | None:
        """Execute on a connection inside a transaction that is always undone."""
        try:
            async with connection.cursor(row_factory=dict_row) as cursor:
                # Without autocommit psycopg has already opened a transaction
                # for us; with it, nothing is open and there would be nothing
                # to roll back.
                if getattr(connection, "autocommit", False):
                    await cursor.execute("BEGIN")

                setup = self._transaction_setup(in_context)
                if setup is not None:
                    await self._apply_setup(cursor, setup, in_context)

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

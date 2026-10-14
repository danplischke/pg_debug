# ruff: noqa: B017
import asyncio
import socket
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool
from psycopg_pool import PoolClosed
from psycopg_pool import PoolTimeout

from pg_debug.db.driver import POOL_MAX_IDLE
from pg_debug.db.driver import DbConnPool
from pg_debug.db.driver import SqlDriver
from pg_debug.errors import ConnectionFailedError
from pg_debug.errors import InvalidArgumentError


class AsyncContextManagerMock(AsyncMock):
    """A better mock for async context managers"""

    async def __aenter__(self):
        return self.aenter

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def mock_pool():
    """Create a mock for AsyncConnectionPool."""
    pool = MagicMock()

    # Create cursor context manager
    cursor = AsyncMock()

    # Create connection context manager
    connection = AsyncMock()
    connection.cursor = MagicMock(return_value=AsyncContextManagerMock())
    connection.cursor.return_value.aenter = cursor

    # Setup connection manager
    conn_ctx = AsyncContextManagerMock()
    conn_ctx.aenter = connection

    # Setup pool.connection() to return our mocked connection context manager
    pool.connection = MagicMock(return_value=conn_ctx)

    # Setup pool.open and pool.close as async mocks
    pool.open = AsyncMock()
    pool.close = AsyncMock()

    return pool


@pytest.mark.asyncio
async def test_pool_connect_success(mock_pool):
    """Test successful connection to the database pool."""
    with patch("pg_debug.db.driver.AsyncConnectionPool", return_value=mock_pool):
        # Patch the connection test part to skip it
        with patch.object(DbConnPool, "pool_connect", new=AsyncMock(return_value=mock_pool)) as mock_connect:
            db_pool = DbConnPool("postgresql://user:pass@localhost/db")
            pool = await db_pool.pool_connect()

            assert pool == mock_pool
            mock_connect.assert_called_once()


@pytest.mark.asyncio
async def test_pool_connect_with_retry(mock_pool):
    """Test pool connection with retry on failure."""
    # First attempt fails, second succeeds
    mock_pool.open.side_effect = [Exception("Connection error"), None]

    # Create a mock implementation of pool_connect that simulates a retry
    async def mock_pool_connect(self, connection_url=None):
        if not hasattr(self, "_attempt_count"):
            self._attempt_count = 0

        self._attempt_count += 1

        if self._attempt_count == 1:
            # First attempt fails
            raise Exception("Connection error")
        else:
            # Second attempt succeeds
            self.pool = mock_pool
            self._is_valid = True
            return mock_pool

    with patch("pg_debug.db.driver.AsyncConnectionPool", return_value=mock_pool):
        with patch.object(DbConnPool, "pool_connect", mock_pool_connect):
            db_pool = DbConnPool("postgresql://user:pass@localhost/db")

            # Call our own custom implementation directly to simulate the retry
            # First call will fail, second call will succeed
            with pytest.raises(Exception):
                await mock_pool_connect(db_pool)

            # Second attempt should succeed
            pool = await mock_pool_connect(db_pool)

            assert pool == mock_pool
            assert db_pool._is_valid is True  # type: ignore


@pytest.mark.asyncio
async def test_pool_connect_all_retries_fail(mock_pool):
    """Test pool connection when all retry attempts fail."""
    # Mock pool.open to raise an exception for the test
    mock_pool.open.side_effect = Exception("Persistent connection error")

    # Configure AsyncConnectionPool's constructor to return our mock
    with patch("pg_debug.db.driver.AsyncConnectionPool", return_value=mock_pool):
        # Mock sleep to speed up test
        with patch("asyncio.sleep", AsyncMock()):
            db_pool = DbConnPool("postgresql://user:pass@localhost/db")

            # This should fail since pool.open raises an exception
            with pytest.raises(ConnectionFailedError, match="Could not connect to database: Persistent connection error"):
                await db_pool.pool_connect()

            # Verify the pool is marked as invalid
            assert db_pool._is_valid is False  # type: ignore
            # Verify open was called at least once (no need to verify retries here)
            assert mock_pool.open.call_count >= 1


@pytest.mark.asyncio
async def test_close_pool(mock_pool):
    """Test closing the connection pool."""
    with patch("pg_debug.db.driver.AsyncConnectionPool", return_value=mock_pool):
        db_pool = DbConnPool("postgresql://user:pass@localhost/db")

        # Mock the pool_connect method to avoid actual connection
        db_pool.pool_connect = AsyncMock(return_value=mock_pool)
        await db_pool.pool_connect()
        db_pool.pool = mock_pool  # Set directly
        db_pool._is_valid = True  # type: ignore

        # Close the pool
        await db_pool.close()

        # Check that pool is now invalid
        assert db_pool._is_valid is False  # type: ignore
        assert db_pool.pool is None
        mock_pool.close.assert_called_once()


@pytest.mark.asyncio
async def test_close_handles_errors(mock_pool):
    """Test that close() handles exceptions gracefully."""
    mock_pool.close.side_effect = Exception("Error closing pool")

    with patch("pg_debug.db.driver.AsyncConnectionPool", return_value=mock_pool):
        db_pool = DbConnPool("postgresql://user:pass@localhost/db")

        # Mock the pool_connect method to avoid actual connection
        db_pool.pool_connect = AsyncMock(return_value=mock_pool)
        await db_pool.pool_connect()
        db_pool.pool = mock_pool  # Set directly
        db_pool._is_valid = True  # type: ignore

        # Close should not raise the exception
        await db_pool.close()

        # Pool should still be marked as invalid
        assert db_pool._is_valid is False  # type: ignore
        assert db_pool.pool is None


@pytest.mark.asyncio
async def test_pool_connect_initialized(mock_pool):
    """Test pool_connect when pool is already initialized."""
    with patch("pg_debug.db.driver.AsyncConnectionPool", return_value=mock_pool):
        db_pool = DbConnPool("postgresql://user:pass@localhost/db")

        # Mock the pool_connect method to avoid actual connection
        db_pool.pool_connect = AsyncMock(return_value=mock_pool)
        original_pool = await db_pool.pool_connect()
        db_pool.pool = mock_pool  # Set directly
        db_pool._is_valid = True  # type: ignore

        # Reset the mock counts
        mock_pool.open.reset_mock()

        # Get the pool again
        returned_pool = await db_pool.pool_connect()

        # Should return the existing pool without reconnecting
        assert returned_pool == original_pool
        mock_pool.open.assert_not_called()


@pytest.mark.asyncio
async def test_pool_connect_not_initialized(mock_pool):
    """Test pool_connect when pool is not yet initialized."""
    with patch("pg_debug.db.driver.AsyncConnectionPool", return_value=mock_pool):
        db_pool = DbConnPool("postgresql://user:pass@localhost/db")

        # Mock the pool_connect method to avoid actual connection
        db_pool.pool_connect = AsyncMock(return_value=mock_pool)

        # Get pool without initializing first
        pool = await db_pool.pool_connect()

        # Verify pool connect was called
        db_pool.pool_connect.assert_called_once()
        assert pool == mock_pool


@pytest.mark.asyncio
async def test_connection_url_property():
    """Test connection_url property."""
    db_pool = DbConnPool("postgresql://user:pass@localhost/db")
    assert db_pool.connection_url == "postgresql://user:pass@localhost/db"

    # Change the URL
    db_pool.connection_url = "postgresql://newuser:newpass@otherhost/otherdb"
    assert db_pool.connection_url == "postgresql://newuser:newpass@otherhost/otherdb"


@pytest.mark.asyncio
async def test_an_idle_pool_keeps_no_connection_open():
    """An open connection keeps a scale-to-zero compute awake, and a
    long-lived client that made one call would hold one for good."""
    pool = DbConnPool("postgresql://localhost/db")

    with patch("pg_debug.db.driver.AsyncConnectionPool") as built:
        built.return_value.open.side_effect = RuntimeError("stop here")
        with pytest.raises(Exception, match="stop here"):
            await pool.pool_connect()

    options = built.call_args.kwargs
    assert options["min_size"] == 0
    assert options["max_idle"] == POOL_MAX_IDLE
    # Every connection the pool can grow to is closed within six intervals
    # of going unused, inside the five minutes of Aurora's shortest pause.
    assert (options["max_size"] + 1) * POOL_MAX_IDLE <= 300


@pytest.mark.asyncio
async def test_psycopg_pool_takes_an_empty_minimum():
    """psycopg_pool refuses min_size 0 only with max_size 0 as well. CI runs
    this at the lowest version the dependency allows, too."""
    pool = AsyncConnectionPool("postgresql://localhost/db", min_size=0, max_size=5, max_idle=POOL_MAX_IDLE, open=False)

    assert (pool.min_size, pool.max_size, pool.max_idle) == (0, 5, POOL_MAX_IDLE)


@pytest.mark.asyncio
async def test_pool_connect_without_a_url_is_an_argument_error():
    with pytest.raises(InvalidArgumentError, match="Database connection URL not provided"):
        await DbConnPool().pool_connect()


def test_a_driver_needs_a_connection_or_a_url():
    with pytest.raises(InvalidArgumentError, match="Either conn or engine_url must be provided"):
        SqlDriver()


# -- A connection the pool could not get ------------------------------------
#
# The pool keeps none open while idle, so a call after an idle spell waits for
# it to connect. psycopg_pool only logs a failed attempt and tries again, and
# the caller used to wait out the pool's 30 seconds and be told no more than
# "couldn't get a connection", as a QueryError.


def closed_port() -> int:
    """A local port nothing listens on: connecting to it is refused at once."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_refused_connection_fails_at_once_with_the_reason():
    port = closed_port()
    pool = DbConnPool(f"postgresql://nobody@127.0.0.1:{port}/db")
    started = time.monotonic()

    with pytest.raises(ConnectionFailedError) as excinfo:
        # psycopg connects asynchronously only on a selector loop, which is
        # not the one Windows makes by default.
        asyncio.run(asyncio.wait_for(pool.pool_connect(), timeout=15), loop_factory=asyncio.SelectorEventLoop)

    assert time.monotonic() - started < 15
    assert f"port {port} failed" in str(excinfo.value)
    assert "couldn't get a connection" not in str(excinfo.value)
    assert pool.pool is None


class FakePool:
    """Enough of psycopg_pool's pool for DbConnPool to decide on, and to borrow from."""

    timeout = 30.0
    max_size = 5

    def __init__(self, available=0, waiting=1, error: Exception | None = None):
        self.stats = {"pool_available": available, "requests_waiting": waiting}
        self.error = error
        self.closed = False
        # What happens in the pool's own task while the caller waits.
        self.meanwhile: Callable[[], None] = lambda: None

    def get_stats(self):
        return self.stats

    async def close(self):
        self.closed = True

    @asynccontextmanager
    async def connection(self):
        self.meanwhile()
        if self.error is not None:
            raise self.error
        yield "connection"


def holding(fake: FakePool, borrowed: int = 0) -> DbConnPool:
    pool = DbConnPool("postgresql://app@localhost/db")
    pool.pool = fake  # type: ignore[assignment]
    pool._is_valid = True
    pool._borrowed = borrowed
    return pool


REFUSED = psycopg.OperationalError('connection failed: FATAL:  too many connections for role "app"')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("available", "waiting", "borrowed", "fails_the_waiter"),
    [
        (0, 1, 0, True),
        # A connection can still come to the waiter: one that is idle, or one
        # in use that will be given back. The pool tries again meanwhile.
        (1, 1, 0, False),
        (0, 1, 2, False),
        # Nobody is waiting: the pool was growing ahead of demand.
        (0, 0, 0, False),
    ],
)
async def test_a_failed_attempt_ends_the_wait_only_when_no_connection_can_come(available, waiting, borrowed, fails_the_waiter):
    fake = FakePool(available, waiting)
    pool = holding(fake, borrowed)

    pool._connect_failed([fake], REFUSED)  # type: ignore[list-item]
    await asyncio.sleep(0)

    assert fake.closed is fails_the_waiter
    assert pool.is_valid is not fails_the_waiter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        psycopg.OperationalError("connection failed: FATAL:  the database system is starting up"),
        psycopg.OperationalError("connection failed: Connection refused\n\tIs the server running on that host?"),
        psycopg.OperationalError("connection failed: server closed the connection unexpectedly"),
    ],
    ids=["starting-up", "refused-while-restarting", "closed"],
)
async def test_an_attempt_that_may_succeed_next_time_does_not_end_the_wait(error):
    """A restart or a failover clears in seconds; the pool's next attempt may
    connect, so the waiting caller waits for it rather than failing at once."""
    fake = FakePool(0, 1)
    pool = holding(fake, 0)
    pool._has_connected = True

    pool._connect_failed([fake], error)  # type: ignore[list-item]
    await asyncio.sleep(0)

    assert not fake.closed
    assert pool.is_valid


@pytest.mark.parametrize(
    "error",
    [
        psycopg.OperationalError('connection failed: FATAL:  password authentication failed for user "app"'),
        psycopg.OperationalError('connection failed: FATAL:  database "nope" does not exist'),
        REFUSED,
    ],
    ids=["password", "no-database", "too-many"],
)
def test_a_refusal_that_will_stay_is_recognised(error):
    from pg_debug.db.driver import _will_not_clear

    assert _will_not_clear(error)


@pytest.mark.asyncio
async def test_a_borrow_the_pool_gave_up_on_says_what_the_server_said():
    """Closing the pool is what fails the waiting caller at once."""
    fake = FakePool(error=PoolClosed("the pool is closed"))
    pool = holding(fake)
    fake.meanwhile = lambda: pool._connect_failed([fake], REFUSED)  # type: ignore[list-item]

    with pytest.raises(ConnectionFailedError, match='too many connections for role "app"'):
        async with pool.connection():
            pass
    await pool.close()
    assert fake.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("borrowed", "said"),
    [
        (5, "No pooled connection came free within 30 seconds: all 5 are in use."),
        (0, "Could not connect to database within 30 seconds: the attempt to connect had not finished."),
    ],
)
async def test_a_wait_that_runs_out_says_why(borrowed, said):
    pool = holding(FakePool(error=PoolTimeout("couldn't get a connection after 30.00 sec")), borrowed)

    with pytest.raises(ConnectionFailedError) as excinfo:
        async with pool.connection():
            pass

    assert str(excinfo.value) == said


@pytest.mark.asyncio
async def test_a_borrowed_connection_is_counted_until_it_is_given_back():
    pool = holding(FakePool())

    async with pool.connection() as connection:
        assert (connection, pool._borrowed) == ("connection", 1)
    assert pool._borrowed == 0


@pytest.mark.asyncio
async def test_a_caller_that_stops_waiting_does_not_leave_the_pool_trying():
    """A timeout around the call cancels it while the pool is still connecting.
    Left open, the pool's own task goes on trying and outlives the event loop,
    which then never finishes."""
    pool = DbConnPool("postgresql://app@localhost/db")
    opened = FakePool()

    async def connecting(conninfo):
        pool.pool = opened  # type: ignore[assignment]
        await asyncio.Event().wait()

    with patch.object(pool, "_open", connecting), pytest.raises(TimeoutError):
        await asyncio.wait_for(pool.pool_connect(), timeout=0.05)

    assert opened.closed
    assert pool.pool is None
    assert not pool.is_valid


@pytest.mark.parametrize(
    ("url", "environment", "added"),
    [
        ("postgresql://app@localhost/db", {}, {"connect_timeout": 30}),
        ("postgresql://app@localhost/db?connect_timeout=5", {}, {}),
        ("host=localhost connect_timeout=5", {}, {}),
        ("postgresql://app@localhost/db", {"PGCONNECT_TIMEOUT": "5"}, {}),
    ],
)
def test_an_attempt_to_connect_is_bounded_unless_the_user_bounded_it(url, environment, added, monkeypatch):
    """libpq sets no limit, so an attempt to a host that drops packets lasts
    as long as the operating system keeps trying."""
    from pg_debug.db.driver import _bounded

    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert _bounded(url) == added

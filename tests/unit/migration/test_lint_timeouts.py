"""The lock_timeout a migration's statements would wait for their locks with, and the advice when there is none.

A statement waiting for a lock that conflicts with ordinary reads or writes
queues every later one that conflicts with it, because PostgreSQL grants locks
in the order they were asked for. Without a lock_timeout that queue lasts as
long as the transaction it waits for.
"""

from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.migration import MigrationLinter
from pg_debug.migration.lint import TIMEOUTS_SQL
from pg_debug.migration.lint import milliseconds


def server(
    *,
    tables: tuple[str, ...] = ("orders",),
    lock_timeout: str = "0",
    default: str = "0",
    statement_timeout: str = "0",
    role: str = "owner",
    login_role: str = "owner",
    role_settings: list[str] | None = None,
) -> MagicMock:
    """A driver that answers the size query for ``tables`` and the timeouts query as given."""
    driver = MagicMock(spec=SqlDriver)

    async def answer(query, *args, **kwargs):
        if query == TIMEOUTS_SQL:
            return [
                SqlDriver.RowResult(
                    cells={
                        "role": role,
                        "login_role": login_role,
                        "lock_timeout": lock_timeout,
                        "lock_timeout_default": default,
                        "statement_timeout_default": statement_timeout,
                        "role_settings": role_settings or [],
                    }
                )
            ]
        if "pg_total_relation_size" in query:
            return [
                SqlDriver.RowResult(cells={"name": name, "bytes": 8192, "partitioned": False, "partitions": None, "partitions_partitioned": None})
                for name in tables
            ]
        return []

    driver.execute_query = AsyncMock(side_effect=answer)
    return driver


def timeout_calls(driver: MagicMock) -> list[Any]:
    return [call for call in driver.execute_query.call_args_list if call.args[0] == TIMEOUTS_SQL]


@pytest.mark.asyncio
async def test_an_access_exclusive_statement_with_no_lock_timeout_is_advised_one():
    [review] = await MigrationLinter(server()).review("ALTER TABLE orders ADD COLUMN note text")

    assert review.timeouts["lock_timeout"]["milliseconds"] == 0
    assert review.lock_timeout_advice is not None
    assert "SET lock_timeout" in review.lock_timeout_advice
    assert "every read and write on orders" in review.lock_timeout_advice
    assert "in the order they were asked for" in review.lock_timeout_advice
    assert review.to_dict()["lock_timeout_advice"] == review.lock_timeout_advice


@pytest.mark.asyncio
async def test_the_timeouts_are_read_in_the_query_context():
    """The migration's role and settings decide them, not pg-debug's own connection."""
    driver = server()

    await MigrationLinter(driver).review("ALTER TABLE orders ADD COLUMN note text")

    [call] = timeout_calls(driver)
    assert call.kwargs.get("in_context") is True


@pytest.mark.asyncio
async def test_a_share_lock_queues_writes_only():
    [review] = await MigrationLinter(server()).review("CREATE INDEX orders_note ON orders (note)")

    assert review.lock_timeout_advice is not None
    assert "every write on orders" in review.lock_timeout_advice


@pytest.mark.asyncio
async def test_a_lock_that_blocks_no_application_statement_is_not_advised():
    [review] = await MigrationLinter(server()).review("CREATE INDEX CONCURRENTLY orders_note ON orders (note)")

    assert review.lock_timeout_advice is None


@pytest.mark.asyncio
async def test_a_table_that_does_not_exist_yet_is_not_advised():
    """Nobody else can be using a table the migration has not created yet."""
    [review] = await MigrationLinter(server(tables=())).review("ALTER TABLE fresh ADD COLUMN note text")

    assert review.lock_timeout_advice is None


@pytest.mark.asyncio
async def test_a_lock_timeout_the_migration_sets_is_read_from_it():
    driver = server()

    [review] = await MigrationLinter(driver).review("SET lock_timeout = '2s'; ALTER TABLE orders ADD COLUMN note text")

    assert review.timeouts["lock_timeout"] == {"value": "2s", "milliseconds": 2000, "source": "set by the migration before this statement"}
    assert review.lock_timeout_advice is None


@pytest.mark.asyncio
async def test_a_reset_or_a_committed_set_local_ends_what_the_migration_set():
    reviews = await MigrationLinter(server()).review(
        "BEGIN; SET LOCAL lock_timeout = 1000; ALTER TABLE orders ADD COLUMN a text; COMMIT; ALTER TABLE orders ADD COLUMN b text;"
        " SET lock_timeout = '3s'; RESET lock_timeout; ALTER TABLE orders ADD COLUMN c text"
    )
    by_column = {review.statement.split()[-2]: review for review in reviews}

    assert by_column["a"].timeouts["lock_timeout"]["milliseconds"] == 1000
    assert by_column["a"].lock_timeout_advice is None
    assert by_column["b"].lock_timeout_advice is not None
    assert by_column["c"].lock_timeout_advice is not None


@pytest.mark.asyncio
async def test_a_lock_timeout_the_query_context_sets_is_read():
    [review] = await MigrationLinter(server(lock_timeout="4000")).review("ALTER TABLE orders ADD COLUMN note text")

    assert review.timeouts["lock_timeout"] == {"value": "4s", "milliseconds": 4000, "source": "set by the query context"}
    assert review.lock_timeout_advice is None


@pytest.mark.asyncio
async def test_the_role_the_context_names_is_read_for_its_own_settings():
    """SET ROLE does not apply ALTER ROLE ... SET; logging in as the role, as the migration would, does."""
    driver = server(role="app", login_role="owner", role_settings=["lock_timeout=3s", "statement_timeout=1min"])

    [review] = await MigrationLinter(driver).review("ALTER TABLE orders ADD COLUMN note text")

    assert review.timeouts["lock_timeout"]["milliseconds"] == 3000
    assert "ALTER ROLE app SET lock_timeout" in review.timeouts["lock_timeout"]["source"]
    assert review.timeouts["statement_timeout"]["milliseconds"] == 60_000
    assert review.lock_timeout_advice is None


@pytest.mark.asyncio
async def test_a_role_without_settings_of_its_own_says_where_the_value_came_from():
    [review] = await MigrationLinter(server(role="app", login_role="owner")).review("ALTER TABLE orders ADD COLUMN note text")

    assert "app sets none of its own" in review.timeouts["lock_timeout"]["source"]
    assert review.lock_timeout_advice is not None


@pytest.mark.asyncio
async def test_offline_nothing_is_read_and_nothing_is_advised():
    [review] = await MigrationLinter().review("ALTER TABLE orders ADD COLUMN note text")

    assert review.timeouts == {}
    assert review.lock_timeout_advice is None


@pytest.mark.asyncio
async def test_a_server_that_will_not_say_leaves_the_review_standing():
    driver = server()
    sizes = driver.execute_query.side_effect

    async def refuse_timeouts(query, *args, **kwargs):
        if query == TIMEOUTS_SQL:
            raise RuntimeError("permission denied")
        return await sizes(query, *args, **kwargs)

    driver.execute_query = AsyncMock(side_effect=refuse_timeouts)

    [review] = await MigrationLinter(driver).review("ALTER TABLE orders ADD COLUMN note text")

    assert review.lock_timeout_advice is None
    assert review.timeouts == {}


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0", 0), ("5000", 5000), ("5s", 5000), ("'2min'", 120_000), ("250ms", 250), ("1h", 3_600_000), ("soon", None)],
)
def test_timeouts_are_read_in_their_units(text, expected):
    assert milliseconds(text) == expected


def test_the_timeouts_query_passes_restricted_mode():
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(TIMEOUTS_SQL)

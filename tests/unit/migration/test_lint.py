"""What each statement in a migration will do while it runs.

The lock levels asserted here were measured against PostgreSQL 16 — each
statement run in a transaction, pg_locks read for the modes it held, and
relfilenode compared before and after — not recalled. Several contradict the
usual advice, and those are the ones worth pinning down.
"""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SqlDriver
from pg_debug.errors import InvalidArgumentError
from pg_debug.migration import Duration
from pg_debug.migration import LockLevel
from pg_debug.migration import MigrationLinter
from pg_debug.severity import Severity


@pytest.fixture
def linter():
    """Offline: no sizes, so severities are not softened by a small table."""
    return MigrationLinter()


async def one(linter: MigrationLinter, sql: str):
    reviews = await linter.review(sql)
    assert len(reviews) == 1, f"expected one review, got {[r.kind for r in reviews]}"
    return reviews[0]


# -- Adding columns ---------------------------------------------------------


@pytest.mark.asyncio
async def test_adding_a_column_with_a_constant_default_does_not_rewrite(linter):
    """Since PostgreSQL 11, and still widely believed otherwise."""
    review = await one(linter, "ALTER TABLE orders ADD COLUMN n integer NOT NULL DEFAULT 0")

    assert review.kind == "add_column"
    assert review.rewrites_table is False
    assert review.duration is Duration.INSTANT
    assert review.severity is Severity.LOW


@pytest.mark.asyncio
async def test_adding_a_column_with_a_computed_default_is_flagged(linter):
    review = await one(linter, "ALTER TABLE orders ADD COLUMN u uuid DEFAULT gen_random_uuid()")

    assert review.kind == "add_column_with_computed_default"
    assert review.rewrites_table is True
    assert review.default_functions == ["gen_random_uuid"]
    # The suggested rewrite has to be SQL somebody can actually run.
    assert "ALTER TABLE orders ADD COLUMN u uuid;" in review.safer[0]
    assert "SET DEFAULT gen_random_uuid()" in review.safer[1]


@pytest.mark.asyncio
async def test_a_stored_generated_column_rewrites(linter):
    review = await one(linter, "ALTER TABLE orders ADD COLUMN d bigint GENERATED ALWAYS AS (id * 2) STORED")

    assert review.kind == "add_generated_column"
    assert review.rewrites_table is True


def volatility_driver(name: str, volatile: bool):
    """Answers the volatility lookup, and reports no table sizes."""
    driver = MagicMock(spec=SqlDriver)

    async def answer(query, *args, **kwargs):
        if "provolatile" in query:
            return [SqlDriver.RowResult(cells={"name": name, "volatile": volatile})]
        return []

    driver.execute_query = AsyncMock(side_effect=answer)
    return driver


@pytest.mark.asyncio
async def test_a_stable_default_is_resolved_as_not_rewriting():
    """now() is stable and costs nothing; clock_timestamp() is volatile and
    costs a rewrite. Only the catalog can tell them apart."""
    driver = volatility_driver("now", volatile=False)

    review = await one(MigrationLinter(driver), "ALTER TABLE orders ADD COLUMN seen timestamptz DEFAULT now()")

    assert review.kind == "add_column"
    assert review.rewrites_table is False
    assert review.duration is Duration.INSTANT
    assert "not volatile" in review.detail


@pytest.mark.asyncio
async def test_a_volatile_default_is_resolved_as_rewriting():
    driver = volatility_driver("clock_timestamp", volatile=True)

    review = await one(MigrationLinter(driver), "ALTER TABLE orders ADD COLUMN seen timestamptz DEFAULT clock_timestamp()")

    assert review.rewrites_table is True
    assert "is volatile" in review.detail


# -- Locks that surprise people ---------------------------------------------


@pytest.mark.asyncio
async def test_adding_a_foreign_key_does_not_block_readers(linter):
    """Measured: SHARE ROW EXCLUSIVE, not ACCESS EXCLUSIVE. Most advice says
    otherwise, and it matters — reads keep working throughout."""
    review = await one(linter, "ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (user_id) REFERENCES users(id)")

    assert review.lock is LockLevel.SHARE_ROW_EXCLUSIVE
    assert "reads continue" in review.blocks
    assert review.duration is Duration.SCAN
    assert "NOT VALID" in review.safer[0]
    assert "VALIDATE CONSTRAINT" in review.safer[1]


@pytest.mark.asyncio
async def test_creating_an_index_blocks_writes_but_not_reads(linter):
    review = await one(linter, "CREATE INDEX i ON orders (status)")

    assert review.lock is LockLevel.SHARE
    assert review.safer == ["CREATE INDEX CONCURRENTLY i ON orders (status);"]


@pytest.mark.asyncio
async def test_creating_an_index_concurrently_blocks_nothing(linter):
    review = await one(linter, "CREATE INDEX CONCURRENTLY i ON orders (status)")

    assert review.lock is LockLevel.SHARE_UPDATE_EXCLUSIVE
    assert review.severity is Severity.LOW
    # The catch that actually bites people.
    assert "cannot run inside a transaction" in review.detail


@pytest.mark.asyncio
async def test_validate_constraint_blocks_nothing(linter):
    review = await one(linter, "ALTER TABLE orders VALIDATE CONSTRAINT fk")

    assert review.lock is LockLevel.SHARE_UPDATE_EXCLUSIVE
    assert review.severity is Severity.LOW


@pytest.mark.asyncio
async def test_set_not_null_scans_under_an_exclusive_lock(linter):
    review = await one(linter, "ALTER TABLE orders ALTER COLUMN status SET NOT NULL")

    assert review.lock is LockLevel.ACCESS_EXCLUSIVE
    assert review.duration is Duration.SCAN
    assert review.severity is Severity.HIGH
    assert "NOT VALID" in review.safer[0]
    assert "SET NOT NULL" in review.safer[2]


@pytest.mark.asyncio
async def test_altering_a_column_type_rewrites(linter):
    review = await one(linter, "ALTER TABLE orders ALTER COLUMN user_id TYPE bigint")

    assert review.rewrites_table is True
    assert review.duration is Duration.REWRITE
    assert review.severity is Severity.HIGH


@pytest.mark.asyncio
async def test_dropping_a_column_is_instant(linter):
    """ACCESS EXCLUSIVE, but a catalog update — the wait for the lock is the
    only exposure."""
    review = await one(linter, "ALTER TABLE orders DROP COLUMN note")

    assert review.lock is LockLevel.ACCESS_EXCLUSIVE
    assert review.duration is Duration.INSTANT
    assert review.severity is Severity.LOW
    assert "lock_timeout" in review.detail


@pytest.mark.asyncio
async def test_adopting_an_existing_index_as_a_key_is_instant(linter):
    review = await one(linter, "ALTER TABLE orders ADD CONSTRAINT u UNIQUE USING INDEX orders_ref_key")

    assert review.kind == "add_key_using_index"
    assert review.duration is Duration.INSTANT


@pytest.mark.asyncio
async def test_adding_a_key_without_one_builds_the_index_under_the_lock(linter):
    review = await one(linter, "ALTER TABLE orders ADD CONSTRAINT u UNIQUE (ref)")

    assert review.duration is Duration.SCAN
    assert "CREATE UNIQUE INDEX CONCURRENTLY" in review.safer[0]
    assert "USING INDEX" in review.safer[1]


@pytest.mark.asyncio
async def test_vacuum_full_rewrites_and_blocks_everything(linter):
    review = await one(linter, "VACUUM FULL orders")

    assert review.lock is LockLevel.ACCESS_EXCLUSIVE
    assert review.rewrites_table is True
    assert "pg_repack" in review.safer[0]


# -- Whole migrations -------------------------------------------------------


@pytest.mark.asyncio
async def test_statements_are_ordered_worst_first(linter):
    reviews = await linter.review(
        """
        ALTER TABLE orders DROP COLUMN note;
        ALTER TABLE orders ALTER COLUMN user_id TYPE bigint;
        CREATE INDEX i ON orders (status);
        """
    )

    assert [r.severity for r in reviews] == [Severity.HIGH, Severity.MEDIUM, Severity.LOW]


@pytest.mark.asyncio
async def test_a_statement_is_as_unsafe_as_its_worst_subcommand(linter):
    """One ALTER TABLE holds one lock for all of its parts."""
    review = await one(linter, "ALTER TABLE orders ADD COLUMN n integer, ALTER COLUMN user_id TYPE bigint")

    assert review.rewrites_table is True


@pytest.mark.asyncio
async def test_statements_that_take_no_interesting_lock_are_not_reported(linter):
    assert await linter.review("CREATE TABLE t (id bigint PRIMARY KEY); INSERT INTO t VALUES (1);") == []


@pytest.mark.asyncio
async def test_unparseable_sql_is_rejected_with_its_reason(linter):
    with pytest.raises(InvalidArgumentError, match="Could not parse"):
        await linter.review("ALTER TABLE (")


@pytest.mark.asyncio
async def test_an_empty_migration_is_rejected(linter):
    with pytest.raises(InvalidArgumentError):
        await linter.review("   ")


@pytest.mark.asyncio
async def test_the_statement_text_is_reported_as_written(linter):
    review = await one(linter, "ALTER TABLE   orders\n  DROP COLUMN note")

    assert review.statement == "ALTER TABLE   orders\n  DROP COLUMN note"


# -- Sizing -----------------------------------------------------------------


def sized(name: str, size: int, partitions: list[tuple[str, bool]] | None = None) -> SqlDriver.RowResult:
    """The catalog's answer for one table: its size, and its partitions when it has them."""
    return SqlDriver.RowResult(
        cells={
            "name": name,
            "bytes": size,
            "partitioned": partitions is not None,
            "partitions": [partition for partition, _ in partitions] if partitions else None,
            "partitions_partitioned": [nested for _, nested in partitions] if partitions else None,
        }
    )


def catalog(*rows: SqlDriver.RowResult) -> MagicMock:
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=list(rows))
    return driver


@pytest.mark.asyncio
async def test_a_small_table_softens_the_verdict():
    """The same statement is an outage on a large table and a pause on a small
    one, and only the database knows which this is."""
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[sized("orders", 4 * 1024 * 1024)])

    review = await one(MigrationLinter(driver), "ALTER TABLE orders ALTER COLUMN user_id TYPE bigint")

    assert review.severity is Severity.MEDIUM
    assert "4.0 MB" in review.detail
    assert "pause rather than an outage" in review.detail


@pytest.mark.asyncio
async def test_a_large_table_stays_high():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[sized("orders", 40 * 1024**3)])

    review = await one(MigrationLinter(driver), "ALTER TABLE orders ALTER COLUMN user_id TYPE bigint")

    assert review.severity is Severity.HIGH
    assert "40.0 GB" in review.detail


@pytest.mark.asyncio
async def test_a_database_that_will_not_answer_does_not_stop_the_review():
    """A migration is worth reviewing even against a database that refuses."""
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(side_effect=RuntimeError("permission denied"))

    review = await one(MigrationLinter(driver), "ALTER TABLE orders ALTER COLUMN user_id TYPE bigint")

    assert review.table_bytes is None
    assert review.severity is Severity.HIGH


@pytest.mark.asyncio
async def test_an_instant_statement_is_not_annotated_with_a_size():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[sized("orders", 40 * 1024**3)])

    review = await one(MigrationLinter(driver), "ALTER TABLE orders DROP COLUMN note")

    assert "40.0 GB" not in review.detail, "the size is irrelevant to a catalog update"


def test_the_serialized_form_carries_both_halves_of_the_answer():
    """A lock level without its duration tells the reader nothing."""
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[])
    import asyncio

    review = asyncio.run(MigrationLinter().review("CREATE INDEX i ON orders (status)"))[0]
    payload = review.to_dict()

    assert payload["lock"] == "SHARE"
    assert payload["duration"].startswith("proportional")
    assert payload["blocks"]
    assert payload["safer"]


# -- Partitioned tables -------------------------------------------------------

EVENTS = [("app.events_2025", False), ("app.events_2026", False)]


@pytest.mark.asyncio
async def test_an_index_on_a_partitioned_table_is_built_on_every_partition():
    """The parent has no storage; the build is the partitions', and CONCURRENTLY is refused."""
    review = await one(MigrationLinter(catalog(sized("app.events", 3 * 1024**3, EVENTS))), "CREATE INDEX events_at ON app.events (at)")

    assert review.kind == "create_index_partitioned"
    assert review.severity is Severity.MEDIUM, "SHARE blocks writes, not reads, as on a plain table"
    assert "3.0 GB" in review.detail and "2 partitions" in review.detail
    assert review.safer == [
        "CREATE INDEX events_at ON ONLY app.events (at);",
        "CREATE INDEX CONCURRENTLY events_at_events_2025 ON app.events_2025 (at);",
        "ALTER INDEX app.events_at ATTACH PARTITION app.events_at_events_2025;",
        "CREATE INDEX CONCURRENTLY events_at_events_2026 ON app.events_2026 (at);",
        "ALTER INDEX app.events_at ATTACH PARTITION app.events_at_events_2026;",
    ]
    assert not any(step.startswith("CREATE INDEX CONCURRENTLY events_at ON app.events") for step in review.safer)


@pytest.mark.asyncio
async def test_concurrently_on_a_partitioned_table_fails_and_says_so():
    review = await one(MigrationLinter(catalog(sized("app.events", 1024, EVENTS))), "CREATE INDEX CONCURRENTLY ON app.events (lower(kind))")

    assert review.kind == "create_index_concurrently_partitioned"
    assert review.severity is Severity.HIGH
    assert "fails" in review.summary
    assert review.safer[0] == "CREATE INDEX events_expr_idx ON ONLY app.events ((lower(kind)));"


@pytest.mark.asyncio
async def test_an_index_on_only_the_parent_is_a_catalog_update_with_no_concurrent_rewrite():
    review = await one(MigrationLinter(catalog(sized("app.events", 1024, EVENTS))), "CREATE INDEX events_at ON ONLY app.events (at)")

    assert review.kind == "create_index_on_only"
    assert review.duration is Duration.INSTANT
    assert review.severity is Severity.LOW
    assert not any("CONCURRENTLY events_at ON ONLY" in step for step in review.safer)


@pytest.mark.asyncio
async def test_on_only_reviewed_offline_is_taken_for_a_partitioned_table():
    """ONLY means something only on a partitioned table, so offline it is read as one."""
    review = await one(MigrationLinter(), "CREATE INDEX events_at ON ONLY app.events (at)")

    assert review.kind == "create_index_on_only"
    assert review.safer == []


@pytest.mark.asyncio
async def test_on_only_on_a_plain_table_is_an_ordinary_build():
    review = await one(MigrationLinter(catalog(sized("plain", 1024))), "CREATE INDEX plain_at ON ONLY plain (at)")

    assert review.kind == "create_index"


@pytest.mark.asyncio
async def test_a_partition_that_is_partitioned_itself_gets_a_note_not_a_statement_that_fails():
    review = await one(
        MigrationLinter(catalog(sized("app.events", 1024, [("app.events_2025", False), ("app.events_2026", True)]))),
        "CREATE INDEX events_at ON app.events (at)",
    )

    assert review.safer[-1] == "-- app.events_2026 is partitioned itself: give it the same treatment, ON ONLY and then its partitions."
    assert not any("ON app.events_2026 " in step for step in review.safer)


@pytest.mark.asyncio
async def test_the_recipe_spells_out_the_first_partitions_and_counts_the_rest():
    partitions = [(f"app.events_{n}", False) for n in range(8)]
    review = await one(MigrationLinter(catalog(sized("app.events", 1024, partitions))), "CREATE INDEX events_at ON app.events (at)")

    assert review.safer[-1] == "-- and the same for each of the other 3 partitions."
    assert sum(step.startswith("CREATE INDEX CONCURRENTLY") for step in review.safer) == 5


@pytest.mark.asyncio
async def test_offline_an_ordinary_build_says_what_a_partitioned_table_would_change():
    review = await one(MigrationLinter(), "CREATE INDEX events_at ON events (at)")

    assert "On a partitioned table CONCURRENTLY is refused" in review.detail


# -- Partitions -------------------------------------------------------------
#
# Measured on PostgreSQL 16: CREATE TABLE ... PARTITION OF holds ACCESS
# EXCLUSIVE on the parent and the DEFAULT partition; ATTACH PARTITION holds
# SHARE UPDATE EXCLUSIVE on the parent and ACCESS EXCLUSIVE on the attached
# table and the DEFAULT, and a validated CHECK on each lets it skip the scan;
# DETACH PARTITION holds ACCESS EXCLUSIVE on the parent.


def partitioned(name: str, size: int, default: str | None = None, default_bytes: int = 0, range_key: str | None = "ts") -> SqlDriver.RowResult:
    """The catalog's answer for a partitioned parent: its DEFAULT partition and its range key."""
    row = sized(name, size, [(f"{name}_2026_09", False)])
    row.cells.update({"default_partition": default, "default_bytes": default_bytes if default else None, "range_key": range_key})
    return row


@pytest.mark.asyncio
async def test_attaching_a_table_with_no_check_scans_it_under_access_exclusive(linter):
    review = await one(linter, "ALTER TABLE events ATTACH PARTITION events_new FOR VALUES FROM ('2026-11-01') TO ('2026-12-01')")

    assert review.kind == "attach_partition_scan"
    assert review.lock is LockLevel.ACCESS_EXCLUSIVE
    assert review.table == "events_new"
    assert review.severity is Severity.HIGH
    assert any("VALIDATE CONSTRAINT" in step for step in review.safer)


@pytest.mark.asyncio
async def test_attaching_after_a_validated_check_is_low_and_blocks_nothing_on_the_parent(linter):
    reviews = await linter.review(
        """
        ALTER TABLE events_new ADD CONSTRAINT events_new_bound CHECK (ts >= '2026-11-01' AND ts < '2026-12-01') NOT VALID;
        ALTER TABLE events_new VALIDATE CONSTRAINT events_new_bound;
        ALTER TABLE events ATTACH PARTITION events_new FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
        """
    )
    attach = next(review for review in reviews if review.kind.startswith("attach"))

    assert attach.kind == "attach_partition"
    assert attach.lock is LockLevel.SHARE_UPDATE_EXCLUSIVE
    assert attach.severity is Severity.LOW


@pytest.mark.asyncio
async def test_a_check_added_not_valid_and_never_validated_does_not_count(linter):
    reviews = await linter.review(
        """
        ALTER TABLE events_new ADD CONSTRAINT events_new_bound CHECK (ts >= '2026-11-01') NOT VALID;
        ALTER TABLE events ATTACH PARTITION events_new FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
        """
    )
    assert any(review.kind == "attach_partition_scan" for review in reviews)


@pytest.mark.asyncio
async def test_attaching_a_table_created_empty_in_the_same_migration_is_low(linter):
    reviews = await linter.review(
        "CREATE TABLE events_new (LIKE events);\nALTER TABLE events ATTACH PARTITION events_new FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');"
    )
    (attach,) = reviews
    assert attach.kind == "attach_partition"

    filled = await linter.review(
        "CREATE TABLE events_new (LIKE events);\nINSERT INTO events_new SELECT * FROM staging;\n"
        "ALTER TABLE events ATTACH PARTITION events_new FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');"
    )
    assert [review.kind for review in filled] == ["attach_partition_scan"]


@pytest.mark.asyncio
async def test_attaching_scans_a_default_partition_holding_rows_and_says_how_big_it_is():
    linter = MigrationLinter(catalog(partitioned("events", 4096, default="public.events_default", default_bytes=2 * 1024**3)))
    reviews = await linter.review(
        """
        ALTER TABLE events_new ADD CONSTRAINT events_new_bound CHECK (ts >= '2026-11-01' AND ts < '2026-12-01') NOT VALID;
        ALTER TABLE events_new VALIDATE CONSTRAINT events_new_bound;
        ALTER TABLE events ATTACH PARTITION events_new FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
        """
    )
    attach = reviews[0]

    assert attach.kind == "attach_partition_scans_default"
    assert attach.table == "public.events_default"
    assert attach.severity is Severity.HIGH
    assert "2.0 GB" in attach.detail
    recipe = "\n".join(attach.safer)
    assert "ALTER TABLE public.events_default ADD CONSTRAINT events_default_excludes_events_new CHECK (NOT (ts IS NOT NULL" in recipe
    assert "CREATE TABLE" not in recipe, "the table being attached exists already"


@pytest.mark.asyncio
async def test_the_attach_recipe_passes_its_own_review_against_a_default_holding_rows():
    """The recipe is the safe way, so reviewing it must not raise the alarm it answers."""
    from pg_debug.partition.recipes import attach_partition_recipe

    linter = MigrationLinter(catalog(partitioned("public.events", 4096, default="public.events_default", default_bytes=2 * 1024**3)))
    recipe = attach_partition_recipe("public.events", "public.events_2026_11", "ts", "'2026-11-01'", "'2026-12-01'", default="public.events_default")
    reviews = await linter.review("\n".join(recipe))

    assert {review.severity for review in reviews} == {Severity.LOW}, [(r.kind, r.severity) for r in reviews]


@pytest.mark.asyncio
async def test_create_partition_of_locks_the_parent_and_offers_the_attach_recipe():
    linter = MigrationLinter(catalog(partitioned("events", 4096)))
    review = await one(linter, "CREATE TABLE events_2026_11 PARTITION OF events FOR VALUES FROM ('2026-11-01') TO ('2026-12-01')")

    assert review.kind == "create_partition_of"
    assert review.lock is LockLevel.ACCESS_EXCLUSIVE
    assert review.duration is Duration.INSTANT
    assert review.severity is Severity.LOW
    assert "ALTER TABLE events ATTACH PARTITION events_2026_11 FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');" in "\n".join(review.safer)


@pytest.mark.asyncio
async def test_create_partition_of_on_a_parent_whose_default_holds_rows_scans_it_under_the_parents_lock():
    linter = MigrationLinter(catalog(partitioned("events", 4096, default="public.events_default", default_bytes=50 * 1024**2)))
    review = await one(linter, "CREATE TABLE events_2026_11 PARTITION OF events FOR VALUES FROM ('2026-11-01') TO ('2026-12-01')")

    assert review.kind == "create_partition_of_scans_default"
    assert review.severity is Severity.MEDIUM, "50 MB is a pause rather than an outage"
    assert "public.events_default or events" in review.detail


@pytest.mark.asyncio
async def test_partitions_of_a_parent_created_in_the_same_migration_need_no_recipe(linter):
    reviews = await linter.review(
        "CREATE TABLE events_p (id int, ts date) PARTITION BY RANGE (ts);\n"
        "CREATE TABLE events_p_2026 PARTITION OF events_p FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');"
    )
    (review,) = reviews
    assert review.severity is Severity.LOW
    assert review.safer == []


@pytest.mark.asyncio
async def test_detach_locks_the_parent_and_suggests_concurrently(linter):
    review = await one(linter, "ALTER TABLE events DETACH PARTITION events_2020")
    concurrent = await one(linter, "ALTER TABLE events DETACH PARTITION events_2020 CONCURRENTLY")

    assert review.kind == "detach_partition"
    assert review.lock is LockLevel.ACCESS_EXCLUSIVE
    assert review.safer == ["ALTER TABLE events DETACH PARTITION events_2020 CONCURRENTLY;  -- PostgreSQL 14 and later, outside a transaction block"]
    assert concurrent.kind == "detach_partition_concurrently"
    assert concurrent.lock is LockLevel.SHARE_UPDATE_EXCLUSIVE

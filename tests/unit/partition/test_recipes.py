"""The lock-aware way to add a partition, and the advisor's sketch that carries it."""

import pytest

from pg_debug.migration import MigrationLinter
from pg_debug.partition.advisor import _ddl
from pg_debug.partition.recipes import attach_partition_recipe
from pg_debug.partition.result import Strategy
from pg_debug.severity import Severity


def test_the_recipe_attaches_with_validated_checks_and_drops_them_afterwards():
    steps = attach_partition_recipe("app.events", "app.events_2026_11", "ts", "'2026-11-01'", "'2026-12-01'", default="app.events_default")
    statements = [step for step in steps if not step.startswith("--")]

    assert statements[0] == "CREATE TABLE app.events_2026_11 (LIKE app.events INCLUDING ALL EXCLUDING IDENTITY);"
    assert "CHECK (ts IS NOT NULL AND ts >= '2026-11-01' AND ts < '2026-12-01') NOT VALID;" in statements[1]
    assert statements[2].startswith("ALTER TABLE app.events_2026_11 VALIDATE CONSTRAINT events_2026_11_bound;")
    assert "CHECK (NOT (ts IS NOT NULL AND ts >= '2026-11-01' AND ts < '2026-12-01')) NOT VALID;" in statements[3]
    assert statements[5].startswith("ALTER TABLE app.events ATTACH PARTITION app.events_2026_11 FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');")
    assert statements[-1] == "ALTER TABLE app.events_default DROP CONSTRAINT events_default_excludes_events_2026_11;"


def test_without_a_default_partition_there_is_no_check_to_add_to_it():
    steps = attach_partition_recipe("events", "events_2026_11", "ts", "'2026-11-01'", "'2026-12-01'")

    assert not any("excludes" in step for step in steps)


def test_a_constraint_name_is_quoted_and_cut_where_postgresql_would_cut_it():
    steps = attach_partition_recipe("e", '"Events_' + "x" * 70 + '"', "ts", "'a'", "'b'")
    name = steps[2].split("ADD CONSTRAINT ")[1].split(" CHECK")[0]

    assert name.startswith('"Events_') and len(name.strip('"').encode()) == 63


@pytest.mark.asyncio
async def test_the_advisors_range_sketch_carries_the_recipe_and_passes_review_without_a_high_finding():
    table = {"table_ident": "public.events", "table_bytes": 5 * 1024**3}
    column = {
        "column": "created_at",
        "column_ident": "created_at",
        "bounds": ["2024-01-03 00:00:00+00", "2024-03-30 00:00:00+00"],
    }
    script = "\n".join(_ddl(table, column, Strategy.RANGE, []))

    assert "ALTER TABLE public.events_partitioned ATTACH PARTITION public.events_2024_04 FOR VALUES FROM ('2024-04-01') TO ('2024-05-01');" in script
    reviews = await MigrationLinter().review(script)
    assert Severity.HIGH not in {review.severity for review in reviews}, [(r.kind, r.severity) for r in reviews]

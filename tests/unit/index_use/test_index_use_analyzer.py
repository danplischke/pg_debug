"""The analysis over a mocked catalog and planner: which reason fires, and which stays quiet."""

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug import QueryContext
from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.errors import InvalidArgumentError
from pg_debug.index_use import IndexUseAnalyzer
from pg_debug.index_use import IndexUseReason
from pg_debug.index_use.analyzer import _resolve_tables
from pg_debug.model import Confidence

ORDERS = ("public", "orders")


def index_row(name, columns, method="btree", valid=True, predicate=None, opclasses=None, table_rows=100_000.0, table_pages=1_000):
    return {
        "schema": "public",
        "table": "orders",
        "index": name,
        "method": method,
        "valid": valid,
        "ready": True,
        "unique": False,
        "key_columns": len(columns),
        "columns": columns,
        "opclasses": opclasses or ["text_ops"] * len(columns),
        "predicate": predicate,
        "definition": f"CREATE INDEX {name} ON public.orders USING {method} ({', '.join(columns)})",
        "size_bytes": 8192,
        "table_rows": table_rows,
        "table_pages": table_pages,
    }


def column_rows(*columns):
    return [{"schema": "public", "table": "orders", "column": c, "type": "text" if c in ("status", "email", "region") else "bigint"} for c in columns]


def scan(node_type="Seq Scan", relation="orders", index=None, plan_rows=1000, cost=500.0, **fields):
    node = {
        "Node Type": node_type,
        "Relation Name": relation,
        "Startup Cost": 0.0,
        "Total Cost": cost,
        "Plan Rows": plan_rows,
        "Plan Width": 8,
    }
    if index:
        node["Index Name"] = index
    node.update(fields)
    return [{"Plan": node}]


def driver(indexes, columns, plan, forced_plan=None, stale=(), collation="en_US.UTF-8", restricted=False, context=None):
    """A driver answering each query the analysis makes by what it contains.

    An EXPLAIN run in a context that turns sequential scans off gets the
    forced plan; the context is the only way a setting reaches a plan. Every
    driver ``with_context`` makes records into the same log.
    """
    log: list[tuple[str, bool, QueryContext | None]] = []

    def make(context: QueryContext | None) -> Any:
        inner = MagicMock(spec=SqlDriver)
        inner.context = context

        async def execute(query, *args, in_context=False, **kwargs):
            applied = context if in_context else None
            log.append((query, in_context, applied))
            if "pg_index i" in query:
                return [SqlDriver.RowResult(cells=row) for row in indexes]
            if "pg_attribute a" in query:
                return [SqlDriver.RowResult(cells=row) for row in columns]
            if query.startswith("EXPLAIN"):
                forced = applied is not None and applied.settings.get("enable_seqscan") == "off"
                chosen = forced_plan if forced and forced_plan is not None else plan
                return [SqlDriver.RowResult(cells={"QUERY PLAN": chosen})]
            if "datcollate" in query:
                return [SqlDriver.RowResult(cells={"collation": collation})]
            if "pg_stat_user_tables" in query:
                return [SqlDriver.RowResult(cells=row) for row in stale]
            return []

        inner.execute_query = AsyncMock(side_effect=execute)
        inner.with_context = make
        inner.log = log

        @asynccontextmanager
        async def session():
            yield inner

        inner.session = session
        return inner

    inner = make(context)
    if restricted:
        return SafeSqlDriver(inner)
    return inner


def log(d) -> list[tuple[str, bool, QueryContext | None]]:
    """Each query, whether it was asked to run in context, and the context it ran in."""
    inner: Any = d.sql_driver if isinstance(d, SafeSqlDriver) else d
    return inner.log


def asked(d) -> list[str]:
    return [query for query, _, _ in log(d)]


def forced_explains(d) -> list[QueryContext]:
    """The contexts the EXPLAINs with sequential scans off ran in."""
    return [c for query, _, c in log(d) if query.startswith("EXPLAIN") and c is not None and c.settings.get("enable_seqscan") == "off"]


async def analyze(d, sql, index=None):
    return await IndexUseAnalyzer(d).analyze(sql, index=index)


# -- Used -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_index_the_plan_reads_is_reported_used_with_its_node():
    d = driver([index_row("orders_status", ["status"])], column_rows("id", "status"), scan("Index Scan", index="orders_status"))

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE status = 'open'")

    assert verdict.used is True
    assert verdict.node_type == "Index Scan"
    assert verdict.findings == []
    assert forced_explains(d) == [], "nothing to force when every index is used"


# -- Structural reasons -----------------------------------------------------


@pytest.mark.asyncio
async def test_an_invalid_index_is_the_whole_answer():
    d = driver([index_row("orders_status", ["status"], valid=False)], column_rows("id", "status"), scan())

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE status = 'open'")

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.INVALID
    assert finding.confidence is Confidence.PROVEN
    assert finding.remediation == ["REINDEX INDEX CONCURRENTLY orders_status;"]


@pytest.mark.asyncio
async def test_a_query_that_wraps_the_column_does_not_match_a_bare_index():
    d = driver([index_row("orders_email", ["email"])], column_rows("id", "email"), scan(), forced_plan=scan())

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE lower(email) = 'a@b.c'")

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.EXPRESSION_MISMATCH
    assert finding.confidence is Confidence.CANDIDATE
    assert finding.remediation == ["CREATE INDEX ON public.orders (lower(email));"]


@pytest.mark.asyncio
async def test_a_bare_predicate_does_not_match_an_expression_index():
    d = driver([index_row("orders_lower_email", ["lower(email)"])], column_rows("id", "email"), scan(), forced_plan=scan())

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE email = 'a@b.c'")

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.EXPRESSION_MISMATCH
    assert "lower(email)" in finding.summary


@pytest.mark.asyncio
async def test_the_cast_spelling_from_the_catalog_matches_the_query_s():
    """``(created_at)::date`` in the catalog is ``CAST(created_at AS date)``
    to the parser; they are the same key and must not be reported as a mismatch."""
    d = driver(
        [index_row("orders_day", ["(created_at)::date"])],
        column_rows("id", "created_at"),
        scan(),
        forced_plan=scan("Index Scan", index="orders_day", cost=50.0),
    )

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE created_at::date = '2026-01-01'")

    assert [f.reason for f in verdict.findings] == [IndexUseReason.PLANNER_PREFERRED_OTHER]


@pytest.mark.asyncio
async def test_restricting_the_second_column_only_names_the_first():
    d = driver([index_row("orders_status_region", ["status", "region"])], column_rows("id", "status", "region"), scan(), forced_plan=scan())

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE region = 'eu'")

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.NOT_LEADING_COLUMN
    assert "column 2" in finding.summary
    assert finding.remediation[0] == "CREATE INDEX ON public.orders (region);"


@pytest.mark.asyncio
async def test_no_predicate_on_any_index_column():
    d = driver([index_row("orders_status", ["status"])], column_rows("id", "status", "region"), scan(), forced_plan=scan())

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE region = 'eu'")

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.NO_PREDICATE
    assert finding.evidence == {"index_columns": ["status"], "restricted": ["region"]}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders WHERE status <> 'open'",
        "SELECT * FROM orders WHERE status NOT IN ('open', 'paid')",
        "SELECT * FROM orders WHERE NOT (status = 'open')",
    ],
)
@pytest.mark.asyncio
async def test_an_exclusion_cannot_be_looked_up(sql):
    d = driver([index_row("orders_status", ["status"])], column_rows("id", "status"), scan(), forced_plan=scan())

    (verdict,) = await analyze(d, sql)

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.OPERATOR_NOT_INDEXABLE
    assert "excludes" in finding.summary


@pytest.mark.asyncio
async def test_a_leading_wildcard_needs_trigrams():
    d = driver([index_row("orders_email", ["email"])], column_rows("id", "email"), scan(), forced_plan=scan())

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE email LIKE '%@example.com'")

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.OPERATOR_NOT_INDEXABLE
    assert "gin_trgm_ops" in finding.remediation[1]


@pytest.mark.asyncio
async def test_a_prefix_pattern_under_a_locale_collation_needs_pattern_ops():
    d = driver([index_row("orders_email", ["email"])], column_rows("id", "email"), scan(), forced_plan=scan(), collation="en_US.UTF-8")

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE email LIKE 'ann%'")

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.PATTERN_NEEDS_OPCLASS
    assert finding.remediation == ["CREATE INDEX ON public.orders (email text_pattern_ops);"]


@pytest.mark.asyncio
async def test_a_prefix_pattern_under_the_c_collation_is_fine():
    forced = scan("Index Scan", index="orders_email", cost=20.0)
    d = driver([index_row("orders_email", ["email"])], column_rows("id", "email"), scan(), forced_plan=forced, collation="C")

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE email LIKE 'ann%'")

    assert [f.reason for f in verdict.findings] == [IndexUseReason.PLANNER_PREFERRED_OTHER]


@pytest.mark.asyncio
async def test_a_cast_the_plan_applies_to_the_column_is_a_type_mismatch():
    plan = scan(Filter="((orders.customer_id)::numeric = '7'::numeric)")
    d = driver([index_row("orders_customer", ["customer_id"])], column_rows("id", "customer_id"), plan, forced_plan=plan)

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE customer_id = 7::numeric")

    reasons = [f.reason for f in verdict.findings]
    assert IndexUseReason.TYPE_MISMATCH in reasons
    mismatch = next(f for f in verdict.findings if f.reason is IndexUseReason.TYPE_MISMATCH)
    assert mismatch.confidence is Confidence.PROVEN
    assert mismatch.evidence["cast_to"] == "numeric"
    assert mismatch.evidence["column_type"] == "bigint"


@pytest.mark.asyncio
async def test_a_partial_index_the_query_does_not_mention():
    d = driver(
        [index_row("orders_open_created", ["created_at"], predicate="(status = 'open'::text)")],
        column_rows("id", "status", "created_at"),
        scan(),
        forced_plan=scan(),
    )

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE created_at > '2026-01-01'")

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.PARTIAL_PREDICATE_NOT_MATCHED
    assert finding.evidence["index_predicate_columns"] == ["status"]


@pytest.mark.asyncio
async def test_an_or_spanning_tables_cannot_use_a_single_table_index():
    columns = [*column_rows("id", "status"), {"schema": "public", "table": "customers", "column": "vip", "type": "boolean"}]
    d = driver([index_row("orders_status", ["status"])], columns, scan(), forced_plan=scan())

    (verdict,) = await analyze(d, "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.status = 'open' OR c.vip")

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.OR_ACROSS_TABLES


# -- The planner's own answer -----------------------------------------------


@pytest.mark.asyncio
async def test_when_forcing_makes_the_planner_use_it_the_reason_is_cost():
    chosen = scan(plan_rows=60_000, cost=1_800.0)
    forced = scan("Index Scan", index="orders_status", plan_rows=60_000, cost=4_200.0)
    d = driver([index_row("orders_status", ["status"])], column_rows("id", "status"), chosen, forced_plan=forced)

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE status = 'closed'")

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.PLANNER_PREFERRED_OTHER
    assert finding.confidence is Confidence.PROVEN
    assert finding.evidence["chosen_cost"] == 1_800.0
    assert finding.evidence["forced_cost"] == 4_200.0
    assert finding.evidence["share"] == pytest.approx(0.6)
    assert finding.remediation == ["ANALYZE public.orders;"]
    assert "60.0%" in finding.detail


@pytest.mark.asyncio
async def test_the_planner_s_answer_overrides_a_text_comparison():
    """The text said 'expression mismatch'; the planner used the index when
    forced. The planner is right and the comparison was not."""
    forced = scan("Index Scan", index="orders_email", cost=90.0)
    d = driver([index_row("orders_email", ["email"])], column_rows("id", "email"), scan(cost=10.0), forced_plan=forced)

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE lower(email) = 'x'")

    assert [f.reason for f in verdict.findings] == [IndexUseReason.PLANNER_PREFERRED_OTHER]


@pytest.mark.asyncio
async def test_a_tiny_table_says_so_instead_of_arguing_selectivity():
    forced = scan("Index Scan", index="orders_status", plan_rows=1, cost=8.3)
    d = driver(
        [index_row("orders_status", ["status"], table_rows=20.0, table_pages=1)],
        column_rows("id", "status"),
        scan(plan_rows=1, cost=1.25),
        forced_plan=forced,
    )

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE status = 'open'")

    (finding,) = verdict.findings
    assert "1 page" in finding.detail
    assert finding.remediation == []


@pytest.mark.asyncio
async def test_nothing_explains_it_is_said_rather_than_guessed():
    d = driver([index_row("orders_status", ["status"])], column_rows("id", "status"), scan(), forced_plan=scan(cost=1e10))

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE status = 'open'")

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.UNEXPLAINED
    assert finding.confidence is Confidence.CANDIDATE


STALE = [
    {"schema": "public", "table": "orders", "last_analyzed": "2026-01-01", "changed_rows": 50_000, "changed_fraction": 0.5, "never_analyzed": False}
]


@pytest.mark.asyncio
async def test_stale_statistics_ride_on_a_verdict_that_rests_on_the_estimate():
    forced = scan("Index Scan", index="orders_status", plan_rows=60_000, cost=4_200.0)
    d = driver(
        [index_row("orders_status", ["status"])], column_rows("id", "status"), scan(plan_rows=60_000, cost=1_800.0), forced_plan=forced, stale=STALE
    )

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE status = 'closed'")

    assert [f.reason for f in verdict.findings] == [IndexUseReason.PLANNER_PREFERRED_OTHER, IndexUseReason.STALE_STATISTICS]
    assert verdict.findings[1].remediation == ["ANALYZE public.orders;"]


@pytest.mark.asyncio
async def test_stale_statistics_do_not_ride_on_a_structural_verdict():
    """No predicate on the index is true whatever the statistics say."""
    d = driver([index_row("orders_status", ["status"])], column_rows("id", "status", "region"), scan(), forced_plan=scan(), stale=STALE)

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE region = 'eu'")

    assert [f.reason for f in verdict.findings] == [IndexUseReason.NO_PREDICATE]


@pytest.mark.asyncio
async def test_another_index_the_planner_prefers_is_named():
    """Sequential scans off or on, the planner reads the table through a
    different index. That is not unexplained; it is second place."""
    rows = [index_row("orders_status", ["status"]), index_row("orders_open", ["created_at"], predicate="(status = 'open'::text)")]
    through_other = scan("Index Scan", index="orders_open", cost=120.0)
    d = driver(rows, column_rows("id", "status", "created_at"), through_other, forced_plan=through_other)

    verdicts = await analyze(d, "SELECT * FROM orders WHERE status = 'open'")

    status = next(v for v in verdicts if v.index == "orders_status")
    (finding,) = status.findings
    assert finding.reason is IndexUseReason.PLANNER_PREFERRED_OTHER
    assert finding.evidence["preferred_index"] == "orders_open"
    assert "second" in finding.detail


@pytest.mark.asyncio
async def test_restricted_mode_takes_the_forced_plan_too():
    """Sequential scans go off for the EXPLAIN's transaction alone, which writes nothing."""
    forced = scan("Index Scan", index="orders_status", plan_rows=60_000, cost=4_200.0)
    d = driver([index_row("orders_status", ["status"])], column_rows("id", "status"), scan(), forced_plan=forced, restricted=True)

    (verdict,) = await analyze(d, "SELECT * FROM orders WHERE status = 'open'")

    (finding,) = verdict.findings
    assert finding.reason is IndexUseReason.PLANNER_PREFERRED_OTHER
    assert verdict.notes == []


@pytest.mark.asyncio
async def test_the_forced_plan_turns_sequential_scans_off_in_its_own_transaction():
    """Not with a SET before it and a RESET after: three transactions, which a
    transaction pooler may send to three backends."""
    d = driver([index_row("orders_status", ["status"])], column_rows("id", "status"), scan(), forced_plan=scan())

    await analyze(d, "SELECT * FROM orders WHERE status = 'open'")

    assert len(forced_explains(d)) == 1
    assert not any(query.lstrip().upper().startswith(("SET", "RESET")) for query in asked(d))


@pytest.mark.asyncio
async def test_the_forced_plan_keeps_the_query_context():
    """Planned as the application, with sequential scans off as well."""
    app = QueryContext(role="app_user", settings={"app.tenant_id": "t7"})
    d = driver([index_row("orders_status", ["status"])], column_rows("id", "status"), scan(), forced_plan=scan(), context=app)

    await analyze(d, "SELECT * FROM orders WHERE status = 'open'")

    (context,) = forced_explains(d)
    assert context == app.with_overrides(settings={"enable_seqscan": "off"})


# -- Selecting and refusing -------------------------------------------------


@pytest.mark.asyncio
async def test_the_index_argument_narrows_the_report():
    rows = [index_row("orders_status", ["status"]), index_row("orders_region", ["region"])]
    d = driver(rows, column_rows("id", "status", "region"), scan(), forced_plan=scan())

    verdicts = await analyze(d, "SELECT * FROM orders WHERE status = 'open'", index="public.orders_region")

    assert [v.index for v in verdicts] == ["orders_region"]


@pytest.mark.asyncio
async def test_an_index_not_on_the_query_s_tables_is_refused():
    d = driver([index_row("orders_status", ["status"])], column_rows("id", "status"), scan())

    with pytest.raises(InvalidArgumentError, match="No index named nope"):
        await analyze(d, "SELECT * FROM orders WHERE status = 'open'", index="nope")


@pytest.mark.asyncio
async def test_unused_indexes_come_first():
    rows = [index_row("orders_status", ["status"]), index_row("orders_region", ["region"])]
    d = driver(
        rows, column_rows("id", "status", "region"), scan("Index Scan", index="orders_status"), forced_plan=scan("Index Scan", index="orders_status")
    )

    verdicts = await analyze(d, "SELECT * FROM orders WHERE status = 'open'")

    assert [(v.index, v.used) for v in verdicts] == [("orders_region", False), ("orders_status", True)]


def test_bare_table_references_resolve_through_what_the_catalog_returned():
    rows = [{"schema": "shop", "table": "orders", "column": "id"}, {"schema": "public", "table": "customers", "column": "id"}]

    assert _resolve_tables({"orders", "public.customers", "missing"}, rows) == {"orders": "shop.orders", "public.customers": "public.customers"}


# -- The query context ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_plans_are_taken_in_the_query_context_and_the_catalog_is_read_outside_it():
    d = driver(
        [index_row("orders_status", ["status"])],
        column_rows("id", "status"),
        scan(),
        forced_plan=scan("Index Scan", index="orders_status", cost=900.0),
    )

    await analyze(d, "SELECT * FROM orders WHERE status = 'open'")

    in_context = [query for query, asked_for, _ in log(d) if asked_for]
    outside = [query for query, asked_for, _ in log(d) if not asked_for]
    assert len(in_context) == 2, "the plan and the plan with sequential scans disabled"
    assert all(query.startswith("EXPLAIN") for query in in_context)
    assert not any(query.startswith("EXPLAIN") for query in outside)
    assert any("pg_index i" in query for query in outside)

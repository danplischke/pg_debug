"""The plan rules that read the catalog, against a real server.

Supabase's pattern is the case that matters most: a policy written
``USING (user_id = auth.uid())`` calls ``auth.uid()`` for every row the scan
tests, and ``(SELECT auth.uid())`` calls it once. Run as the table's owner the
policy does not apply at all, so these run under the query context's role.
"""

from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug import QueryContext
from pg_debug.plans import PlanIssue
from pg_debug.severity import Confidence

ROLE = "pgdebug_rules_authenticated"
USER = "00000000-0000-0000-0000-000000000007"
CORPUS = Path(__file__).parents[1] / "unit" / "plans" / "plans"


@pytest.fixture(scope="class")
def database(test_postgres_connection_string):
    url = test_postgres_connection_string[0]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {}) THEN CREATE ROLE {} NOLOGIN; END IF; END $$").format(
                sql.Literal(ROLE), sql.Identifier(ROLE)
            )
        )
        conn.execute("CREATE SCHEMA IF NOT EXISTS auth")
        # PL/pgSQL, so the planner cannot inline it and the plan names it.
        conn.execute(
            "CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE plpgsql STABLE AS "
            "$$ BEGIN RETURN nullif(current_setting('request.jwt.claim.sub', true), '')::uuid; END $$"
        )
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA auth TO {}").format(sql.Identifier(ROLE)))
        conn.execute("DROP TABLE IF EXISTS public.todos")
        conn.execute("CREATE TABLE public.todos (id bigint PRIMARY KEY, user_id uuid NOT NULL, done bool NOT NULL DEFAULT false, tags jsonb)")
        conn.execute(
            "INSERT INTO public.todos SELECT g, ('00000000-0000-0000-0000-' || lpad((g % 500)::text, 12, '0'))::uuid, false, "
            "jsonb_build_object('n', g % 1000) FROM generate_series(1, 100000) g"
        )
        conn.execute("CREATE INDEX todos_tags_gin ON public.todos USING gin (tags)")
        conn.execute("ALTER TABLE public.todos ENABLE ROW LEVEL SECURITY")
        conn.execute(sql.SQL("CREATE POLICY todos_owner ON public.todos FOR SELECT TO {} USING (user_id = auth.uid())").format(sql.Identifier(ROLE)))
        conn.execute(sql.SQL("GRANT SELECT ON public.todos TO {}").format(sql.Identifier(ROLE)))

        conn.execute("DROP TABLE IF EXISTS public.children, public.parents")
        conn.execute("CREATE TABLE public.parents (id bigint PRIMARY KEY)")
        conn.execute("CREATE TABLE public.children (id bigint PRIMARY KEY, parent_id bigint REFERENCES public.parents (id) ON DELETE CASCADE)")
        conn.execute("INSERT INTO public.parents SELECT generate_series(1, 500)")
        conn.execute("INSERT INTO public.children SELECT g, (g % 500) + 1 FROM generate_series(1, 200000) g")

        conn.execute("DROP TABLE IF EXISTS public.users")
        conn.execute("CREATE TABLE public.users (id bigint PRIMARY KEY, email text NOT NULL)")
        conn.execute("INSERT INTO public.users SELECT g, 'User' || g || '@Example.com' FROM generate_series(1, 200000) g")
        conn.execute("CREATE INDEX users_email_idx ON public.users (email)")
        conn.execute("ANALYZE public.todos, public.parents, public.children, public.users")
    return url


def as_app() -> QueryContext:
    return QueryContext(role=ROLE, settings={"request.jwt.claim.sub": USER})


def issues(findings, issue):
    return [finding for finding in findings if finding.issue is issue]


class TestPlanRules:
    @pytest.mark.asyncio
    async def test_a_policy_calling_a_stable_function_per_row_is_named_with_its_rewrite(self, database):
        async with PgDebug(database, context=as_app()) as app:
            findings = await app.diagnose_query("SELECT * FROM public.todos WHERE NOT done")

        (finding,) = issues(findings, PlanIssue.RLS_PER_ROW_CALL)
        assert finding.confidence is Confidence.PROVEN
        assert finding.evidence["policy"] == "todos_owner"
        assert finding.evidence["functions"] == ["auth.uid"]
        assert finding.remediation == ['ALTER POLICY "todos_owner" ON "public"."todos" USING (user_id = (SELECT auth.uid()));']
        # The discarded rows are the call's to explain, not an index's.
        assert not issues(findings, PlanIssue.DISCARDED_ROWS)

    @pytest.mark.asyncio
    async def test_the_rewrite_it_recommends_makes_the_call_once(self, database):
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute("ALTER POLICY todos_owner ON public.todos USING (user_id = (SELECT auth.uid()))")
        try:
            async with PgDebug(database, context=as_app()) as app:
                findings = await app.diagnose_query("SELECT * FROM public.todos WHERE NOT done")
            assert not issues(findings, PlanIssue.RLS_PER_ROW_CALL)
        finally:
            with psycopg.connect(database, autocommit=True) as conn:
                conn.execute("ALTER POLICY todos_owner ON public.todos USING (user_id = auth.uid())")

    @pytest.mark.asyncio
    async def test_as_the_owner_the_policy_does_not_apply_and_nothing_is_said_of_it(self, database):
        async with PgDebug(database) as owner:
            findings = await owner.diagnose_query("SELECT * FROM public.todos WHERE NOT done")
        assert not issues(findings, PlanIssue.RLS_PER_ROW_CALL)

    @pytest.mark.asyncio
    async def test_a_leaky_operator_under_the_policy_is_kept_from_its_index(self, database):
        async with PgDebug(database, context=as_app()) as app:
            findings = await app.diagnose_query("""SELECT id FROM public.todos WHERE tags @> '{"n": 7}'""")

        (finding,) = issues(findings, PlanIssue.RLS_LEAKY_PREDICATE)
        assert finding.evidence["not_leakproof"] == ["@>"]
        assert finding.evidence["indexes"] == ["todos_tags_gin"]
        assert finding.confidence is Confidence.CANDIDATE

    @pytest.mark.asyncio
    async def test_a_foreign_key_trigger_without_an_index_names_the_index(self, database):
        async with PgDebug(database) as pg:
            findings = await pg.diagnose_query("DELETE FROM public.parents WHERE id <= 20")

        (finding,) = issues(findings, PlanIssue.TRIGGER_TIME)
        assert finding.evidence["constraint"] == "children_parent_id_fkey"
        assert finding.evidence["indexed"] is False
        assert finding.remediation == ['CREATE INDEX CONCURRENTLY ON "public"."children" (parent_id);']
        assert finding.confidence is Confidence.PROVEN

    @pytest.mark.asyncio
    async def test_a_function_over_an_indexed_column_suggests_the_expression_index(self, database):
        async with PgDebug(database) as pg:
            findings = await pg.diagnose_query("SELECT * FROM public.users WHERE lower(email) = 'user77@example.com'")

        (finding,) = issues(findings, PlanIssue.FUNCTION_ON_INDEXED_COLUMN)
        assert finding.evidence["index"] == "users_email_idx"
        assert finding.remediation == ['CREATE INDEX CONCURRENTLY ON "public"."users" ((lower(email)));']
        assert not issues(findings, PlanIssue.DISCARDED_ROWS)

    @pytest.mark.asyncio
    async def test_restricted_mode_reads_the_catalog_for_a_plan_handed_over(self, database):
        """Every catalog read the rules make passes restricted mode's checks."""
        async with PgDebug(database, access_mode=AccessMode.RESTRICTED) as pg:
            findings = await pg.diagnose_plan((CORPUS / "rls_per_row.json").read_text())

        (finding,) = issues(findings, PlanIssue.RLS_PER_ROW_CALL)
        assert finding.evidence["policy"] == "todos_owner"

    @pytest.mark.asyncio
    async def test_a_planner_choice_names_what_could_pin_the_plan(self, database):
        async with PgDebug(database, access_mode=AccessMode.RESTRICTED) as pg:
            findings = await pg.diagnose_plan((CORPUS / "order_by_limit.json").read_text())

        (finding,) = issues(findings, PlanIssue.LIMIT_SCAN_FILTER)
        assert finding.plan_control, "a planner choice carries the plan-control advice"
        assert all("pg-debug" not in line or "does not detect" in line for line in finding.plan_control)

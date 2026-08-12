---
name: pg-debug
description: Diagnose and tune a PostgreSQL database with the pg_debug library and its pg-debug CLI — inspect schemas and tables, read EXPLAIN plans, test hypothetical indexes without creating them, see what every backend is doing right now and which one is blocking the others, track vacuum progress, check planner statistics behind bad row estimates, run health checks (bloated/duplicate/unused indexes, vacuum wraparound, sequence exhaustion, replication, cache hit rates, invalid constraints, stale statistics), find the most expensive queries from pg_stat_statements, and get index recommendations. Use this skill whenever the user asks why a Postgres query is slow, why the database is stuck or blocked, whether an index would help, what indexes to add, whether their database is healthy, what tables or columns exist, or wants to script any of that in Python — even if they never mention pg_debug by name. Also use it when writing code against the pg_debug package.
---

# pg_debug

`pg_debug` answers questions about a live PostgreSQL database: what is in it,
why a query is slow, whether an index would help, and whether anything is
quietly rotting. It is a Python library (`PgDebug`, async; `SyncPgDebug`,
blocking) with a `pg-debug` CLI on top.

## Before anything else: connect

Every command needs a connection URL. Prefer the environment variable so the
password stays out of shell history and process listings:

```bash
export DATABASE_URI=postgresql://user:pass@host:5432/dbname
```

`-d/--database-url` overrides it. `$DATABASE_URL` works as a fallback.

**When exploring a database you do not own, add `--access-mode restricted`.**
It parses every statement, rejects anything that is not read-only, runs it in a
read-only transaction and applies a 30 second timeout. It is the difference
between a diagnostic tool and a loaded gun:

```bash
pg-debug --access-mode restricted execute "SELECT count(*) FROM orders"
```

In unrestricted mode there is no timeout unless you ask for one. On a
production database, `--timeout 10` is cheap insurance against an accidental
sequential scan.

## Diagnosing a slow query

Work outside in — plan first, then indexes, then confirm.

```bash
# 1. What is the planner doing?
pg-debug explain "SELECT * FROM orders WHERE user_id = 42 AND status = 'paid'"

# 2. What would it really do? (runs the query)
pg-debug explain --analyze "SELECT * FROM orders WHERE user_id = 42"

# 3. Would an index help? Nothing is created — HypoPG simulates it.
pg-debug explain "SELECT * FROM orders WHERE user_id = 42" -i 'orders(user_id)'

# 4. Or let the advisor search for candidates itself.
pg-debug tune-queries "SELECT * FROM orders WHERE user_id = 42 AND status = 'paid'"
```

`--analyze` executes the query, so do not use it for `UPDATE`/`DELETE` unless
you mean it. It also cannot be combined with `-i`.

Read the plan for the shape of the problem: a `Seq Scan` with a selective
`Filter` wants an index; a huge row-estimate error (`Plan Rows` far from
`Actual Rows`) usually means stale statistics — suggest `ANALYZE` before
suggesting indexes.

Step 3 is the honest way to answer "would this index help?" — it reports the
planner's cost with the index in place, without touching the database. Compare
`Total Cost` before and after and say what changed rather than asserting an
index is a good idea.

## Recommending indexes

```bash
pg-debug tune-queries "SELECT ..." "SELECT ..."   # up to 10 queries
pg-debug tune-workload --max-index-size-mb 500    # from pg_stat_statements
```

Each recommendation carries an estimated size and the planner cost before and
after, individually and cumulatively. Report those numbers rather than just the
DDL — the improvement multiple is what justifies the write cost of a new index.

Two things routinely surprise people:

- **Requires HypoPG.** Without it, tuning returns an `error` explaining how to
  install it. `top-queries` and `tune-workload` additionally need
  `pg_stat_statements`. No superuser is needed — pg_debug uses HypoPG's
  functions, which any role can execute, not its views, which are private to
  the role that created the extension.
- **Unqualified names resolve through the `search_path`.** Schema-qualified
  names (`reporting.orders`) work directly. For unqualified queries against
  another schema, pass `--search-path reporting,public`.

"No index recommendations found" is a real answer, not a failure — it usually
means a suitable index already exists or the planner does not think one helps.
Check `pg-debug describe <table>` before assuming something went wrong.

## Something is wrong right now

When the complaint is "the database is stuck" rather than "this query is slow",
start with what is actually running:

```bash
pg-debug activity                    # everything doing something
pg-debug activity --blocked          # only backends waiting on a lock
pg-debug activity --min-duration 30  # anything stuck in its current state 30s+
pg-debug vacuum-progress             # is that vacuum going to finish?
```

`activity` hides idle backends by default and excludes its own connection.
Each row carries `blocked_by` — the pids holding the lock it is waiting for —
so a lock pile-up reads directly off the output: find the backend with an empty
`blocked_by` that others point at, and that is the one to deal with.

`wait_event_type` says what kind of wait it is: `Lock` means another backend,
`IO` means storage, `Timeout` usually means the query is deliberately sleeping,
and `LWLock` means internal contention. A long-running `active` query with no
wait event is simply working.

`vacuum-progress` pairs with the vacuum health check: the check tells you
wraparound is approaching, this tells you whether the vacuum fixing it is
moving. Watch `percent_scanned` across two calls rather than guessing from one.

## Why the planner got it wrong

When `explain --analyze` shows estimated rows far from actual rows, no index
will fix it — the planner is working from bad numbers. Look at them:

```bash
pg-debug column-stats orders
pg-debug health --type statistics
```

`column-stats` shows what the planner believes per column: `null_frac`,
`n_distinct` (negative means a multiple of the row count, so `-1` is unique),
`correlation` (how closely physical order tracks the column, which decides
whether an index scan looks cheap) and how many most-common values it tracks.
It also lists any extended statistics on the table.

Two common findings, and what they mean:

- **Stale or missing statistics.** `health --type statistics` flags tables
  never analyzed, or where a large fraction of rows changed since. The fix is
  `ANALYZE <table>`, and it is worth doing before any index work.
- **Correlated columns.** If a two-column predicate estimates far too few rows
  and each column's own statistics look right, the planner is multiplying
  independent selectivities for columns that are not independent. That is what
  `CREATE STATISTICS ... ON a, b FROM t` fixes — check `column-stats` first to
  see whether such an object already exists.

## Health checks

```bash
pg-debug health                          # everything
pg-debug health --type index,vacuum      # a subset
pg-debug --json health                   # structured, for scripts
pg-debug health --exit-code              # exit 3 if anything is unhealthy (CI)
```

Checks: `index` (invalid, duplicate, bloated, unused), `connection`, `vacuum`
(transaction ID wraparound), `sequence` (approaching maximum), `replication`,
`buffer` (cache hit rates), `constraint` (invalid constraints), `statistics`
(never analyzed or drifted), `all`.

In JSON each check has `healthy`, `summary`, `findings` (the flagged objects)
and `metrics` (the numbers and thresholds behind the verdict). Filter on
`healthy` rather than parsing prose:

```bash
pg-debug --json health | jq 'to_entries | map(select(.value.healthy == false))'
```

Interpret before relaying. "Unused index" on a database that restarted
yesterday means the counters are young, not that the index is dead — check
`metrics.max_scans` and how long statistics have been accumulating. Cache hit
rate on an idle test database is meaningless.

## Finding expensive queries

```bash
pg-debug top-queries                                  # blended resource cost
pg-debug top-queries --sort-by mean_time --limit 5    # slowest per call
pg-debug top-queries --sort-by total_time --limit 5   # biggest total consumer
pg-debug top-queries --interval 10                    # measure the next 10s only
```

`resources` blends execution time, buffer access and WAL volume, and reports
everything above a threshold — `--limit` does not apply to it in cumulative
mode. Use `mean_time` to find individually slow queries and `total_time` to
find what the database actually spends its life doing; they often disagree, and
the disagreement is the interesting part.

**Prefer `--interval` when the question is about now.** Without it the counters
are cumulative since statistics were last reset, so the ranking is often
dominated by a migration or a bulk load that ran once, months ago. Sampling
takes two snapshots and reports the difference — the live workload, with
`mean_exec_time` recomputed over the interval. A few seconds is enough on a
busy server; on a quiet one, sample longer or you will rank noise.

Needs `pg_stat_statements`. Query text comes back normalized (`$1` for
literals), which is what you feed to `tune-queries`.

## Exploring a schema

```bash
pg-debug schemas
pg-debug objects public                    # tables; -t view|sequence|extension
pg-debug describe public.orders            # columns, constraints, indexes
pg-debug describe orders --schema reporting
```

`describe` is the fastest way to check what indexes already exist before
recommending more.

## Scripting it

Add `--json` and the output is machine-readable. Everything except `explain`
returns rows or an object; `explain --json` returns the raw plan document.

```bash
pg-debug --json objects public | jq -r '.[].name'
pg-debug --json explain - < slow.sql | jq '.Plan."Total Cost"'
```

Exit status: `0` success, `1` error, `2` usage error, `3` for
`health --exit-code` when a check is unhealthy.

## From Python

Async is the native API; `SyncPgDebug` mirrors it exactly for code that is not.

```python
import asyncio
from pg_debug import AccessMode, PgDebug

async def main():
    async with PgDebug("postgresql://localhost/mydb", access_mode=AccessMode.RESTRICTED) as pg:
        plan = await pg.explain_query(
            "SELECT * FROM orders WHERE user_id = $1",
            hypothetical_indexes=[{"table": "orders", "columns": ["user_id"]}],
        )
        print(plan.plan_tree.total_cost)   # parsed nodes
        print(plan.to_text())              # readable summary

        for name, check in (await pg.analyze_db_health("index")).items():
            if not check.healthy:
                print(name, check.findings)

asyncio.run(main())
```

```python
from pg_debug import SyncPgDebug

with SyncPgDebug("postgresql://localhost/mydb") as pg:
    rows = pg.execute_sql("SELECT count(*) FROM orders")
```

The client connects lazily; `await pg.connect()` first if you want a bad URL to
fail immediately. Close it (or use the context manager) so the pool is
released.

Failures raise rather than returning error strings. Catch `PgDebugError`, or
the specific subclass — `ConnectionFailedError`, `InvalidArgumentError`,
`ExtensionRequiredError` (has `.extension`), `ExplainPlanError`,
`MissingDependencyError`. The one exception is index tuning, which reports
problems as an `error` key in its result dict.

Returns: `list[dict]` for schemas/objects/rows/top-queries/activity/vacuum
progress/column statistics, `dict` for `get_object_details` and the tuning
results, `ExplainPlanArtifact` for plans, and `dict[str, HealthCheckResult]`
for health.

The live-activity and statistics methods mirror the commands:
`list_activity(include_idle=, min_duration_seconds=, blocked_only=)`,
`vacuum_progress()`, `column_stats(table)`, `extended_statistics(table)`, and
`get_top_queries(..., interval_seconds=)` for sampling.

## The LLM tuning method

`--method llm` asks a language model for candidate index sets instead of
enumerating them. Every suggestion is still scored with HypoPG against the real
planner, so the model proposes and the planner decides.

```bash
pip install 'pg-debug[llm]'
export OPENAI_API_KEY=sk-...
pg-debug tune-queries --method llm "SELECT * FROM orders WHERE user_id = 42"
```

Reach for it only when `dta` finds nothing and you suspect a non-obvious index.
It handles **one query at a time**, makes several paid API calls per run, and
samples at a high temperature, so runs differ. Set the model with
`--llm-model`, `PG_DEBUG_LLM_MODEL`, or `llm_model=` on the client (default
`gpt-4o`). For everyday tuning `dta` is faster, free and
reproducible — do not reach for `llm` by default, and tell the user it costs
money before running it.

## Answering well

The output of these commands is evidence, not a conclusion. A useful answer
names the specific finding, the number behind it, and what to do — "the plan
switches from a Seq Scan costing 448 to an Index Scan costing 14.8, so
`orders(user_id, status)` is worth its 68 kB", not "you should add an index".

When a number is missing or an extension is not installed, say so plainly
instead of estimating around it.

# Slow queries, plans and indexes

How to read `diagnose`, `explain`, `index-use`, `column-stats` and the index
advisor, what each of their answers does and does not mean, and the planner
pitfalls none of them names yet.

## Diagnosing a slow query

Work outside in — plan first, then indexes, then confirm.

```bash
# 1. Ask what is wrong with the plan. Start here — it reads the plan for you.
pg-debug diagnose "SELECT * FROM orders WHERE status = 'open' AND region = 'eu'"

# 2. What is the planner doing?
pg-debug explain "SELECT * FROM orders WHERE user_id = 42 AND status = 'paid'"

# 3. What would it really do? (runs the query)
pg-debug explain --analyze "SELECT * FROM orders WHERE user_id = 42"

# 4. Would an index help? Nothing is created — HypoPG simulates it.
pg-debug explain "SELECT * FROM orders WHERE user_id = 42" -i 'orders(user_id)'

# Expressions work too, exactly as CREATE INDEX would take them.
pg-debug explain "SELECT * FROM users WHERE lower(email) = 'a@b.c'" -i 'users(lower(email))'

# 5. Or let the advisor search for candidates itself.
pg-debug tune-queries "SELECT * FROM orders WHERE user_id = 42 AND status = 'paid'"
```

`diagnose` is the one to reach for first when the question is "why is this
slow". It runs `EXPLAIN ANALYZE` and reads the result: the lowest node whose
row estimate diverged from what it found (everything above it inherited that
error), whether the cause is stale statistics or correlated columns with no
extended statistics covering them, sorts and hashes that outgrew `work_mem`,
scans reading rows only to discard them, and parallel workers that were planned
for and not granted. Findings are ranked by the share of the query's work
behind them, and each carries the statement or setting that addresses it.

Because it runs the query it needs `--access-mode unrestricted`, and it says so
rather than half-answering. A statement that writes is run in a transaction
that is rolled back — which undoes the rows but not a sequence it consumed.

When the user already has a plan — pasted into the conversation, from
`auto_explain`, from a replica — do not ask them to run the query again:

```bash
pg-debug diagnose --plan plan.json          # EXPLAIN (ANALYZE, FORMAT JSON) output
pg-debug diagnose --plan - < plan.json
pg-debug diagnose --plan --offline plan.json   # never connect, even with DATABASE_URI set
```

This runs nothing, so it works in restricted mode and without a database. With
a connection it also checks the tables for stale statistics and missing
extended statistics; without one a misestimate says those were not checked.
Only the JSON format is read: a plan pasted in the text format has to be
re-taken with `EXPLAIN (ANALYZE, FORMAT JSON)`, and say so. From Python,
`diagnose_plan(plan)` on a client does the connected version and the top-level
`pg_debug.diagnose_plan(plan)` the offline one, synchronously.

An empty result is a real answer: the estimates matched and nothing spilled.
Say that, rather than reaching for something else to blame.

`--analyze` executes the query, so do not use it for `UPDATE`/`DELETE` unless
you mean it. It also cannot be combined with `-i`.

When the question is "I have an index, why is it not used?", do not reason it
out from the plan by hand:

```bash
pg-debug index-use "SELECT * FROM orders WHERE lower(email) = 'a@b.c'"
pg-debug index-use -i orders_email_idx ./query.sql    # one index only
```

It reports every index on the query's tables as used (with the plan node) or
not, and for each unused one the reason, marked proven or candidate: the
query restricts a different column, or the column inside an expression the
index is not on; an operator a btree cannot search (`<>`, `NOT IN`,
`LIKE '%x'`, `ILIKE`, regex); `LIKE 'prefix%'` under a non-C collation without
`text_pattern_ops`; a cast the plan applies to the column (`org_id =
7::numeric`); a partial index whose `WHERE` the query does not match; an
invalid index. When nothing stops the planner it says so, with the cost of
the plan it chose against the plan with sequential scans disabled and the
share of the table the predicate is expected to match — above a few percent a
sequential scan is cheaper and no index on that column will be chosen. When
another index is used either way, it names that index. Each finding carries
the statement or rewrite that addresses it. It works in restricted mode too:
sequential scans are turned off for the forced plan's own transaction, which
writes nothing.

An index only helps a predicate it matches: to speed up `WHERE lower(email) =
...` the index has to be on `lower(email)`, not on `email`. PostgreSQL will
only accept an expression built from IMMUTABLE functions, so `date_trunc('day',
created_at)` on a `timestamptz` is rejected — that is the server's rule, not a
parsing failure.

Read the plan for the shape of the problem: a `Seq Scan` with a selective
`Filter` wants an index; a huge row-estimate error (`Plan Rows` far from
`Actual Rows`) usually means stale statistics — suggest `ANALYZE` before
suggesting indexes. `diagnose` does both of those readings for you and names
which one applies, so prefer it to eyeballing the plan.

Step 3 is the honest way to answer "would this index help?" — it reports the
planner's cost with the index in place, without touching the database. Compare
`Total Cost` before and after and say what changed rather than asserting an
index is a good idea.

## When the application runs as another role

pg-debug sees the database as the user in the connection URL. When that is the
tables' owner and the application connects as a role of its own, with session
variables its row-level security policies read, the answers differ: the owner
bypasses the policies, so its plan has no policy predicate and its counts
include every tenant's rows. Suspect it when the user mentions row-level
security, tenants, `SET ROLE` or `current_setting(...)`; when `diagnose` fails
with `unrecognized configuration parameter "app.something"`; or when a plan the
user pasted has a filter the one you got does not.

Run the query the way the application does:

```bash
pg-debug explain --role app_user --set app.tenant_id=7 "<query>"
pg-debug diagnose --role app_user --set app.tenant_id=7 "<query>"
pg-debug explain --role app_user --set app.tenant_id=7 -i 'orders(tenant_id, total)' "<query>"
```

A project can declare it once in a `pg-debug.toml`, which the commands that
run or plan a query read from the working directory up; look for one before
asking the user for the role:

```toml
[context]
role = "app_user"

[context.settings]
app.tenant_id = "7"
```

`--role` and `--set` override the file, and `--no-config` ignores it. Every run
that applies a context prints `pg-debug: query context: ...` on stderr. Relay
it with the answer: a plan is only true for the role and settings it was taken
under.

- The context covers only the statements that run or plan the query; the
  catalog is still read as the connecting user, who has to be a member of the
  role. A role or setting the server refuses fails before anything runs.
- Switching to a role does not apply its `ALTER ROLE ... SET` defaults, which
  only a login gets. If the application's role has any, add them with `--set`.
- The index advisor costs its candidates with the policy in the plan but takes
  candidate columns from the query's text, so it never proposes a column only
  the policy restricts. Test that one yourself with `-i`, as above.
- `statement_timeout`, `search_path` and the role cannot be `--set`; use
  `--timeout`, `--search-path` and `--role`.

`--set` also tries a setting before anyone changes the server. When `diagnose`
suggests a `work_mem`, run it again with `--set work_mem=...` and report
whether the spill went away.

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

## Planner pitfalls

Five causes of slow queries that none of `diagnose`'s rules names yet. An
empty result, or a "discarded rows" finding with the generic advice to add an
index, does not rule them out; when the symptom fits, check by hand.

### A prepared statement that slows down after a few runs

Applications prepare statements without asking for it: psycopg prepares one
once it has run five times on a connection. PostgreSQL plans a prepared
statement's first five executions with the values given (custom plans), then
builds a generic plan without them and compares its estimated cost with the
custom plans' average; from then on it may run the generic one. A generic plan
ignores the value, so it serves a skewed value badly and cannot prune
partitions when it is planned. The symptom is a query that is fast when run by
hand with a value and slow from the application.

A generic plan shows `$1` where a custom plan shows the value. On PostgreSQL
16 and later, `explain` plans a query written with `$1` as the generic plan,
unless it contains `LIKE`; with a value written in, it shows the custom plan
for that value. Compare the two:

```bash
pg-debug explain 'SELECT * FROM orders WHERE status = $1'
pg-debug explain "SELECT * FROM orders WHERE status = 'refunded'"
```

On older servers, and always in `diagnose`, pg-debug fills `$1` from the
column statistics instead, which gives a custom plan for a value it chose. On
a partitioned table the generic plan lists every partition, because it was
planned with no value at all; pg-debug cannot tell from it whether the
application's executions prune them, so do not read the list as the
partitions the application scans. On PostgreSQL 14 and later,
`pg_prepared_statements` counts `generic_plans` and `custom_plans` for each
statement; read it from the application's connection, since the view shows
only the statements of the session that reads it.

`plan_cache_mode = force_custom_plan` plans every execution with its own
values, paying the planning time each time. The setting is read when a cached
plan is executed, not when it is prepared, so set it where the statement runs:
for the role (`ALTER ROLE app_user SET plan_cache_mode = force_custom_plan`) or
in the transaction that runs it (`SET LOCAL`). Behind a transaction pooler use
`SET LOCAL` or `set_config('plan_cache_mode', 'force_custom_plan', true)`,
never `SET`, which stays on the server connection for the next client of the
same role and database. Trying it with `--set` shows nothing, because pg-debug
never executes a cached plan.

### JIT on a short query

JIT compiles a query when its estimated cost passes `jit_above_cost` (100,000
by default; inlining and optimization start at 500,000). Compiling takes time
up front: it pays off for long, CPU-bound queries and can cost a short one
more than it saves, and because it goes by the estimate, a query whose cost is
overestimated is compiled anyway. `diagnose` does not look at JIT, and the
text output of `explain --analyze` leaves it out. The JSON plan has it, and
has no `JIT` key when nothing was compiled:

```bash
pg-debug --json explain --analyze "<query>" | jq '{jit_ms: .JIT.Timing.Total, execution_ms: ."Execution Time"}'
```

When JIT is a large share of the execution time, raise `jit_above_cost` above
the plan's `Total Cost` for the role that runs the query, rather than turning
JIT off for the whole server. Measure the change before proposing it:

```bash
pg-debug --json explain --analyze --set jit_above_cost=<above the total cost> "<query>" | jq '{jit: .JIT, execution_ms: ."Execution Time"}'
```

`ALTER ROLE app_user SET jit_above_cost = ...` makes it stick, and `--role`
will not pick that up afterwards (see above), so keep passing `--set` when you
check it. Which queries in the workload are compiled shows in the `jit_*`
columns of `pg_stat_statements` (PostgreSQL 15 and later), which `top-queries`
does not report; read them with `pg-debug execute`.

### Row-level security that looks like a missing index

Two policy problems look like a missing index: `diagnose` reports rows the
scan discarded with the advice to add one, and `tune-queries` has nothing
useful to propose. Plan the query both as the connecting user (no `--role`),
which bypasses the policy when it owns the table or is a superuser, and as the
application's role (`--role`, `--set`, as above); when the scans differ, the
policy is the cause.

**A function the policy calls for every row.** A policy written
`user_id = auth.uid()` can evaluate `auth.uid()` for every row the scan reads;
written `user_id = (select auth.uid())`, the call runs once, as an InitPlan,
which pg-debug's plan shows as a one-row `Result` node under the scan, with a
parameter in the scan's filter where the call was. Measured
on a table of 100,000 rows of which the role sees 1,000, the per-row policy
took 83–104 ms and the rewritten one 7 ms; Supabase documents the same rewrite
taking a query from 179 ms to 9 ms. The same holds for `auth.jwt()`, and
Supabase's own advisor flags it as `auth_rls_initplan`. Index the column the
policy filters on as well, and test that index with `-i`, because the advisor
never proposes a column only the policy restricts. On Supabase the plan has to
be taken as `authenticated`, since `postgres` bypasses RLS, and with that
role's 8-second timeout, which `--role` does not apply:

```bash
pg-debug --timeout 8 diagnose --role authenticated --set 'request.jwt.claims={"sub":"<user uuid>","role":"authenticated"}' "<query>"
```

**A condition that is not leakproof.** PostgreSQL enforces a policy before any
condition of the query that calls a function or operator not marked
`LEAKPROOF`. Such a condition cannot be pushed below the policy, so an index
on it goes unused: the plan taken without `--role` uses the index, and the
role's plan applies the condition as a `Filter` after the policy's. `index-use` run as the
role calls the index unused with an "unexplained" reason, and neither the
hypothetical index it suggests nor `tune-queries` does better, for the same
reason. Check the functions in the condition, and for an operator the function
behind it (`pg_operator.oprcode`):

```bash
pg-debug execute "SELECT proname, proleakproof FROM pg_proc WHERE proname = 'lower'"
```

The fix is a condition built from leakproof functions and operators, or
marking the function `LEAKPROOF` after reviewing it, which only a superuser
can do; on a platform that grants no superuser, such as RDS or Neon, only the
rewrite is open.

### Deep pages with OFFSET

`OFFSET` does not skip rows. The scan produces every row before the page and
the `Limit` throws them away, so the node under the `Limit` reports `OFFSET`
plus `LIMIT` actual rows, and each page costs more than the one before.
`diagnose` does not name the `OFFSET`. On a deep page it usually finds
nothing; when the page ends early in the table, it reports the scan under the
`Limit` as a row overestimate instead. That finding is the `Limit` stopping
the scan early, as any `LIMIT` does, not a bad estimate, and `ANALYZE` will
not change it. Measure a deep page with its numbers written in: wherever
pg-debug fills in parameters, `OFFSET $1` becomes `OFFSET 0` and `LIMIT $1`
becomes `LIMIT 100`, which measures the first page and draws that same
overestimate finding.

The fix is keyset pagination: keep the last row's sort key and ask for the
rows after it, with an index on that key, for example
`WHERE id < $1 ORDER BY id DESC FETCH FIRST 10 ROWS ONLY`. Each page then
reads only its own rows.

### One cheap query called very often (N+1)

Code that loads a list and then runs one query per item shows in
`pg_stat_statements` as one template with a very high call count, a low mean
time and a large share of the total time, often shaped
`WHERE <foreign key> = $1`. Look for it in the live workload:

```bash
pg-debug top-queries --interval 10 --sort-by total_time
```

No call count proves it: it is an inference from counters and query text, so
confirm it in the application's code, a query inside a loop, before reporting
it as N+1. An index makes each call cheaper but keeps every round trip; the
fix is in the application, fetching the set in one query, a join or one query
over all the keys. `top-queries` reads `pg_stat_statements` for every database
on the server, and on some platforms the platform's own queries land there
too, so make sure the template is the application's.

## Recommending indexes

```bash
pg-debug tune-queries "SELECT ..." "SELECT ..."   # up to 10 queries
pg-debug tune-workload --max-index-size-mb 500    # from pg_stat_statements
```

Each recommendation carries an estimated size and the planner cost before and
after, individually and cumulatively. Report those numbers rather than just the
DDL — the improvement multiple is what justifies the write cost of a new index.
The size is what HypoPG measures for the candidate index, and it is also what
`--max-index-size-mb` budgets against, so a small budget really does drop
recommendations.

Two things routinely surprise people:

- **Requires HypoPG.** Without it, tuning fails with a message saying how to
  get it on this platform, or that the platform does not offer it
  (`ExtensionRequiredError` from Python). `top-queries` and `tune-workload` additionally need
  `pg_stat_statements`. No superuser is needed — pg_debug uses HypoPG's
  functions, which any role can execute, not its views, which are private to
  the role that created the extension.
- **Unqualified names resolve through the `search_path`.** Schema-qualified
  names (`reporting.orders`) work directly. For unqualified queries against
  another schema, pass `--search-path reporting,public`.

"No index recommendations found" is a real answer, not a failure — it usually
means a suitable index already exists or the planner does not think one helps.
Check `pg-debug describe <table>` before assuming something went wrong.

### When tuning reports fewer queries than expected

The workload comes from `pg_stat_statements` and not every entry can be used.
Entries that cannot be parsed or planned are skipped and the run says how many
and why — relay that rather than presenting the result as a complete picture.
The usual cause is privilege: a role that is neither a superuser nor a member
of `pg_read_all_stats` sees `<insufficient privilege>` instead of other roles'
query text, so the workload it analyzes is only its own queries. `GRANT
pg_read_all_stats TO <role>` fixes it. Queries against views are skipped by
design: an index goes on the tables underneath.

Each recommendation also carries a `write_cost` when the table has recorded
writes: how many inserts, deletes and non-HOT updates every index on it has to
be maintained for. Report it alongside the benefit rather than after it — an
index that halves a query and is maintained on ten million writes a day is a
different proposition from the same index on a table nobody writes to. A
`hot_update_warning` means most updates to that table currently avoid the
indexes entirely, and an index over a column they change would end that.

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

# Slow queries, plans and indexes

How to read `diagnose`, `explain`, `index-use`, `column-stats` and the index
advisor, and what each of their answers does and does not mean.

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
the statement or rewrite that addresses it. It works in restricted mode, where
the forced-plan step is skipped and the verdict says so.

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

- **Requires HypoPG.** Without it, tuning fails with a message explaining how
  to install it (`ExtensionRequiredError` from Python). `top-queries` and `tune-workload` additionally need
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

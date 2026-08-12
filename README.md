# pg_debug

PostgreSQL inspection, health checks and index tuning — as an async Python library with a CLI.

`pg_debug` is an extraction of the analysis engine behind
[crystaldba/postgres-mcp](https://github.com/crystaldba/postgres-mcp). The database
work is the same; the MCP server layer has been replaced by a plain Python API and a
`pg-debug` command, so the tooling can be used from scripts, tests, CI and the shell
without an MCP client in the loop.

## What it does

- **Schema introspection** — list schemas, tables, views, sequences and extensions; describe an object's columns, constraints and indexes.
- **Explain plans** — plan or `EXPLAIN ANALYZE` a query, including plans against *hypothetical* indexes that are never created (via [HypoPG](https://github.com/HypoPG/hypopg)).
- **Live activity** — what every backend is doing right now, what it is waiting on, and which process is blocking it.
- **Vacuum progress** — running vacuums, their phase and how far through the heap they are.
- **Planner statistics** — the per-column numbers behind a plan's row estimates, the extended statistics defined on a table, and which tables the planner is reasoning about with stale numbers.
- **Health checks** — invalid/duplicate/bloated/unused indexes, connection utilization, transaction ID wraparound risk, sequences near their limit, replication lag and slots, buffer cache hit rates, invalid constraints, statistics freshness.
- **Top queries** — the most expensive queries recorded by `pg_stat_statements`, ranked by resource blend or by execution time, cumulatively or sampled over an interval.
- **Index tuning** — recommend indexes for your recorded workload or for a specific set of queries, with estimated size and before/after planner costs.
- **Access modes** — run unrestricted, or in a `restricted` mode that parses each statement, rejects anything that is not read-only, and runs it in a read-only transaction with a statement timeout.

## Install

```bash
pip install pg-debug            # library + CLI
pip install 'pg-debug[llm]'     # adds the optional LLM index tuning method
```

Requires Python 3.12+ and PostgreSQL 12+.

Some features need server-side extensions: index tuning and hypothetical-index plans
need `hypopg`, and workload analysis and `top-queries` need `pg_stat_statements`. The
library tells you which one is missing and how to install it.

No superuser is required. Everything runs through HypoPG's functions, which are
executable by any role; the extension's convenience *views* are not readable by
default, and pg_debug does not use them.

## CLI

```bash
export DATABASE_URI=postgresql://user:pass@localhost:5432/mydb   # or pass -d/--database-url

pg-debug schemas
pg-debug objects public --type table
pg-debug describe public.orders

pg-debug execute "SELECT count(*) FROM orders"
pg-debug explain "SELECT * FROM orders WHERE user_id = 42"
pg-debug explain "SELECT * FROM orders WHERE user_id = 42" -i 'orders(user_id)'
pg-debug explain --analyze "SELECT * FROM orders LIMIT 10"

pg-debug activity                       # what is running right now
pg-debug activity --blocked              # only backends waiting on a lock
pg-debug activity --min-duration 30      # queries running longer than 30s
pg-debug vacuum-progress

pg-debug health
pg-debug health --type index,vacuum
pg-debug column-stats orders             # why the planner estimates what it does

pg-debug top-queries --sort-by mean_time --limit 5
pg-debug top-queries --interval 10       # measure the next 10 seconds instead

pg-debug tune-queries "SELECT * FROM orders WHERE user_id = 42"
pg-debug tune-workload --max-index-size-mb 500
```

### Global options

These go before the command (`pg-debug --json health`, not `pg-debug health --json`):

| Option | Meaning |
| --- | --- |
| `-d, --database-url URL` | Connection URL. Defaults to `$DATABASE_URI`, then `$DATABASE_URL`. |
| `--access-mode {unrestricted,restricted}` | `unrestricted` (default) runs any statement; `restricted` allows read-only statements only. |
| `--timeout SECONDS` | Statement timeout. Defaults to none in unrestricted mode and 30 in restricted mode. |
| `--search-path SCHEMAS` | Schema search path for the connection, e.g. `reporting,public`. |
| `--llm-model MODEL` | Model for the `llm` tuning method. Defaults to `$PG_DEBUG_LLM_MODEL`, then `gpt-4o`. |
| `--json` | Emit raw JSON instead of formatted text. |
| `-v, --verbose` | Log at INFO, or DEBUG when repeated. |
| `--version` | Print the version and exit. |

### Commands

| Command | Arguments and options |
| --- | --- |
| `schemas` | — |
| `objects SCHEMA` | `-t, --type {table,view,sequence,extension}` (default `table`) |
| `describe NAME` | `-s, --schema SCHEMA` (default `public`, ignored if `NAME` is qualified), `-t, --type {table,view,sequence,extension}` |
| `execute SQL` | — |
| `explain SQL` | `--analyze`, `-i, --hypothetical-index SPEC` (repeatable) |
| `activity` | `-a, --all` (include idle), `-m, --min-duration SECONDS`, `-b, --blocked` |
| `vacuum-progress` | — |
| `column-stats TABLE` | — |
| `health` | `-t, --type CHECKS` (comma-separated, default `all`), `--exit-code` |
| `top-queries` | `-s, --sort-by {resources,mean_time,total_time}` (default `resources`), `-n, --limit N` (default 10, ignored for cumulative `resources`), `-i, --interval SECONDS` |
| `tune-workload` | `--max-index-size-mb MB` (default 10000), `--method {dta,llm}` (default `dta`) |
| `tune-queries QUERY...` | up to 10 queries, same options as `tune-workload` |

Health checks are `index`, `connection`, `vacuum`, `sequence`, `replication`,
`buffer`, `constraint`, `statistics` and `all`.

A hypothetical index `SPEC` is `table(column, ...)` with an optional method —
`orders(user_id)`, `orders(user_id, created_at):btree`,
`reporting.orders(status):hash`.

SQL arguments accept `-` to read the statement from stdin:

```bash
pg-debug --json explain - < slow_query.sql | jq '.Plan."Total Cost"'
```

`health` can gate a CI job on the result:

```bash
pg-debug health --type index,vacuum --exit-code
```

Exit status is `0` on success, `1` on error, `2` on a usage error, and `3` for
`health --exit-code` when a check reports a problem.

`python -m pg_debug` works the same as the `pg-debug` command.

## Library

Everything is async and returns plain Python data structures.

```python
import asyncio
from pg_debug import AccessMode, PgDebug

async def main():
    async with PgDebug("postgresql://localhost/mydb", access_mode=AccessMode.RESTRICTED) as pg:
        for schema in await pg.list_schemas():
            print(schema["schema_name"], schema["schema_type"])

        plan = await pg.explain_query(
            "SELECT * FROM orders WHERE user_id = $1",
            hypothetical_indexes=[{"table": "orders", "columns": ["user_id"]}],
        )
        print(plan.to_text())          # readable summary
        print(plan.plan_tree.total_cost)  # parsed plan nodes

        for name, check in (await pg.analyze_db_health("index,vacuum")).items():
            print(f"{name}: {check.summary}")
            for finding in check.findings:      # the objects it flagged
                print("   ", finding)

        result = await pg.analyze_query_indexes(["SELECT * FROM orders WHERE user_id = 42"])
        for rec in result["recommendations"]:
            print(rec["index_definition"], rec["index_estimated_size"])

asyncio.run(main())
```

The client connects lazily on first use; `await pg.connect()` up front fails fast on a
bad URL instead.

### Return values

| Method | Returns |
| --- | --- |
| `list_schemas()` | `list[dict]` — one per schema |
| `list_objects(schema, object_type="table")` | `list[dict]` — one per object |
| `get_object_details(schema, name, object_type="table")` | `dict` with `basic`, `columns`, `constraints`, `indexes` |
| `execute_sql(sql)` | `list[dict]` — one per row, empty for statements with no result set |
| `explain_query(sql, analyze=False, hypothetical_indexes=None)` | `ExplainPlanArtifact` |
| `list_activity(include_idle=False, min_duration_seconds=None, blocked_only=False)` | `list[dict]` — one per backend, with `blocked_by` |
| `vacuum_progress()` | `list[dict]` — one per running vacuum |
| `column_stats(table)` / `extended_statistics(table)` | `list[dict]` — planner statistics |
| `analyze_db_health(health_type="all")` | `dict[str, HealthCheckResult]` — check name to result |
| `get_top_queries(sort_by="resources", limit=10, interval_seconds=None)` | `list[dict]` — one per query |
| `analyze_workload_indexes(...)` / `analyze_query_indexes(queries, ...)` | `dict` with `summary`, `recommendations`, `query_impact` |

Each `HealthCheckResult` carries `healthy`, a text `summary`, the flagged
`findings` (one dict per object) and the `metrics` behind the verdict, and has
`to_dict()` for serialization.

Failures raise instead of returning error strings. All exceptions derive from
`PgDebugError`: `ConnectionFailedError`, `InvalidArgumentError`,
`ExtensionRequiredError` (carries the missing `extension` name),
`ExplainPlanError`, `QueryError`, `MissingDependencyError`.

### Without async

`SyncPgDebug` mirrors the same API, blocking instead of awaiting. It runs the
client on its own event loop, so close it (or use it as a context manager) when
you are done:

```python
from pg_debug import SyncPgDebug

with SyncPgDebug("postgresql://localhost/mydb") as pg:
    for row in pg.execute_sql("SELECT count(*) FROM orders"):
        print(row)
```

### Using the tools directly

If you already manage your own connection, the underlying tools take a `SqlDriver` and
can be used without `PgDebug`:

```python
from pg_debug import DatabaseHealthTool, SchemaInspector, SqlDriver, TopQueriesCalc
from pg_debug.explain import ExplainPlanTool
from pg_debug.index import DatabaseTuningAdvisor, TextPresentation

driver = SqlDriver(engine_url="postgresql://localhost/mydb")
checks = await DatabaseHealthTool(driver).run_checks("buffer")
```

## The LLM tuning method

Both tune commands take `--method`. `dta` (the default) is a deterministic
search: it enumerates index combinations from the query's predicates and scores
each one against the planner. `llm` instead asks a language model for candidate
index sets, then scores those the same way — every suggestion is still
validated with HypoPG against the real planner, so the model proposes, it does
not decide.

It is worth trying when `dta` finds nothing and you suspect a non-obvious index
(an unusual column order, or a column the predicate only touches indirectly).
For everyday tuning, `dta` is faster, free and reproducible.

```bash
pip install 'pg-debug[llm]'
export OPENAI_API_KEY=sk-...

pg-debug tune-queries --method llm "SELECT * FROM orders WHERE user_id = 42 AND status = 'paid'"
```

What to expect:

- **One query at a time.** The optimizer reasons about a single plan, so
  `tune-queries --method llm` takes exactly one query and rejects more.
  `tune-workload --method llm` only works if the recorded workload narrows to a
  single query — otherwise use `dta` for workloads.
- **Several API calls per run**, at your own cost. It keeps proposing until
  five consecutive rounds fail to improve the objective.
- **HypoPG is still required** — that is what scores each proposal.
- **Non-deterministic.** It samples at a high temperature by design, so two
  runs on the same query can differ. Treat the output as candidates to review,
  not a verdict.
- **The model is configurable** three ways, most specific first: the
  `--llm-model` flag (or `llm_model=` on the client and per call),
  `PG_DEBUG_LLM_MODEL`, then the `gpt-4o` default. The client reads
  `OPENAI_API_KEY` and honours `OPENAI_BASE_URL`, so an OpenAI-compatible
  endpoint works if it supports structured outputs.

The same choice is available from the library:

```python
pg = PgDebug(url, llm_model="o3-mini")                       # for every call
result = await pg.analyze_query_indexes([query], method="llm")
result = await pg.analyze_query_indexes([query], method="llm", llm_model="gpt-4o")  # just this one
```

### Relationship to pganalyze

The live-activity, vacuum-progress, planner-statistics and interval-sampling
features were prompted by reading the
[pganalyze collector](https://github.com/pganalyze/collector), which is a good
map of which catalog views matter. No code was copied — the queries here were
written against the PostgreSQL catalogs directly. pg_debug's SQL parsing also
runs on pganalyze's
[libpg_query](https://github.com/pganalyze/libpg_query), via `pglast`.

### Known limitations

- Index tuning resolves schema-qualified table names directly. Unqualified
  names resolve through the connection's `search_path`, which `--search-path` /
  the `search_path` option sets.
- `analyze_query_indexes` (and `tune-queries`) accepts at most
  `MAX_NUM_INDEX_TUNING_QUERIES` (10) queries. `analyze_workload_indexes`
  (`tune-workload`) selects from `pg_stat_statements` instead: up to 100
  queries with at least 50 calls and a mean execution time of 5 ms or more.

### Environment variables

| Variable | Effect |
| --- | --- |
| `DATABASE_URI`, `DATABASE_URL` | Default connection URL. |
| `PG_DEBUG_INCLUDE_TRACE` | Set to `true` to include the index advisor's internal trace under `_trace` in tuning results. |
| `PG_DEBUG_LLM_MODEL` | Model for the `llm` tuning method (default `gpt-4o`). |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | Credentials and endpoint for the `llm` tuning method. |

## Using it from Claude Code

`.claude/skills/pg-debug/SKILL.md` is a project skill that teaches Claude Code
how to drive this tool — which command answers which question, how to read the
output, and the traps (extensions, `search_path`, access modes). It loads
automatically when you ask about slow queries, indexes or database health in a
session started in this repository.

## Development

```bash
uv sync --all-extras          # install into .venv
uv run ruff check .
uv run pytest tests/unit      # unit tests, no database needed
uv run pytest tests/integration   # spins up PostgreSQL containers, needs Docker
```

Some tests under `tests/unit` also start containers (`test_explain_plan_real_db.py`,
`test_db_conn_pool.py`); see `tests/README.md`.

## Relationship to postgres-mcp

The database logic — safe SQL parsing, the explain tooling, health calculators and the
Database Tuning Advisor — comes from
[crystaldba/postgres-mcp](https://github.com/crystaldba/postgres-mcp) (MIT, Copyright (c)
2025 Crystal Corp.) and is redistributed here under the same license. Changes made
during extraction:

- The MCP server, its tool registrations and transports are gone; `PgDebug` and the
  `pg-debug` command take their place.
- Tools return structured data instead of MCP text content, and raise exceptions
  instead of returning `"Error: ..."` strings.
- `TopQueriesCalc` returns rows rather than a stringified list; health checks return
  `HealthCheckResult` objects through `run_checks()` (`health()` still renders the
  same text report).
- The LLM tuning method's dependencies moved to the optional `llm` extra.
- `POSTGRES_MCP_INCLUDE_LANGFUSE_TRACE` became `PG_DEBUG_INCLUDE_TRACE`, and now
  defaults to off.
- The index advisor handles schema-qualified tables, and table size estimation
  no longer falls back to a fixed default for every table. See
  [CHANGELOG.md](CHANGELOG.md) for the full list.

## License

MIT. See [LICENSE](LICENSE).

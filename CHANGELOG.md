# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `pg-debug activity` and `PgDebug.list_activity()`: what every backend is
  doing, what it is waiting on, and which pids are blocking it. Idle backends
  and the client's own connection are excluded by default; `--blocked` and
  `--min-duration` narrow it further.
- `pg-debug vacuum-progress` and `PgDebug.vacuum_progress()`, reporting phase
  and percent scanned, version-gated for the PostgreSQL 17 counter changes.
- `pg-debug column-stats` and `PgDebug.column_stats()` /
  `extended_statistics()`: the per-column numbers behind the planner's row
  estimates, and the extended statistics objects on a table.
- A `statistics` health check flagging tables never analyzed or whose contents
  have drifted since the last ANALYZE.
- `top-queries --interval N` and `get_top_queries(interval_seconds=)`, which
  sample pg_stat_statements over an interval instead of reporting totals since
  the last reset. Handles resets and newly appearing queries, and excludes
  pg_debug's own queries and transaction control from the result.
- A `pg-debug` project skill (`.claude/skills/pg-debug/SKILL.md`) documenting
  how to drive the tool from Claude Code.
- `PG_DEBUG_LLM_MODEL`, which overrides the model used by the `llm` tuning
  method (previously hardcoded to `gpt-4o`).
- A complete CLI command reference and an "LLM tuning method" section in the
  README.
- A `table` health check estimating how much of each table's heap holds no live
  rows, from `pg_stats` and `pg_class` — no extension required. It builds a row
  the way PostgreSQL does, walking the columns in `attnum` order and padding
  each one forward to its type's alignment boundary, and it honours the table's
  `fillfactor`, so a table with nothing wrong estimates exactly. Summing column
  widths instead — as most catalog bloat queries do — understates the row width
  and invents bloat on healthy tables: 11 bytes a row, a phantom 7.5%, on a
  six-column table used in testing. A table is reported only when it wastes at
  least 100 MB *and* at least 20% of its size, so ordinary free space in a large
  table is not flagged.
- `pg-debug model` and `PgDebug.review_data_model()`, a review of the data
  model rather than of what the server is doing: foreign keys with no index
  behind them, keys whose two sides are different types, tables with no primary
  key, columns ordered so that alignment padding costs bytes on every row,
  `timestamp without time zone`, `character(n)`, key columns that are nullable
  but never null, and text columns holding a handful of distinct values. Needs
  no extension, and each rule is one catalog query.

  Every finding says how much of it is fact. A *proven* finding is read from
  the catalog and cannot be wrong; a *candidate* is inferred from sampled
  planner statistics and carries the query that settles it, because a column
  with no NULLs in the sample may still have one. Severity comes from the
  evidence rather than the rule — an unindexed foreign key on a large child
  whose parent has recorded deletes is not the same finding as one on a
  40-row lookup table — and findings are ordered by severity and then by each
  rule's own measure of impact, deliberately not by a single score across
  rules. `--exit-code` gates a CI job on high-severity findings only.
- `pg_debug.tuple_layout`, the row layout the table bloat check was built on,
  extracted so the column-order rule shares it. The two therefore cannot
  disagree: a table the review calls optimally packed is one the bloat check
  will not accuse of padding. On a table of fixed-width columns the review
  predicts the reordered page count exactly, which the integration tests
  verify by rebuilding the table and counting.
- `SqlDriver.session()`, an async context manager that pins one pooled
  connection so session state — hypothetical indexes, `SET`, temp tables,
  advisory locks — survives across calls. `SafeSqlDriver` proxies it, and index
  tuning now runs each analysis inside one.

### Fixed

- Workload analysis stopped at the first query it could not parse, reporting
  `syntax error at or near "<"` and abandoning the rest. That text comes from
  `pg_stat_statements` itself: a role that is not a superuser and lacks
  `pg_read_all_stats` sees `<insufficient privilege>` in place of other roles'
  queries. Entries that cannot be parsed are now skipped, and the run says how
  many were dropped and why — including the grant that makes them visible.
- Nor could the advisor be trusted to survive a query it could not *plan*.
  `pg_stat_statements` records queries with their constants stripped out, and
  the values put back in their place are guesses; one of the wrong type — an
  integer against an enum or a domain — raised and ended the analysis. Such
  queries are now dropped in the same pass.
- A workload that was dropped entirely reported "No index recommendations
  found.", which reads as "your indexes are fine" rather than "none of your
  queries could be used". It now explains itself.
- `tune-queries` and `tune-workload` raised `hypopg: "x" is not a table or
  materialized view` for any query against a view — an ordinary thing for a
  workload to contain, and fatal to the whole run. Candidates are now limited
  to relations an index can actually be created on, which also stops the
  advisor proposing indexes on catalog tables. The advisor no longer analyzes
  pg_debug's own queries either.
- A statement that failed inside `SqlDriver.session()` left the pinned
  connection's transaction aborted, so every later statement in that session
  failed with "current transaction is aborted". Outside a session the pool
  resets a returned connection; pinned, it does not, so one recoverable error
  took the whole session with it. The session now clears the aborted
  transaction and carries on — hypothetical indexes live in backend memory and
  survive it.
- The index advisor measured every candidate with `hypopg_relation_size()` and
  then threw the measurement away, rebuilding each recommendation without it.
  Everything downstream fell back to a formula over `pg_stats` that read
  `n_distinct` as a count when PostgreSQL reports it as a negative fraction of
  the row count, so every high-cardinality column — the best index candidates —
  was priced at about 26 bytes. That number drove the reported size, the
  `--max-index-size-mb` budget and the space term of the search's objective
  function, so the budget never bound and the search was biased toward exactly
  the indexes whose size it underestimated. Measured sizes are now the source
  of truth, and the fallback formula sizes an index by the table's row count.
- `IndexRecommendation.__eq__` read an attribute that does not exist, so
  comparing two recommendations raised `AttributeError` instead of comparing
  them.
- The "long text column" warning on index recommendations fired for every
  `text` column, however short its values, which made it meaningless. It now
  applies only where the values are actually long enough to matter. Its text
  also cited the wrong limit: a btree entry has to fit in about 2704 bytes, not
  8191.
- The CLI rejected expression indexes: `-i 'users(lower(email))'` and anything
  with a comma inside a function call or quoted string failed to parse. The
  spec parser is now parenthesis- and quote-aware, so index columns accept the
  same expressions `CREATE INDEX` does.
- Generated index names could be invalid identifiers. An expression such as
  `date_trunc('day', created_at)` produced a name containing quotes, which the
  server rejected outright; names are now reduced to identifier characters and
  kept within PostgreSQL's 63-byte limit, with a digest suffix so two long
  expressions cannot collapse onto the same truncated name.
- Index candidate sizes were measured in a separate statement from the
  hypothetical index creation. Hypothetical indexes are session-scoped and the
  connection pool does not promise the next statement the same connection, so
  the measurement could see no indexes and silently record every candidate as
  costing nothing. Creation and measurement now share one round trip.
- Index tuning failed with "permission denied for view hypopg_list_indexes"
  for any role that did not create the HypoPG extension. HypoPG grants no
  privileges on its views and a view is private to its owner, so the size
  lookup now reads the `hypopg()` function it wraps, which defaults to EXECUTE
  for PUBLIC and exposes the same columns.

### Changed

- The `llm` tuning method's model can be set with `--llm-model`, the
  `llm_model` client argument, or per call, in addition to
  `PG_DEBUG_LLM_MODEL`.
- `analyze_query_indexes(..., method="llm")` rejects more than one query up
  front, instead of failing deep in the analysis, since the LLM optimizer
  reasons about a single plan.

## [0.1.0] - 2026-08-12

First release. `pg_debug` extracts the analysis engine of
[crystaldba/postgres-mcp](https://github.com/crystaldba/postgres-mcp) into an
async Python library with a `pg-debug` CLI, replacing the MCP server layer.

### Added

- `PgDebug`, an async client that owns a connection pool and exposes schema
  introspection, query execution, explain plans, health checks, top queries and
  index tuning. It connects lazily and can take a caller-supplied pool.
- `SyncPgDebug`, the same API without `await`, backed by a dedicated event loop
  so the pool stays usable across calls.
- `SchemaInspector`, holding the catalog queries that used to live in the MCP
  tool handlers.
- A `pg-debug` command with `schemas`, `objects`, `describe`, `execute`,
  `explain`, `health`, `top-queries`, `tune-workload` and `tune-queries`;
  `--access-mode`, `--json`, `--search-path`, `--timeout`, stdin input via `-`,
  and hypothetical index specs such as `orders(user_id, created_at):btree`.
- `python -m pg_debug` as an alternative entry point.
- `health --exit-code`, which exits 3 when any check reports a problem.
- `--search-path` / the `search_path` client option, applied to every pooled
  connection. Index tuning resolves table names through it.
- A `py.typed` marker, so installed wheels export their type information.
- An optional `llm` extra carrying the LLM tuning method's dependencies.

### Changed

- Tools return structured data instead of MCP text content, and raise
  exceptions (`PgDebugError` and its subclasses) instead of returning
  `"Error: ..."` strings.
- Health checks return `HealthCheckResult` objects — a `healthy` flag, a text
  `summary`, the flagged `findings` and the `metrics` behind the verdict —
  through `DatabaseHealthTool.run_checks()` and `PgDebug.analyze_db_health()`.
  `DatabaseHealthTool.health()` still renders the same text report.
- `TopQueriesCalc` returns rows rather than a stringified list.
- `--timeout` applies in both access modes as a server-side
  `statement_timeout`. It defaults to none in unrestricted mode and to 30
  seconds in restricted mode, so an unrestricted query is no longer unbounded
  when a timeout is asked for.
- `POSTGRES_MCP_INCLUDE_LANGFUSE_TRACE` became `PG_DEBUG_INCLUDE_TRACE` and now
  defaults to off. The trace key is `_trace`.
- Generated index names and the query tag are prefixed `pg_debug` rather than
  `crystaldba`.

### Fixed

- The index advisor resolves schema-qualified table names instead of dropping
  the qualifier and relying on the `search_path`.
- A schema-qualified table produced an invalid generated index name
  (`pg_debug_idx_reporting.orders_...`), which made hypothetical-index explain
  plans fail outright.
- Table size estimation interpolated the table name as a string literal
  (`FROM 'orders'`) and quoted qualified names as a single identifier, so both
  lookups failed and every table fell back to a 10MB default.

### Removed

- The MCP server, its tool registrations and its stdio/SSE/streamable-HTTP
  transports.

[Unreleased]: https://github.com/danplischke/pg_debug/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/danplischke/pg_debug/releases/tag/v0.1.0

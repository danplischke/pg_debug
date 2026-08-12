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

### Changed

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

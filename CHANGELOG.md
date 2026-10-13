# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Python 3.14 is supported, and the package metadata says so; CI runs the
  suite on 3.12, 3.13 and 3.14, and on macOS and Windows, where the
  container-backed tests skip and the rest run.

- `--baseline FILE` and `--save-baseline FILE` on `health`, `model` and
  `config`. Their `--exit-code` makes them build gates, and the trouble with
  a gate on an existing database is the first run: it fails on everything,
  deliberate findings included, and gets turned off. A baseline is the
  findings a team has read and accepted; against one, the gate fails only on
  what is new. It records what each finding is about — the check and the
  object, the rule and the table, the setting — never the numbers, so a
  bloated index is the same finding at 31% and 34%, and a finding that goes
  away is simply not listed. The file is JSON, one finding per line in a
  stable order, for the repository. Text output ends with what the baseline
  changed; JSON output marks each finding `known` or `new`.

- A `scan` health check: tables read sequentially far more than through an
  index, from `pg_stat_user_tables` and no extension. Index tuning finds the
  same tables, but only with `pg_stat_statements` and `hypopg` installed; the
  counters are always there. The check argues in bytes, not scan counts: each
  table is charged the data its sequential scans read, scaled down when a
  scan returned only part of the table, and reported only when that reading
  is large, the table is not small, and sequential scans outnumber index
  scans. Findings carry the counters, the rows each scan returned against the
  table's rows, when the counting started, and on PostgreSQL 16 and later the
  time of the last sequential scan.

- `pg-debug index-use` and `PgDebug.explain_index_use()`: why the planner
  did not use an index for a query. The question has two halves and they need
  different evidence. Whether the planner *can* use the index is read from
  the query's text against the index's definition and the catalog: the
  column each predicate restricts, bare or wrapped in a function or cast the
  index is not on; an operator a btree cannot search (`<>`, `NOT IN`, a
  leading-wildcard `LIKE`, `ILIKE`, a regular expression); `LIKE 'prefix%'`
  under a non-C collation without a pattern operator class; a cast the plan
  applies to the column; a partial index whose `WHERE` the query does not
  match; an invalid index; an OR that spans tables. Whether it *chose* to is
  read by planning the query again with sequential scans disabled: if the
  index is used then, nothing stopped it, and the finding reports both costs
  and the share of the table the predicate is expected to match. When another
  index serves the query either way, that index is named rather than the
  verdict left unexplained. Each finding is marked proven or candidate and
  carries the statement or rewrite that addresses it. Nothing is executed;
  in `restricted` mode the forced plan is skipped and the verdict says so.

- `pg-debug diagnose --plan` and `PgDebug.diagnose_plan()`: diagnose a plan
  that already exists. `diagnose` had to run the query itself with
  `EXPLAIN ANALYZE`, in unrestricted mode, which put its best reading behind
  its heaviest requirement — most people with a slow query hold a plan, not
  production access. `--plan` takes `EXPLAIN (ANALYZE, FORMAT JSON)` output
  from a file, stdin or inline, including the object `auto_explain` logs, and
  runs nothing, so it works in `restricted` mode and, with no database URL,
  with no connection at all. Connected, the tables the plan reads are checked
  for stale statistics and missing extended statistics as before; unconnected,
  a misestimate says those were not checked instead of staying silent.
  `pg_debug.diagnose_plan(plan)` does the offline reading from Python without
  a client or an event loop. The unit tests gain a corpus of real PostgreSQL
  16 plans diagnosed this way.

- `pg-debug locks` and `PgDebug.lock_tree()`: who is blocking whom, as a tree.
  `activity` already reported the pids blocking each waiter, which left the
  reader to follow them by hand when the database was stuck. This walks the
  blocking graph from every waiter back to the backend that waits on nothing
  and prints that root first — usually a session idle in a transaction, with
  the last statement it ran — and nests everything stuck behind it beneath,
  each line saying what it waits for (the lock mode and table, or the other
  transaction and the table of the row) and what the holder has on it. Roots
  are ordered by how many backends they block. It works in `restricted` mode
  and needs no extension.

### Fixed

- `SyncPgDebug` could not connect on Windows. Its event loop was Windows'
  default, the proactor loop, on which psycopg refuses to open an async
  connection, so the first call that reached the database failed with
  `InterfaceError`. The CLI has always worked around this; the sync client
  never did. It now runs on a selector loop. The CLI chooses its loop for each
  run instead of changing the process-wide event loop policy, an API Python
  deprecated in 3.14 and removes in 3.16.

- SQL and plans given as a file or on standard input were decoded in the
  platform's code page. That is UTF-8 on Linux and macOS, but cp1252 or similar
  on Windows, where a migration or query with anything outside ASCII in it
  arrived garbled, or stopped with a codec error. A UTF-8 file that starts
  with a byte order mark, as Notepad writes them, failed everywhere: the mark
  reached the parser as `syntax error at or near "ALTER"`. Input is now read
  as UTF-8 on every platform, with or without the mark, and input that is not
  UTF-8 is refused with the name of the file it came from rather than read
  wrongly.

- On Windows, `pg-debug explain` stopped with `'charmap' codec can't encode
  character '\u2192'` whenever its output went to a file or a pipe, and so did
  index tuning output with an `∞` in it. Windows encodes redirected output in
  the ANSI code page, cp1252 or similar, which has neither character. Output
  and errors are now written as UTF-8 on Windows, as Python itself will by
  default from 3.15. Output to a console was never affected.

- `pg-debug diagnose ./slow.sql` ran the literal text `./slow.sql` as the
  query. The README had promised a file path since the command appeared;
  it now reads one, as `check-migration` does.

## [0.2.0] - 2026-10-06

First published release. 0.1.0 below was never tagged or uploaded to PyPI;
its entry is kept because it describes what the package started as.

### Fixed

- The buffer health checks no longer report a cache hit rate taken over too few
  block accesses to mean anything. A server that has just started, or one whose
  statistics were just reset, reads every page once and reported "0.0% (below
  95.0% threshold)" — a false alarm pointing at `shared_buffers` on a server
  with nothing wrong with it, and one that contradicted `pg-debug config`,
  which already applied a floor. Both now use the same constant, and the
  verdict carries the block counts it was taken over.
- Numeric columns rendered raw in the CLI's tables: the fractions in
  `top-queries` came back as `Decimal` and printed at full precision
  (`0.09286047596826878208`), or in scientific notation when a division came
  out zero (`0E-20`). They are rounded like floats now, while a whole numeric
  such as `wal_bytes` keeps its whole number.

### Added

- Python 3.13 is supported; CI runs the suite on 3.12 and 3.13.
- Published wheels declare `License-Expression: MIT` and ship `NOTICE`, which
  records the derivation from postgres-mcp, alongside `LICENSE`. Package
  metadata now carries the project URLs.
- `pg-debug diagnose` and `PgDebug.diagnose_query()`: run a query and report
  what is wrong with the plan it produced, rather than only printing it. Nine
  rules over the parsed plan — the lowest node whose row estimate diverged from
  what it found, whether that is stale statistics or correlated columns with no
  `pg_statistic_ext` object covering them, a nested loop re-executing its inner
  side far more often than planned for, a scan reading rows only to discard
  them, sorts and hashes and bitmaps that outgrew `work_mem`, and parallel
  workers planned for but not granted. Findings carry the statement or setting
  that addresses them, and are ranked by the share of the query's work behind
  them — a bad estimate charged for the whole subtree it misled rather than for
  the scan itself. Needs `EXPLAIN ANALYZE`, so `restricted` mode refuses with
  the reason; a statement that writes is run in a transaction that is rolled
  back.
- `SqlDriver.execute_query_and_rollback()`, which runs a statement and keeps
  only what it returned. `SafeSqlDriver` overrides it to refuse.
- `PlanNode` now parses the fields the diagnosis reads: rows removed by a
  filter, sort method and spill size, hash batches, lossy heap blocks, workers
  planned and launched, index and join conditions, and the schema a relation is
  in.
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
- Index tuning now weighs what an index costs to maintain. Every insert,
  delete and non-HOT update on a table has to maintain each index on it, and
  those counters are already kept — HOT updates are subtracted, since one
  changes no indexed column and updates no index at all. Each recommendation
  reports the writes it will be maintained for, and warns where a table's
  updates are mostly HOT today, because an index over the wrong column gives
  that up.

  The figure also feeds the search's objective, at a weight chosen by testing
  rather than assumed: the term moves the objective by log(2) when the first
  index is added to a table, which at weight 1.0 outweighs a 20% improvement in
  query time and vetoed indexes plainly worth having. At the 0.1 default it
  breaks ties towards the quieter table without overruling a real improvement.
  `write_weight=0` restores the previous behaviour.
- `pg-debug partitions` and `PgDebug.suggest_partitions()`, which say whether a
  table is worth partitioning and on what key, decided by the share of recorded
  query time that restricts on that key rather than by the table's size. It is
  built to say no: every table examined is reported with its verdict, and the
  refusals carry the share they were refused on, because a large table where
  most of the query time filters on nothing in particular is made slower by
  partitioning and that is the more useful answer more often.

  Predicates are read from the parsed statement, so a column a query merely
  selects does not count as one it prunes on, and the operator decides the
  strategy: HASH prunes on equality alone, so a column queried by range is
  never given one. Deletes are weighed separately, since a delete that restricts
  on the key becomes a DROP rather than a scan and the vacuum after it.

  For a recommended table it emits the target shape — not a migration.
  Converting a populated table needs a copy, a constraint reconciliation and a
  name swap, which is not something to generate unseen. The sketch does run:
  where the primary key would have to gain the partition key it is recreated
  rather than copied, because `LIKE ... INCLUDING ALL` brings the old key along
  and PostgreSQL rejects the CREATE. Monthly boundaries come from the column's
  histogram, so they cover the span the data occupies.
- `pg-debug config` and `PgDebug.review_configuration()`, which review the
  server's settings against what it is actually doing rather than against a
  table of recommended values. `work_mem` is raised only where queries have
  spilled to temporary files, `shared_buffers` questioned only where the cache
  is genuinely missing, `max_wal_size` only where checkpoints are being forced
  rather than scheduled — so a default doing no harm produces no finding, and
  every finding carries the counter it rests on.

  It does not guess at hardware. PostgreSQL cannot see how much memory the
  machine has or whether its storage is solid state, so where the right value
  depends on one of those the finding says so and offers no number.
- Two `vacuum` health checks beyond wraparound. "Autovacuum backlog" reports
  tables autovacuum is not keeping clean and separates the three reasons that
  look alike: past its threshold and waiting, autovacuum switched off for it,
  or a table large enough that the default scale factor makes it wait for a
  fifth of itself to die. "Vacuum horizon" reports what is stopping VACUUM
  reclaiming anything at all — a transaction holding a snapshot or a
  transaction id, a replication slot nobody consumes, a prepared transaction
  left behind — which is what explains bloat that keeps coming back.

  The horizon check is narrower than the usual advice to hunt "idle in
  transaction". Measured on PostgreSQL 16: a READ COMMITTED transaction that
  has only read holds neither a snapshot nor a transaction id between
  statements and blocks no cleanup, so it is not reported. One that has written
  holds a transaction id, and one at REPEATABLE READ or above holds a snapshot;
  those are reported, and the finding says which.
- `pg-debug check-migration` and `PgDebug.check_migration()`, which review a
  migration before it runs: per statement, the lock it takes, how long it holds
  it, whether it rewrites the table, and a safer rewrite where one exists. The
  lock level alone settles little — ACCESS EXCLUSIVE held for a catalog update
  lasts microseconds, the same lock held for a rewrite is an outage — so the
  duration is reported beside it, and a connection is used to say how big the
  affected tables actually are.

  The lock and rewrite behaviour was measured against PostgreSQL 16, not taken
  from documentation: each statement run in a transaction, `pg_locks` read for
  the modes held, `relfilenode` compared before and after. Several results
  contradict common advice, and the ones that do are pinned by tests —
  `ADD COLUMN ... NOT NULL DEFAULT 0` has not rewritten the table since
  PostgreSQL 11, and `ADD FOREIGN KEY` takes SHARE ROW EXCLUSIVE rather than
  ACCESS EXCLUSIVE, so it never blocks readers. Whether a column default forces
  a rewrite is resolved through `pg_proc`, since only volatility decides it.

  `--offline` reviews without a connection, for CI where the reachable database
  is not the one the migration will run against and its table sizes would
  soften every verdict.
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

- The version is written in one place, `pg_debug.__version__`;
  `pyproject.toml` reads it from there at build time.
- Every statement pg_debug issues now carries the `/* pg_debug */` marker, in
  both access modes. Only `SafeSqlDriver` applied it before, so in the default
  unrestricted mode the tool's own catalog reads were indistinguishable from
  the workload — and `top-queries` duly reported them. The marker moves to
  `SqlDriver`, where everything passes through, and `SafeSqlDriver` no longer
  adds a second one.
- `top-queries` excludes marked statements and transaction control in the
  statement rather than after it, so pg_debug's own queries can neither take up
  slots in a `LIMIT 10` nor inflate the denominator each resource fraction is
  measured against. Note that statements run *through* pg_debug — `pg-debug
  execute`, `explain` — are marked too, and so no longer appear in
  `top-queries`.
- The `llm` tuning method's model can be set with `--llm-model`, the
  `llm_model` client argument, or per call, in addition to
  `PG_DEBUG_LLM_MODEL`.
- `analyze_query_indexes(..., method="llm")` rejects more than one query up
  front, instead of failing deep in the analysis, since the LLM optimizer
  reasons about a single plan.
- The package is reorganised so that where a module lives says what it does.
  The top-level `pg_debug` exports and the CLI are unchanged; code that imports
  submodules needs the new paths:
  - `pg_debug.sql` is split into `pg_debug.db` (the driver, restricted mode and
    its allowlist, extension checks, identifiers) and `pg_debug.parsing` (the
    pglast visitors and `SqlBindParams`).
  - `pg_debug.explain`, `pg_debug.plan` and `pg_debug.artifacts` are merged into
    `pg_debug.plans`, which also holds `IndexDefinition`.
  - `pg_debug.database_health` is now `pg_debug.health`, with one module per
    check under `pg_debug.health.checks`.
  - `pg_debug.index.index_opt_base`, `dta_calc` and `llm_opt` are now
    `pg_debug.index.base`, `dta` and `llm`; `pg_debug.top_queries` is a single
    module; `pg_debug.tuple_layout` is `pg_debug.queries.tuple_layout`; and
    `pg_debug.formatting` is `pg_debug.cli.render`.

## [0.1.0] - 2026-08-12 (unpublished)

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

[Unreleased]: https://github.com/danplischke/pg_debug/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/danplischke/pg_debug/compare/332df27...v0.2.0
[0.1.0]: https://github.com/danplischke/pg_debug/releases/tag/v0.1.0

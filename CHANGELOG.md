# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The Claude Code skill covers what it was silent on: transaction poolers and
  managed platforms, read replicas and replication slots, sharding, and
  partitions once they exist. A new `platforms.md` says which endpoint to
  use, how to tell a build that works through a pooler from one that does
  not, which role to connect as, how long the counters behind a finding have
  been counting, which settings Neon, Supabase, RDS, Aurora, Cloud SQL,
  AlloyDB and Azure fix or re-tune, where pg-debug still misreads them (a
  cache hit ratio with a second cache tier behind it, the platform's own
  queries in the workload), and when the evidence lives outside SQL. A new
  `scale-out.md` covers replicas, slots and logical replication, and answers
  "should we shard?" from what the server shows, starting from "not yet". `schema.md` adds pruning, lock slots and the DDL that maintains
  partitions; `incidents.md` adds the DDL lock queue, multixact member
  exhaustion, collation changes after an OS upgrade and reading waits from
  one snapshot; `slow-queries.md` adds the planner pitfalls no rule names yet:
  generic plans, JIT on short queries, row-level security that looks like a
  missing index, deep `OFFSET` pages and N+1 queries. The claims about
  PostgreSQL were checked against a PostgreSQL 16 server; the claims about
  platforms come from their documentation and have not been tried on them.

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
  carries the statement or rewrite that addresses it. Nothing is executed.
  Sequential scans are turned off for the forced plan's own transaction, not
  with a `SET` and a `RESET` around it, so the verdict holds behind a
  transaction pooler, which could send the three to three backends, and
  `restricted` mode takes the forced plan too.

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

- A Claude Code plugin, published from this repository as its own
  marketplace: `/plugin install pg-debug --marketplace danplischke/pg_debug`.
  The skill that teaches Claude to drive pg-debug was a project skill, so it
  existed only in sessions started inside a clone of this repository; the
  plugin takes it to any project. It uses the `pg-debug` on the PATH, or runs
  it with `uvx` from this repository without installing anything. The skill
  also costs less: its description, which every session carries, is half its
  old length, and the old one ran past the 1,536 characters Claude Code shows,
  so its end was cut off; and the detail moved from `SKILL.md` into reference
  files Claude reads when a question needs them, so a use of the skill loads
  about 1,900 tokens instead of 10,000. The plugin's version is the package's.
  In this repository the skill now lives in
  `plugins/pg-debug/skills/pg-debug/`, and `.claude/skills/pg-debug` links to
  it.

- A query context: the role and settings to run a query with, so that
  pg-debug sees the database the way the application does. pg-debug connects
  as whoever the URL names, often the tables' owner, and the owner bypasses
  row-level security: a query on a table with a policy saw every tenant's
  rows and planned without the policy's predicate, a `Seq Scan` where the
  application gets an index scan, and `diagnose` of a query whose policy reads
  a session variable failed with `unrecognized configuration parameter`.
  `execute`, `explain`, `diagnose`, `index-use`, `tune-queries` and
  `tune-workload` take `--role ROLE` and `--set NAME=VALUE`, or read a
  `[context]` table from a `pg-debug.toml` in the working directory or the
  nearest parent that has one (`--config FILE` names another, `--no-config`
  reads none), and say on stderr which context they ran in. It is applied
  inside each statement's own transaction with `set_config(..., true)`, so it
  never outlives the statement on a pooled connection, and only to the
  statements that run or plan the query: pg-debug's catalog reads stay with
  the connecting user, from whom row-level security hides no statistics. The
  server checks it before anything runs; it works in `restricted` mode; and it
  refuses the settings pg-debug sets itself (`statement_timeout`,
  `search_path`, the role, the read-only settings), naming the option that
  does. `--set` also tries a setting without changing the server:
  `diagnose --set work_mem=64MB` reruns a query with the `work_mem` a spill
  finding suggested. In the library it is `PgDebug(context=QueryContext(...))`,
  and `QueryContext.from_file()` reads a `pg-debug.toml`.

- `PgDebug.statistics_access()`, `withheld_backends()` and
  `sample_top_queries()`, with their `SyncPgDebug` twins. A role without
  `pg_read_all_stats`, which `pg_monitor` includes, is shown other roles'
  backends and statements with their details withheld, and nothing said which
  rows those were or why. `statistics_access()` says whether the role is shown
  everything, whether it is a member of `pg_read_all_stats` that does not
  inherit it, and the statement that would show it everything.
  `withheld_backends()` gives what PostgreSQL still shows of each withheld
  backend: whether it is in a transaction, the locks it holds, and the
  backends waiting on it. `sample_top_queries()` returns the ranking
  `get_top_queries(interval_seconds=...)` returns, with how many other roles'
  statements ran and could not be ranked. The functions behind the first two
  are in `pg_debug.db.privileges`. `list_activity()` rows and `lock_tree()`
  nodes carry `in_transaction`.

- `pg.counting_windows()`, `SyncPgDebug.counting_windows()` and
  `health --min-window-days` (`analyze_db_health(min_window_days=)`). Every
  finding argued from a counter is argued from what happened since the
  counter last started from zero, and nothing said when that was. The
  method returns, for this database's table and index counters, the
  checkpoint counters and `pg_stat_statements`, when they started counting,
  for how long, and whether a reset or a restart that discarded them started
  them; for `pg_stat_statements` 1.9 and later also how often it evicted
  entries since. The window is the later of the database's
  `stats_reset` and `pg_stat_bgwriter.stats_reset`, which a crash, an
  immediate shutdown, a failover or a restore moves; not the server's uptime,
  since a clean restart keeps the counters.

- `pg-debug shard-readiness` (`pg.assess_shard_readiness()`, returning a
  `ShardReadiness`). "Should we shard?" was answered by reading `config`,
  `health --type vacuum` and a hand-summed `top-queries --interval` and
  weighing them yourself, with no WAL rate, no working-set figure and no
  routing key across tables. The command samples the WAL written
  (`pg_stat_wal`, or the WAL position before PostgreSQL 14), shared buffers
  evicted (`pg_stat_io`, 16) and checkpoints over `--interval` (10 seconds by
  default, reported with the figures), reads the autovacuum backlog and this
  database's `pg_stat_statements`, and answers "not yet" with each reason
  and its figure: reads taking 80% of the recorded time (replicas), the top
  five statements taking half (tune them), a table `partitions` would
  partition by time (partition), a write path with no pressure (size alone),
  no column compared by equality in half the time (no routing key). Only
  with none standing does it say "worth evaluating", never "shard". On a
  Citus coordinator it reads shard size skew, the top tenant's CPU share,
  calls with no distribution key, timestamp and low-cardinality distribution
  columns, and keys without the distribution column. The Citus checks follow
  Citus's documentation and have not been run against a Citus cluster.
  `pg_current_wal_lsn()` is now allowed in restricted mode.

- `pg-debug history` (`pg.read_history()`, returning a `HistoryReport`).
  The history a platform keeps beyond `pg_stat_statements` was a table of
  views in the skill to read by hand with `execute`. The command reads Azure's
  Query Store (from the `azure_sys` database), Aurora's
  `aurora_stat_statements()` and `aurora_stat_plans()`, the Cloud SQL and
  AlloyDB index advisor's recommendations, Neon's `neon_stat_file_cache`
  beside the buffer cache hit ratio that misreads it, and `pg_qualstats` and
  `pg_wait_sampling` where installed. Each source says how far back it goes
  and, when capture is off, which setting says so; whether it is there is
  read from the catalog first. The platform readers follow each platform's
  documentation and have not been run on the platforms; `pg_qualstats` and
  `pg_wait_sampling` were not available to test against either.
  `aurora_stat_statements` and `aurora_stat_plans` are now allowed in
  restricted mode.
- `activity --sample SECONDS` (`--sample-interval`, default 0.5) and
  `PgDebug.sample_waits(seconds, interval=)`, with its `SyncPgDebug` twin.
  `activity` showed each backend's wait at one instant, and the only way to
  learn where time went over a minute was `pg_wait_sampling`, which most
  servers and managed platforms do not have. The sampler reads
  `pg_stat_activity` once per sample, one statement each, and reports
  average active sessions overall, for client backends, per wait event and
  per query (by `query_id` where the server reports one, by text with
  constants replaced where it does not, saying why). It states the samples
  taken and the interval achieved, measured on the server's clock, marks
  every attribution a candidate (each sample is exact; the profile is a
  sample), leaves out pg-debug's own backend and statements, and says how
  many backends' state was withheld from a role without `pg_read_all_stats`.
  Where `pg_wait_sampling` is installed its profile over the same window is
  read too; where Azure's Query Store records waits, its last interval.

- `activity` and `locks` end their text with what the waits in that moment
  amount to, and `PgDebug.classify_waits()` returns it: a lock queue behind a
  statement waiting for ACCESS EXCLUSIVE (or another lock that blocks
  writes), an idle-in-transaction session blocking others, row lock
  contention, and LWLock pressure on `BufferMapping` or `LockManager`. They
  showed each backend's wait and who blocks whom, and the pattern — the
  queued queries are victims of the DDL, which is the victim of the idle
  transaction — was left to the reader. Each comes from one statement with
  the pids, lock modes and relation it rests on, and a confidence: proven
  when `pg_locks` shows it, a candidate when inferred, with the judgment in
  it said. `--json` keeps printing the rows.

- `check-migration` reads the `lock_timeout` and `statement_timeout` each
  statement would run with — a `SET` earlier in the migration, the query
  context, the `ALTER ROLE ... SET` of the role the context names, or the
  default — and takes `--role`, `--set`, `--config` and `--no-config` for
  them. It advised a short `lock_timeout` in passing and could not tell
  whether one was set: a `SET lock_timeout` in the file changed nothing in its
  report. Now a statement that waits for ACCESS EXCLUSIVE, SHARE ROW EXCLUSIVE
  or SHARE on an existing table with no `lock_timeout` carries
  `lock_timeout_advice`, saying why: while it waits for its lock, every later
  statement that conflicts with that lock queues behind it.
- `health` reads four things that fill a disk, stop writes or corrupt
  indexes, which it was silent on. `replication` adds "Replication slot
  retention": the WAL each slot retains, its `wal_status`, and a slot that is
  inactive with `max_slot_wal_keep_size = -1`, which retains until the disk
  fills, or holds an `xmin` or `catalog_xmin` past
  `autovacuum_freeze_max_age`. `vacuum` adds "Multixact wraparound": each
  database's and this database's tables' multixact age against 200 million,
  `autovacuum_multixact_freeze_max_age` and the hard limit; the transaction ID
  check never read multixacts, and member space, for which PostgreSQL
  publishes no percentage, is measured on disk where the role may and
  reported as a candidate. A new `collation` check compares recorded
  collation versions with the library's and names the indexes on a changed
  one, with the `REINDEX` and `REFRESH VERSION` that fix it. A new `checksum`
  check reports `pg_stat_database.checksum_failures`, and says when data
  checksums are off rather than calling the server clean. `mxid_age`,
  `pg_current_wal_lsn`, `pg_wal_lsn_diff`, `pg_collation_actual_version` and
  `pg_database_collation_actual_version` are allowed in restricted mode,
  since each only reads, so all of these run there.
- `pg-debug health --type partition` checks the partitioned tables that
  already exist. Before, `partitions` only advised whether to partition, and
  said nothing about a table that was; the skill pointed at `explain` and
  hand-written catalog queries. It now reports a time-range table whose last
  partition ends within one partition's width (or has ended), with the
  statements that add the next one; rows in a DEFAULT partition, which every
  later ATTACH scans under ACCESS EXCLUSIVE; unique indexes created on
  partitions and on no index of the parent, so unique per partition only;
  `enable_partition_pruning = off` in the configuration, for a role or
  database, or in the query context; trees a query can lock more of than the
  fast path holds (16 slots before PostgreSQL 18, `max_locks_per_transaction`
  rounded up to a power of two from 18), estimated from what
  `pg_stat_statements` records filtering on the partition key and stated as
  the worst case otherwise; and more than 1,000 partitions or three levels.
  The lock estimate and the per-partition unique index are candidates.

- `check-migration` reviews `ATTACH PARTITION`, `DETACH PARTITION` and
  `CREATE TABLE ... PARTITION OF`, which it used to pass over in silence.
  Measured on PostgreSQL 16: `PARTITION OF` takes ACCESS EXCLUSIVE on the
  parent; ATTACH takes SHARE UPDATE EXCLUSIVE on it and scans the attached
  table and a DEFAULT partition holding rows under ACCESS EXCLUSIVE, unless a
  validated CHECK proves each; DETACH takes ACCESS EXCLUSIVE unless
  CONCURRENTLY. Connected, a DEFAULT partition's size decides the severity.
  The safer form is the attach recipe, which the review itself reports at low
  severity throughout, and which `partitions` now appends to its range sketch.

- `diagnose` reports `unpruned_partitions`: an Append or MergeAppend that
  scanned four or more partitions of one table, each filtered on the
  partition key, none removed at plan or run time, most of them for no rows.
  That is a condition pruning cannot read (the key in a function or cast,
  another type, pruning off); it is a candidate and says how to confirm it.
- `diagnose` names the causes it used to leave to the reader. A row-level
  security policy written `USING (user_id = auth.uid())` evaluates the
  function for every row the scan tests; `diagnose` reported those rows as
  discarded and advised an index, and now reports `rls_per_row_call` with the
  `ALTER POLICY` that wraps the call as `(SELECT auth.uid())`, which
  PostgreSQL evaluates once — on a 200,000-row table, 179 ms became 16 ms —
  and holds the index advice back until it is applied. It reads the policy
  from `pg_policy` and the function's volatility from `pg_proc`, so it needs a
  connection, and it needs the query run as the role the policy applies to
  (`--role`). Also new, each argued from the plan node it rests on and, where
  the catalog is needed, from the catalog: a condition under a policy kept from
  an existing index because it is not `LEAKPROOF`; an index walked in order for
  a `LIMIT` while a filter discarded most of it, with the composite index; a
  function over a plainly indexed column, with the expression index when the
  function is IMMUTABLE; an OR across a join's two sides; a deep `OFFSET`; a
  CTE computed in full and read once through a filter; an index-only scan
  going to the heap; a sequential scan reading near-empty pages; a generic
  plan's misestimate in `auto_explain` output; and triggers, JIT compilation
  or planning taking a large share of the time, with the missing index for a
  foreign-key trigger. Findings inferred from a condition's text carry
  `confidence: candidate`. Read efficiency of index scans and a generic plan
  read from `EXPLAIN (GENERIC_PLAN)` are not reported: neither can be argued
  from the plan alone.

- A `diagnose` finding about a choice the planner made — a misestimate, a
  generic plan, an index walked for a `LIMIT`, a nested loop that blew up —
  carries `plan_control`: the ways this server offers to hold a plan in place
  while the cause is fixed. `pg_hint_plan` when it is installed, loaded or
  available; PostgreSQL 19's `pg_plan_advice`, which was in beta as of October
  2026; Aurora Query Plan Management on Aurora; AlloyDB's own plan management
  on AlloyDB, which pg-debug does not detect; and, when there is none, that
  there is none. It is advice: pg-debug applies none of it.
- `pg-debug upgrade-check [--to VERSION]` and `pg-debug security`, with
  `PgDebug.check_upgrade()` and `audit_security()`. Before, what stands in
  the way of a major upgrade came out of `pg_upgrade --check`, run with the
  new binaries installed beside the old cluster, and nothing in pg-debug read
  `pg_hba.conf`, roles or row-level security. `upgrade-check` reads, from a
  connection, what `pg_upgrade` 18 refuses (`reg*` columns, also inside a
  domain, array, composite or range; prepared transactions; a
  `data_checksums` setting that differs from the target's initdb default,
  which 18 turned on), what it carries across incomplete (logical slots and
  subscriptions from a server before 17; extended statistics, which 18 does
  not preserve), extensions behind their default version, and MD5
  passwords. `security` reads `pg_hba_file_rules` (`trust`, cleartext
  `password`, `md5`, rules open to every address), `ssl`,
  `password_encryption`, schemas where PUBLIC may CREATE, superuser and
  BYPASSRLS login roles, tables with row-level security and no policy,
  tables without it beside tables that have it (a candidate), and the minor
  version and end of support against releases known on a stated date, which
  ages. Each report lists what every check examined, and a check that could
  not read what it needs (`pg_authid` and `pg_hba_file_rules` are a
  superuser's) says so instead of passing. Both are read-only, run in
  restricted mode, and take `--exit-code` (high severity),
  `--baseline` and `--save-baseline`, knowing a finding by its check and
  subject. They are commands rather than health checks because neither is
  about how the server is running: one depends on the target version, and
  both answer to a different reader and schedule.

- `model` has three more rules. `primary_key_overflow` reads the sequence
  behind a `smallint` or `integer` primary key, serial or identity (found
  through `pg_depend`, since an identity column has no `nextval` default),
  and reports it from half its values used, high from 80%; a `bigint` key
  whose serial sequence is still `AS integer` is judged by the sequence, and
  the remediation widens the referencing integer columns too. `json_column`
  flags `json` where `jsonb` would do, as a candidate with a query that
  counts rows with a duplicate top-level key, the one thing the conversion
  changes. `mostly_null_index` flags a plain btree whose leading column
  `pg_stats` puts at least half NULL, as a candidate for a partial index
  `WHERE column IS NOT NULL`, which serves every condition that cannot be
  true of a NULL.
- `pg-debug server-rates [--interval SECONDS]`, `pg.sample_server_rates()` and
  `SyncPgDebug.sample_server_rates()`. Only `top-queries --interval` measured
  anything over an interval, and only statements; what the server as a whole
  was doing now (commits, block reads, temp files, deadlocks, WAL, checkpoints,
  I/O by process type, how fast a replication slot holds back WAL) could be
  read only as totals since a reset that may be a month or a minute old. The
  command takes two snapshots of `pg_stat_database` for this database, the
  WAL position, `pg_stat_wal` (14+), the checkpointer and background writer
  (`pg_stat_checkpointer` on 17+), `pg_stat_io` (16+) and
  `pg_replication_slots`, and reports each counter's delta and rate per second
  over the interval measured between that view's own reads. A view whose
  recorded reset moved during the interval, or a counter that went
  backwards, is refused with the two times or values, rather than reported as
  a negative or too-small rate; a view the version lacks says which version
  brought it. Restricted mode allows `pg_current_wal_lsn()` and
  `pg_wal_lsn_diff()` for it.

- `pg-debug pooler ADMIN_URL`, `pg.inspect_pooler()` and
  `SyncPgDebug.inspect_pooler()`. Behind PgBouncer, a client waiting for a
  server connection is not a backend, so nothing pg-debug read could show a
  pool that is too small, and the skill could only ask the user for `SHOW
  POOLS` output. The command reads PgBouncer's admin console (`SHOW POOLS`,
  `SHOW STATS`, `SHOW DATABASES`, `SHOW CONFIG`) and reports clients waiting
  and the oldest wait, the average wait over PgBouncer's stats period, and,
  given `-d`, pools that can open more server connections than the server's
  `max_connections` leaves for ordinary roles. The console speaks only the
  simple query protocol and has no transactions, so it is read on its own
  connection rather than through the driver, and is sent nothing but fixed
  `SHOW` commands.

- `top-queries --interval` marks each statement's `change`: `new` when it was
  not in pg_stat_statements before the sample, `slower` or `faster` when its
  mean time in the sample is three times, or a third of, its mean before it.
  The ranking said what was large, which may always have been; it did not
  say what had changed. Both are candidates, and are judged only when
  pg_stat_statements has counted ten times the sample's length before it (and
  from pg_stat_statements 1.11, the statement itself has); otherwise stderr
  says why nothing was judged. `QuerySample` gains `statement_window` and
  `changes_refused`.
- Rule ids for every kind of finding. A build that wanted to act on one kind
  of finding had to match a health check's display name ("Invalid index
  check") or a configuration finding's setting, neither of which is a name
  for a kind of finding. Now each kind that `health`, `model`, `config` and
  `logs` report has a stable, lower-case id unique across the commands
  (`duplicate_index`, `unindexed_foreign_key`, `work_mem_spills`,
  `deadlock`), and in JSON each finding carries its `rule` and a `docs` link
  to its entry in a new rules reference, `references/rules.md`, which also
  says what the rule reads: the catalog, the tables' data, the workload's
  counters, the settings or a log. A health check's JSON lists its `rules`;
  `ConfigFinding` gained `rule`.

- Suppressing a rule for the gate: `--suppress RULE` on `health`, `model` and
  `config`, or a `[suppress]` table in `pg-debug.toml` mapping each rule to
  the reason. A baseline accepts findings one object at a time, so a team
  that had decided against a whole rule (every `timestamp` column is UTC by
  convention) had to accept each finding and every new one again. A
  suppressed rule's findings are still reported, marked `"suppressed"` with
  the reason in JSON and counted after the text, and never fail
  `--exit-code`; the run says on stderr what it suppresses and from where. An
  id that is not a rule, or a suppression without a reason, is an error.

- `--static` on `health` and `model`: only the rules that read the catalog
  alone (invalid and duplicate indexes, invalid constraints; the key, type
  and column rules of the data model review). Run in CI against a freshly
  migrated, empty database, the other rules found nothing for want of data
  or counters and passed, which is not a pass. A selection with no static
  rule in it is refused, naming why.

- `pg-debug plans --queries FILE` and `PgDebug.plan_shapes()`: each query's
  plan shape, read with `EXPLAIN` and nothing run, under the query context:
  node types, join, aggregate and scan strategies, and the relations and
  indexes each node reads, without costs or row counts. With `--baseline` and
  `--exit-code` a build fails (status 3) when a query's plan changes shape,
  and the text shows the accepted and the current shape as a diff, so a
  migration that drops the index a critical query uses fails in CI rather
  than in production. A `-- name:` line names a query; otherwise it is known
  by its fingerprint.

- `pg-debug logs FILE...` and `pg_debug.analyze_logs()`: read PostgreSQL
  server logs with no connection, as stderr (with `--prefix` the server's
  `log_line_prefix`, or detected), csvlog or jsonlog, plain or gzipped, and
  report deadlocks with the processes, queries and relations in them, lock
  waits and how each ended, temporary files per statement, vacuums skipped
  for a lock, autovacuum and autoanalyze runs per table, and too-frequent
  checkpoints. These leave only counters in the statistics views, or
  nothing; the log has who, when and which statement. Each finding names the
  `file:line` it came from, and a kind of event that is absent is reported
  with the setting it depends on (`log_lock_waits`, `log_temp_files`,
  `log_autovacuum_min_duration`), since a quiet log is not a healthy server.
- Index tuning checks its own answer before giving it, and says what it
  refused. The search optimises the workload's total cost, so an index that
  made one query dearer could ride on another's improvement, two indexes
  where one would serve both were both recommended, and an index the greedy
  search chose early could stay in the set after a later one took over its
  queries, costing writes for nothing. Now every query is re-costed against
  the final set and an index that makes any query cost more than 1.1 times
  its baseline is refused (`--max-regression-factor`,
  `max_regression_factor=`; Azure's default, and a cost bound, not a time
  bound); indexes sharing a leading column are merged where one serves every
  query within that bound; and an index no plan reads is refused. Each
  recommendation lists the queries whose plan reads it (`queries_using`), the
  `llm` method lists the alternatives it did not choose with their costs, and
  the result carries `regression_check`, `consolidations`,
  `refused_candidates` and `workload`, which for `tune-workload` holds the
  window `pg_stat_statements` counted the sample in.

- `tune-queries` without HypoPG returns index candidates instead of only the
  hint. Where HypoPG can be neither used nor created (Cloud SQL, Lakebase and
  the other platforms that do not offer it, or a server without the package),
  the dta method used to stop with `ExtensionRequiredError`. It now infers
  candidates from `pg_stats` with PgHero's heuristic, grades each, marks it
  `candidate`, and says in the result that the planner costed none of them;
  what it examined and refused is listed with the figure. On Cloud SQL and
  AlloyDB it adds `google_db_advisor_recommend_indexes()` where that
  extension is installed, outside restricted mode. That call is untested:
  neither platform was available.

### Changed

- pg-debug pins pglast 7.20 instead of 7.11, so installing it no longer holds
  pglast back at the older release. It still parses with PostgreSQL 17's
  grammar, now from 17.7. psycopg-pool 3.3.3, pydantic 2.14 and
  instructor 1.17 are the versions the lockfile now tests with.

- A statement that failed was logged at ERROR with its full text, so a
  `health` run with six checks timing out behind a lock printed close to 300
  lines of pg-debug's own SQL to stderr, and restricted mode logged each
  failure a second time. Each failure is now one line, and the statement is
  logged at debug (`-vv`).

- `--timeout`, restricted mode's default timeout and `--search-path` are set
  inside each statement's transaction, with `set_config(..., true)`, instead
  of with `SET` on each pooled connection; see the fix below for why.
  `DbConnPool` no longer runs anything on the connections it opens, so code
  that borrowed one from its `pool` directly gets the server's defaults;
  `SqlDriver` applies the pool's `statement_timeout` and `search_path` to
  every statement it runs. A timeout PostgreSQL would refuse, below 0 or
  above 2147483 seconds, is refused when the client or the pool is built,
  where it used to fail every transaction. `SqlDriver.execute_query()` takes a
  `cleanup` statement, run after the query in the same transaction even when
  the query fails or is cancelled, for state a rollback does not undo, such as
  hypothetical indexes;
  `with_context()` on either driver gives one with another query context; and
  `session()` no longer claims that the statements it pins share a session,
  which behind a transaction pooler they need not.

- Index tuning raises instead of returning an `error` key. A failed
  `analyze_workload_indexes()` or `analyze_query_indexes()` returned
  `{"error": "..."}`, the one place the library did not raise, so
  `except PgDebugError` never saw it and a caller reading only
  `recommendations` found nothing to recommend. A missing HypoPG now raises
  `ExtensionRequiredError`; tables that were never analyzed, a workload none
  of which could be analyzed, and an analysis that fails part way raise the
  new `IndexTuningError`; any other library error, such as a `QueryError`,
  comes through unchanged. `DatabaseTuningAdvisor.analyze_workload()` raises
  the same way, and `IndexTuningResult` has no `error` field. A workload with
  nothing worth recommending still returns normally. The CLI reports these
  failures as it reports every other one, on standard error with exit status
  1, where it printed `Index tuning failed: ...`, or an `error` object under
  `--json`, on standard output.

- `ExplainPlanTool.explain()`, `explain_analyze()` and
  `explain_with_hypothetical_indexes()` raise `ExplainPlanError` instead of
  returning an `ErrorResult`, and `ErrorResult` is no longer exported from
  `pg_debug` or `pg_debug.plans`. A return value that was either a plan or an
  error had to be checked with `isinstance` after every call, and a caller who
  did not check met an `AttributeError` somewhere else. A malformed
  hypothetical index definition, one that is not a dict or lacks `table` or
  `columns`, is an `InvalidArgumentError`, from `PgDebug.explain_query()` as
  well, where it was an `ExplainPlanError`.

- pg-debug no longer depends on `attrs`. It was installed for two classes,
  `PlanNode` and `ExplainPlanArtifact`, which are now slotted dataclasses
  with the same fields, constructors and equality.

- pydantic is no longer installed with pg-debug. Only the `llm` tuning
  method uses it, so it moves to the `llm` extra, and `import pg_debug` no
  longer loads it. `LLMOptimizerTool` is still exported from `pg_debug` and
  `pg_debug.index`, and is imported when first asked for; without the extra,
  asking for it raises `MissingDependencyError` with the command that installs
  it. `DEFAULT_LLM_MODEL` and `llm_model()` are defined in
  `pg_debug.index.llm_settings`, and `pg_debug.index.llm` still has them.

- A `PgDebug` or `SyncPgDebug` client keeps no pooled connection while idle.
  It held one for as long as it lived, two after its first call, and an open
  connection keeps a scale-to-zero compute awake: Aurora Serverless v2 does
  not pause while a client holds one, so a long-running application that made
  one call kept its database running. The pool now closes a connection 30 to
  60 seconds after its last use, and all five it can grow to within three
  minutes, inside the five minutes that are Aurora's shortest pause delay; the
  next call connects again. The CLI, which closes its pool when each command
  ends, is unaffected. `DbConnPool` builds its pool with `min_size=0` and
  `max_idle=30` (`POOL_MAX_IDLE` in `pg_debug.db.driver`).

- A connection the pool cannot get raises `ConnectionFailedError`, not
  `QueryError`, and says why. Once the pool kept nothing while idle, a call
  after an idle spell had to connect, and a refused connection, from
  `max_connections` or the role's connection limit, waited out the pool's 30
  seconds and raised `QueryError: couldn't get a connection after 30.00 sec`,
  with the server's reason only in a log warning: just when a diagnosis is
  most wanted. A bad URL, port, database or role did the same on the first
  connection. Now a client that has never connected fails at once with the
  server's reason, and so does one that has, when the refusal will not clear
  by the next attempt (too many connections, a wrong password, a role or
  database that does not exist, no `pg_hba.conf` entry) and no connection can
  come back to the caller instead. A refusal that may clear, from a server
  restarting or failing over, is still waited for, and the wait still ends
  after 30 seconds, saying whether every connection was in use or the attempt
  had not finished. Each attempt to connect now gives up after 30 seconds
  unless the connection string or `PGCONNECT_TIMEOUT` sets a limit: libpq
  sets none, so an attempt to a host that drops packets went on for as long as
  the operating system kept trying, about two minutes on Linux, after the
  caller had stopped waiting. A caller that stops waiting itself, under a
  timeout of its own, no longer leaves the pool trying to connect, which kept
  its event loop from finishing.

- `diagnose` printed every finding; with more rules a plan can draw a dozen,
  and the one that matters scrolled away. The text output now shows the six
  costliest and says how many it left out; `--json` still lists all. A share
  of time no plan node includes — a trigger's, JIT's — is stated as a share of
  the execution time, and planning as a share of planning plus execution,
  rather than as a share of "the query's work".
- `activity` listed backends by how long they had been in their state, so the
  session holding everyone up could sit below the sessions waiting on it.
  Culprits now come first: root blockers (blocking others, waiting on
  nothing), the one blocking the most first, then blockers that wait
  themselves, then the waiters, then the rest. Each row carries
  `blocking_total`, how many backends wait on it directly or behind others,
  counted from one read of the lock graph across the whole server, and the
  table shows it as `blocking`.
- Baseline files are version 2: each entry can carry a `fingerprint` of the
  definition of the index, constraint or columns the finding names. A
  baseline accepted a finding by the object's name alone, so a duplicate
  index whose covering index was rebuilt over other columns, or a column
  whose type changed, stayed "known" though what was accepted no longer
  existed. Such a finding is now `changed`, marked so in JSON and listed after
  the text, and fails the gate as a new one would. Version 1 files still load,
  and their findings are known or new as before.

### Fixed

- The README documented `list_objects(schema, ...)` and
  `get_object_details(schema, name, ...)`; the parameters are `schema_name` and
  `object_name`, so a call with the documented keywords raised `TypeError`. Its
  feature list also named none of the commands added in this release.

- One health check that failed ended the whole `health` run. A lock on one
  table (a migration holding ACCESS EXCLUSIVE, say) timed out the first index
  check, and `health` printed only `canceling statement due to statement
  timeout` and exited 1, though most checks never read that table. Each check
  now runs on its own. One that fails is listed in its place as `Could not
  run:` with the server's reason, the others report as usual, stderr names
  the checks that could not run, and the exit status is 1, because a run that
  did not look everywhere cannot pass. With `--exit-code`, a problem found by
  a check that ran still exits 3: it stands whatever the missing checks would
  have said. In JSON and in `HealthCheckResult` such a check has `healthy:
  false` and an `error`, and `analyze_db_health()` returns it rather than
  raising. A check that could not run is not a finding: against a baseline it
  is neither known nor new, and the line after the report says how many
  checks were not compared instead of "nothing new". `--save-baseline` writes
  nothing from such a run, names the checks and exits 1: a baseline without
  them would fail as new on whatever they find once they run, and written
  over an accepted one it would throw away what the team had accepted. When
  the database cannot be reached at all, the checks after it are reported as
  not attempted rather than each waiting out the 30-second connection
  timeout.

- `health --type connection` flagged 500 connections, or 100 idle in
  transaction, whatever `max_connections` was, and counted every process in
  `pg_stat_activity`, the server's own included. On a server with
  `max_connections` 100 it could never fire; on a 4,000-connection Neon
  compute it fired at an eighth of capacity. It now counts client backends
  against the slots an ordinary role can use (`max_connections` less
  `superuser_reserved_connections` and, from PostgreSQL 16,
  `reserved_connections`) and flags 80% of them, a judgment it states. Idle
  in transaction is judged by how long rather than how many: a session idle
  in a transaction for a minute or more is flagged, because it keeps its
  locks, even the share lock of a plain read, which queues DDL and everything
  behind it. Each verdict is now a finding with its evidence:
  `connections_in_use` carries the counts, and each such session is an
  `idle_in_transaction` finding with its pid, role, database, application
  name, how long it has been idle and the start of its last statement, with
  `query_truncated` when the server cut it short. The
  two verdicts once shared one baseline entry, so accepting a forgotten psql
  session also accepted running out of connections; they are now apart, and a
  session is known by its role, database and application name rather than
  its pid, which changes every time the job reconnects. The metrics
  `total_connections`, `idle_connections`, `max_total_connections` and
  `max_idle_connections` give way to `client_connections`,
  `available_connections`, `in_use_percent`, `long_idle_in_transaction` and
  the settings they come from. A role without `pg_read_all_stats` cannot see
  the type or state of other roles' backends; the check counts those with a
  database as possible clients, says when they could change the verdict, and
  says "none of the visible ones" rather than "none" are idle in transaction.

- Connected as a role without `pg_read_all_stats`, `activity` printed
  `(nothing running)` while other roles' transactions held locks. PostgreSQL
  withholds such a role's view of other roles' backends, their state, query,
  wait and timings, and the filter that hides idle backends dropped them
  through NULL logic. It does not withhold whether a backend is in a
  transaction, which its transaction ID and the lock on its own virtual
  transaction show, nor the locks it holds. `activity` now lists the ones in a
  transaction as `in a transaction`, with the locks they hold and the
  backends they block, and says how many others it left out, of which roles,
  and that whether those are running cannot be told; `--all` lists them.
  Since their timings are withheld, `--min-duration` names the ones in a
  transaction. `locks` drew a withheld backend as `?`, and `activity
  --blocked` left a withheld blocker as a bare pid; both now say what is
  still shown of it. `vacuum-progress` printed `None/None` and `autovacuum
  False` for a vacuum it was not shown, and now prints blanks and leaves
  `autovacuum` unknown. `top-queries` listed `<insufficient privilege>` rows
  without saying whose. Each command now says on standard error, in text and
  with `--json` (whose output is the array it was), how many rows were
  withheld and the statement that would show them. From PostgreSQL 15 the
  statistics views ask for the privileges of `pg_read_all_stats`, not
  membership, so a member of `pg_monitor` that does not inherit it is
  withheld from too, and is told `GRANT ... WITH INHERIT TRUE` on 16 and
  later, or `ALTER ROLE ... INHERIT` on 15, rather than a grant that changes
  nothing. It works in restricted mode and on a standby.

- `health` called every index in a database unused the day after a crash,
  a failover, a restore or `pg_stat_reset()`, which starts the index
  counters again: each had been scanned almost never, in the hours the
  counters had. The unused-index check now reads how long they have been
  counting and declines to judge on less than seven days, saying so and why
  they started again; `--min-window-days` moves that. A week is a judgment:
  it covers a weekly job, and is the shortest history DBtune asks for; no
  source gives a figure for indexes. On PostgreSQL 16 and later a flagged
  index carries `last_scan`. The `scan` check's `counting_since` was the
  database's `stats_reset`, which a crash leaves NULL; it is now the same
  window. The `index`, `scan` and `statistics` checks put the window in their
  metrics (`counting_since`, `counting_seconds`, `counting_began_with`) and
  in their summaries, and `config`'s spill, checkpoint and cache findings in
  their evidence. Cumulative `top-queries` says on standard error since when
  its totals count, how many times `pg_stat_statements` evicted entries
  since, and, after a crash, that it lost what it had recorded since the last
  clean shutdown, which its own reset time does not show.

- `--role` (and a context's `role`) ran the user's query without the role's
  own `ALTER ROLE ... SET` values, which PostgreSQL applies at login and
  `SET ROLE` does not: on Supabase `authenticated` ran without its
  8-second `statement_timeout` and its `work_mem`, so a plan or a timeout
  could differ from the application's. They are now applied in the same
  transaction setup, the role's value for this database over its value for
  all databases. The context's settings stand over them, and so do
  `--timeout`, restricted mode's timeout and `--search-path` when pg-debug
  sets its own; a setting the connecting user may not set is left out
  rather than failing every statement. The README and the skill said to
  copy them with `--set`, which is no longer needed.

- `top-queries --limit` did nothing in its default ranking, `resources`
  over cumulative counters: it listed every query over 5% of any resource,
  however many, in order of execution time, so `--limit 3` could print
  thirty rows. It now lists the queries over the threshold with the largest
  share of any one resource first, as `--interval` already ranked them, up to
  `--limit`. `get_top_resource_queries()` takes `limit`.

- After a crash or `pg_stat_reset()`, `health --type statistics` said every
  table's statistics were current, and `tune-queries` and `tune-workload`
  refused with "Statistics are not up-to-date", on the same database. Both
  judged whether a table has statistics from the activity counters, which a
  reset zeroes together with the times of the last ANALYZE, though it leaves
  the statistics themselves; the advisor also read only `last_analyze`, which
  autovacuum's ANALYZE never sets, so a database analyzed only by autovacuum
  was refused too. Whether a table has statistics is now read from
  `pg_stats`, and a table with data and none is reported as having no planner
  statistics, with its size, whatever the counters say. The same reading
  serves `diagnose` and `index-use`, which name a table without statistics
  as the reason for a misestimate. Drift since the last ANALYZE is still read
  from the counters, which start again at a reset.

- `top-queries`, `tune-workload` and `partitions` read `pg_stat_statements`
  rows of every database on the server, so another database's statements were
  ranked as this one's workload, and the advisors tried to plan queries
  against tables this database does not have. They now read the rows of the
  database they are connected to. They named the view without its schema, so
  with a `--search-path` that left out the schema it was installed in
  (`public`, or `extensions` on Supabase) every one of them failed with
  `relation "pg_stat_statements" does not exist`; it is now found through
  `pg_extension`, in whatever schema it is. `tune-workload` and `partitions`
  asked for `total_exec_time`, which `pg_stat_statements` before 1.8
  (PostgreSQL 12) does not have, so `tune-workload` failed there and
  `partitions` judged every table without a workload; the column names now
  follow the extension's own version, which is what decides them after an
  upgrade that did not run `ALTER EXTENSION pg_stat_statements UPDATE`.
  `tune-workload` without the extension reported no recommendations, as if it
  had looked; it now says the extension is missing. `top-queries --interval`
  kept a statement run inside a function and the same statement run at top
  level, which `pg_stat_statements` 1.9 records as two rows, as one, and
  subtracted one's counters from the other's.

- `top-queries --interval`, as a role without `pg_read_all_stats`, reported
  other roles' statements as one. `pg_stat_statements` shows such a role
  their counters but not their queryid, and the sample matched its snapshots
  by queryid, user and database, so every statement of a role in a database
  was one key: seven statements came back as one row with one statement's
  counters, and a lower call count read as a reset, so a cumulative total was
  reported as activity during the interval. Those statements are no longer
  ranked, and the command says on standard error how many ran.

- On a database created with `ENCODING 'SQL_ASCII'`, `health`, `config`,
  `activity`, `vacuum-progress` and `top-queries` failed with "a bytes-like
  object is required, not 'str'", and `describe` and `model` with "sequence
  item 0: expected str instance, bytes found". The other commands printed
  every name as `b'public'`, and two answered wrongly: comparing those bytes
  with the query's columns, `index-use` said the query restricted nothing,
  and `tune-queries` recommended no index where a UTF8 copy of the same
  database gets one. SQL_ASCII declares no encoding, so a client that names
  none is sent the stored bytes, and psycopg handed them over undecoded.
  pg-debug now reads the client encoding the server reports once connected,
  and only when that is SQL_ASCII does it reconnect asking for UTF-8. An
  encoding named anywhere is kept: in the URL, in `PGCLIENTENCODING`, in
  `options` or `PGOPTIONS`, in a service file, or by `ALTER DATABASE` or
  `ALTER ROLE ... SET client_encoding`, so LATIN1 data a database is set to
  read as LATIN1 still reads as LATIN1; `client_encoding=auto`, which a C
  locale turns into SQL_ASCII, counts as naming none. A SQL_ASCII server
  converts nothing but refuses to send bytes that are not UTF-8; pg-debug
  then says the database holds bytes that are not UTF-8 and how to name their
  encoding (`?client_encoding=LATIN1`), rather than guessing. The encoding is
  asked for as a startup parameter, not with a `SET`, so behind PgBouncer in
  transaction mode other clients of pg-debug's backend still read SQL_ASCII.
  A psycopg connection of your own given to `SqlDriver` with a SQL_ASCII
  client encoding is refused with a `QueryError` that names
  `client_encoding="UTF8"`, where it raised a `TypeError` or returned bytes.

- A process that talked to two servers used the first one's major version
  for both, because the version was kept once per process: a library user
  reviewing a PostgreSQL 15 after a 17 had `config` query
  `pg_stat_checkpointer`, which 15 does not have, and the version checks in
  `activity`, `top-queries`, `explain` and the `scan` health check took the
  wrong branch the same way. The version is now kept per connection pool and
  asked again when the pool reconnects, so a server upgraded or failed over
  behind the same URL is not remembered as the old one. The CLI talks to one
  server per run and was not affected. The version is also read from
  `server_version_num`: on a pre-release such as `18beta1` or `19devel`,
  every version-gated command failed with "invalid literal for int()".

- On Neon, `config` reported the `fsync = off` and `full_page_writes = off`
  every Neon compute runs with as HIGH, so `config --exit-code` failed on
  every Neon database. Neon acknowledges a commit once a quorum of its
  safekeepers holds the commit's WAL, so the compute's own fsync is not what
  makes it durable, and its storage rebuilds pages from the WAL, so there is
  no torn page on the compute's disk for a full-page image to repair. pg-debug now
  recognises the platform it runs on, from the connection URL's host and
  options and from settings, functions and roles the platform adds, read with
  one statement restricted mode allows. Where the server has the
  `neon.timeline_id` setting or the host is under `neon.tech`, each is a low
  note that names that signal and does not fail `--exit-code`; so is a
  Databricks Lakebase host, built on Neon. A
  `neon_superuser` role alone is not enough, since any superuser can create
  one and a dump of a Neon project's roles carries it anywhere: then the
  finding stays HIGH and says why. None of the signals has been tried on a
  live platform, so a server none of them matches is reported as
  unrecognised, not as self-hosted, and a Lakebase host is not taken for
  Neon.

- Where HypoPG was missing, `explain -i`, `tune-queries` and `tune-workload`
  told the user to install it with an `execute_query` tool that does not
  exist, and, where the server did not list it, to install a package with
  `apt-get` or `yum`, which no managed platform allows. The message now names
  `pg-debug execute "CREATE EXTENSION hypopg"` (`PgDebug.execute_sql` from
  Python) and asks the user to check with the database's owner first, since
  it changes the database. On Azure it puts the `azure.extensions` allowlist
  first, with what it allows now, and warns against `pg_catalog`, whether or
  not the server lists HypoPG yet. On Cloud SQL, Databricks Lakebase, Tiger
  Cloud, Prisma Postgres and Aurora DSQL it says the platform does not offer
  HypoPG and that `explain`, `diagnose` and `index-use` still work. Each
  message names the signal the platform was recognised by; the package
  instructions remain for a server pg-debug does not recognise.

- On Aurora DSQL, restricted mode and `--timeout` set `statement_timeout` in
  every transaction, which DSQL's documentation says it refuses at any scope,
  so every statement would have failed there. When the connection URL names
  a DSQL endpoint (a cluster's host, a PrivateLink host such as
  `<cluster>.dsql-fnh4.<region>.on.aws` or `vpce-….vpce.amazonaws.com`, or
  `amzn-cluster-id` in the URL's options or `PGOPTIONS`), pg-debug no longer
  sets it: it stops waiting for a statement after the same number of seconds
  and asks the server to cancel it, in both access modes, and warns that this
  bound is weaker than the server's. This rests on DSQL's documentation;
  pg-debug has not been run against DSQL.

- `health --type replication` warned "Replica is not actively replicating
  from primary!" on a healthy replica. It decided from the replica's own
  `pg_stat_replication`, which lists only the standbys streaming from it, so
  every replica without standbys of its own was reported unhealthy. It now
  reads the replica's WAL receiver and says whether it streams, and from which
  host, or why not; a receiver hidden from a role without `pg_read_all_stats`
  is said to be unreadable rather than stopped. In restricted mode the lag
  was left out without a word, because the functions it is read from were not
  on the allowlist; they are now, and a lag that cannot be read comes with the
  reason. On a primary, Neon's `walproposer` connection to its safekeepers no
  longer counts as a replica.

- `check-migration` offered `CREATE INDEX CONCURRENTLY` as the safer form of
  an index on a partitioned table, which PostgreSQL refuses, sized the
  partitioned table at 0 bytes, since it has no storage of its own, and
  reviewed `CREATE INDEX ... ON ONLY`, which builds nothing, as a full build.
  Against the database it now sizes a partitioned table by its partitions,
  says the build runs on every one of them and that `CONCURRENTLY` on the
  parent fails, and gives the recipe that blocks no writes, with the
  partitions' names: the index `ON ONLY` the parent, then each partition's
  built concurrently and attached. Offline it says what a partitioned table
  would change.

- Behind a pooler that does not support prepared statements, `health`,
  `model`, `tune-queries` and `tune-workload` failed with `prepared statement
  "_pg3_0" already exists`, and so did a restricted-mode command that ran six
  statements or more. psycopg prepares a statement on the server once it has
  run five times on a connection, and behind a transaction pooler a connection
  is not one session: the next execution, or another client's preparation of
  the same name, reaches a different backend. Supabase documents prepared
  statements as unsupported on Supavisor's transaction port, and the PgBouncer
  poolers Cloud SQL, AlloyDB and Azure offer set `max_prepared_statements` to
  0 by default. pg-debug's pool now prepares nothing.

- `--timeout`, restricted mode's 30-second default and `--search-path` were
  set with `SET` on each pooled connection. Behind a transaction pooler
  (PgBouncer in transaction mode, Supavisor, the pooled endpoints of Neon and
  Supabase) that reached whichever backends those statements ran on, missed
  the ones pg-debug's later statements ran on, and stayed after pg-debug
  exited, for the pooler's next client of the same database and role: a
  read-only diagnostic run gave the application's sessions a 30-second
  statement timeout. They are now set inside each statement's own
  transaction, as the query context is, and end with it.

- Hypothetical indexes outlived the command that made them. `explain
  --hypothetical-index` never dropped its own, and the index advisor dropped
  its own in a transaction of their own, which behind a transaction pooler may
  reach another backend. HypoPG keeps them in the backend's memory, where the
  next client given that backend planned with them: its plain `EXPLAIN` chose
  `<13605>btree_orders_amount`, an index that does not exist. Every batch now
  drops its hypothetical indexes in the transaction that created them, after
  the plan, and when the plan fails or is cancelled, as restricted mode's
  timeout and Ctrl-C cancel it.
- Restricted mode ran functions its allowlist refuses when the query called
  them in `FROM` or in a `VALUES` list: `SELECT * FROM
  pg_terminate_backend(12345)` terminated the session it named, where
  `SELECT pg_terminate_backend(12345)` was refused. The validator walked one
  level into each list in the parse tree, and pglast nests a function in
  `FROM`, and the rows of `VALUES`, a list deeper. The read-only transaction
  stopped writes to tables, but not a function that signals another backend,
  reloads the configuration or reaches outside the transaction. Every call is
  now checked wherever it appears. `generate_series` and
  `generate_subscripts`, which only compute values, join the allowlist, since
  they had passed only through the gap.

- The skill's Python reference ran a query with a setting by issuing
  `SET LOCAL` in a pinned session. Every statement commits on its own, so the
  setting ended before the query ran, which then ran with the server's
  default. It shows a `QueryContext` instead.

- Index tuning that could not plan a query blamed `pg_stat_statements` and
  kept what PostgreSQL had said to itself. The run reported such queries as
  usually ones recorded without their constants and given back values of the
  wrong type, even from `tune-queries`, whose queries never pass through
  `pg_stat_statements`, and a query on a table the role may not read got the
  same explanation. It now quotes PostgreSQL's reason for the first one —
  `permission denied for table payroll`, `operator does not exist` — and
  offers the old explanation only for queries read from
  `pg_stat_statements`.

- Restricted mode refused a statement with a bare `ValueError` reading
  `Error validating query: <the query>`. It was not a `PgDebugError`, though
  the README promises every failure is one, and it left out why: the reason
  sat only in the exception's cause, so the CLI printed none. A refusal is now
  a `QueryError` that names it (`DeleteStmt is not a read-only statement`,
  `function pg_sleep is not allowed`, `it could not be parsed (...)`), and a
  query that overruns the restricted-mode timeout is a `QueryError` too. A
  query the validator itself fails on is still refused, and says so.

- Connection failures could escape as a bare `ValueError`. `PgDebug.connect()`
  wrapped them in `ConnectionFailedError`, but the pool reconnects on its own
  when a connection drops mid-operation, and that path raised
  `ValueError("Connection attempt failed: ...")` straight past the client. The
  pool now raises `ConnectionFailedError` itself, a missing URL is an
  `InvalidArgumentError`, and a driver used before it has a connection raises
  `NotConnectedError`, which the README listed but nothing raised.

- A statement PostgreSQL rejected raised psycopg's own exception, such as
  `psycopg.errors.UndefinedTable`, straight through `PgDebug.execute_sql()`
  and every tool built on the driver. `except PgDebugError` did not catch the
  most common failure there is, though the README promises every failure is a
  `PgDebugError` and `QueryError` is documented for a query that "fails to
  execute". The driver now raises `QueryError` with PostgreSQL's message, and
  keeps psycopg's exception as its `__cause__` for the SQLSTATE. The CLI prints
  the same message as before.

- The last plain `ValueError`s are the library's own errors, so
  `except PgDebugError` catches them too. A server version that could not be
  read is a `QueryError`. A failure inside the index search is an
  `IndexTuningError` that names its cause, where it said only `Error
  evaluating configuration` or `Error in robust index comparison`. A SQL file
  the advisor cannot read, a query whose bind parameters cannot be filled in,
  and plan JSON without a `Plan` key are `InvalidArgumentError`s, which are
  still `ValueError`s for code that catches those. The `llm` tuning method,
  given a workload of more than one query, says how many there were and to
  use `dta`.

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

- The Vacuum horizon check said nothing was holding the horizon while a
  standby with `hot_standby_feedback` and no replication slot held it, so
  the bloat it caused on the primary came back with no cause named. It now
  reports such a standby by its `application_name`, from its WAL sender's
  `backend_xmin`, with how many transactions it holds back; a physical slot
  holding a standby's xmin names the standby while it is connected. Any hold
  is reported, as it already was for a slot: PostgreSQL publishes no age at
  which it becomes harmful.
- The sequence health check judged a `smallserial` column against
  `bigint`'s limit, so one at 32,000 of its 32,767 values looked as though it
  had barely started. Every type but `integer` was taken for `bigint`; it now
  uses the limit of the column's own type.
- Two runs of `tune-queries` or `tune-workload` over the same workload could
  recommend different indexes: `(b, a)` in one, `(a, b)` in the next, or
  another set altogether. Each candidate's column order, and the order the
  search broke ties in, came from iterating a Python set, which changes from
  one process to the next. Both are now sorted, so the same workload and
  statistics give the same recommendations; apply order follows each index's
  benefit on its own.

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

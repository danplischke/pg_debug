# Schema design, migrations and partitioning

Reading `model`, `check-migration` and `partitions`: what to relay, and what not to apply on trust.

## Reviewing the data model

Everything else here asks what the server is doing. This asks whether the
schema is built the way the data behaves.

```bash
pg-debug model                           # every rule, every user schema
pg-debug model --schema public
pg-debug model --rule unindexed_foreign_key
pg-debug model --proven-only             # drop what was inferred from statistics
pg-debug --json model                    # structured, with the DDL to apply
pg-debug model --exit-code               # exit 3 on a high-severity finding (CI)
```

Rules: `unindexed_foreign_key`, `foreign_key_type_mismatch`,
`missing_primary_key`, `column_order_padding`, `naive_timestamp`,
`blank_padded_char`, `nullable_never_null`, `low_cardinality_text`, `all`.
Needs no extension, and each rule is one catalog query, so it is safe to point
at production.

**Read the `confidence` field before acting on anything.** A `proven` finding
is a catalog fact — a foreign key either has an index behind it or it does not.
A `candidate` is inferred from sampled statistics and can be wrong: a column
with no NULLs in the sample may still have one, and an enum built from sampled
values will fail to apply if the sample missed a value. Every candidate carries
a `verify` query. Run it and report what it returns before proposing the DDL —
do not present a candidate as a fact.

`severity` already accounts for the evidence, so do not re-rank by rule name.
An unindexed foreign key is `high` only when the child table is large *and* the
parent has recorded deletes; on a small table the same finding is `low` and is
not worth interrupting anyone about. `remediation` is ready to run, but read it
first: several rules emit statements that rewrite the table under an ACCESS
EXCLUSIVE lock, and they say so in `detail`.

`column_order_padding` is the one whose numbers can be checked. It predicts the
page count of the reordered table exactly when every column is fixed-width, and
`evidence` carries `current_pages` and `packed_pages` so the claim can be
tested rather than believed. Its remediation is a `CREATE TABLE` sketch, not a
migration: constraints, defaults, indexes and triggers are not reproduced, and
saying so is part of relaying it honestly.

An empty result means the model has nothing wrong with it that these rules can
see — say that, rather than reaching for something to report.

## Reviewing a migration before it runs

```bash
pg-debug check-migration 001_add_column.sql   # a file
pg-debug check-migration "ALTER TABLE orders ALTER COLUMN id TYPE bigint"
pg-debug check-migration --offline --exit-code 001.sql   # CI, no database needed
pg-debug --json check-migration 001.sql
```

Each statement reports the lock it takes, **how long it holds it**, whether it
rewrites the table, and a safer rewrite where one exists. Relay both halves:
"ACCESS EXCLUSIVE" alone frightens people about statements that finish in
microseconds, and reassures nobody about the ones that do not. `duration` is
the field that separates them.

Connected, it reports the real size of each table, and softens severity for a
small one — correct when pointed at production, misleading when pointed at a
development copy. If the database in reach is not the one the migration will
run against, use `--offline` and report the unsoftened verdict.

Facts worth knowing when reading its output, all measured rather than
recalled: `ADD COLUMN ... NOT NULL DEFAULT 0` does *not* rewrite the table on
PostgreSQL 11+; `ADD FOREIGN KEY` takes SHARE ROW EXCLUSIVE and never blocks
readers; `CREATE INDEX CONCURRENTLY` blocks nothing but cannot run inside a
transaction, so it cannot sit in a migration applied atomically. A default of
`now()` is stable and costs nothing; `clock_timestamp()` and `random()` are
volatile and force a rewrite — the tool resolves which through `pg_proc` when
connected.

## Should this table be partitioned?

```bash
pg-debug partitions                                        # every table over 1GB
pg-debug partitions --min-size 500MB                       # look at smaller ones too
pg-debug partitions --min-pruning-share 0.7                # demand a stronger case
pg-debug --json partitions
```

The verdict is decided by `pruning_share` — the fraction of recorded query time
against that table which restricts on the candidate key, and so would still
read one partition instead of all of them. Size is only a filter for what is
worth asking about; it is never the argument.

**Report the refusals.** Every table examined comes back, `recommended` true or
false, and the false ones are usually the more valuable half: a large table
where most of the query time does not filter on any one column would be made
slower by partitioning, and saying so with the number is a real answer. Do not
present the recommended ones as though they were the whole output.

`blockers` names the unique constraints and primary keys that would have to
gain the partition key. Always pass those on — they are what makes the
conversion expensive, and widening a key changes what the database will accept.

The `ddl` is the **target shape, not a migration**. It creates a new empty
partitioned table; it does not convert the existing one, which needs a copy, a
constraint reconciliation and a name swap. Say that plainly rather than letting
someone read it as a script to run against a populated table. Suggest
`pg_partman` for creating and rolling partitions afterwards.

If nothing is recommended, that is a finding, not a failure to find one.

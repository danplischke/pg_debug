# The database is stuck

Reading `activity`, `locks` and `vacuum-progress` when something is wrong right now.

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
Each row carries `blocked_by` — the pids holding the lock it is waiting for.

When backends are waiting on each other, `locks` does the tracing:

```bash
pg-debug locks                       # who is blocking whom, root blockers first
pg-debug --json locks                # the same tree as data
```

It walks `blocked_by` back from every waiter to a backend that waits on
nothing and prints that root first, with everything stuck behind it nested
beneath. Each waiter line says what it waits for — the lock mode and table, or
for a row lock, the other transaction and the table the row is in — and what
the holder has on it. The root is usually `idle in transaction`: an
application that opened a transaction, took a lock, and went off to do
something else. The query shown for it is the last one it ran before going
idle, which is how you find that code. `blocking N` on the root is how many
backends are stuck behind it in total, so when there are several roots the
one listed first is the one to deal with. A root marked as a deadlock is in a
cycle the server will break within `deadlock_timeout`.

`lock_tree()` returns the same trees from Python: each node has `pid`,
`state`, `query`, the `*_seconds` ages, `blocked_by`, `blocking` (nested
nodes), `blocked_total`, and on a waiter `waits_for` with `locktype`, `mode`,
`relation`, `tuple_relation`, `transactionid`, `held_modes` and a
`description`.

`wait_event_type` says what kind of wait it is: `Lock` means another backend,
`IO` means storage, `Timeout` usually means the query is deliberately sleeping,
and `LWLock` means internal contention. A long-running `active` query with no
wait event is simply working.

`vacuum-progress` pairs with the vacuum health check: the check tells you
wraparound is approaching, this tells you whether the vacuum fixing it is
moving. Watch `percent_scanned` across two calls rather than guessing from one.

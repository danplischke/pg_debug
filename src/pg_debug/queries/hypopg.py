"""HypoPG statements the explain tool and the index advisor both run."""

#: Drops every hypothetical index the backend holds.
#:
#: HypoPG keeps them in the backend's memory, not in the transaction, so a
#: rollback does not drop them and a plain EXPLAIN by whoever uses the backend
#: next would plan with them. A batch that creates them runs this in the same
#: transaction, as its cleanup: behind a transaction pooler the next
#: transaction may reach another backend, and the next client this one.
HYPOPG_RESET_SQL = "SELECT hypopg_reset()"

__all__ = ["HYPOPG_RESET_SQL"]

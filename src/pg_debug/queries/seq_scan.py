"""The counters behind the sequential scan check.

``pg_stat_user_tables`` counts, since the statistics were last reset, how
many times each table was read sequentially and how many rows those reads
returned, alongside how many times it was read through an index. The check
needs nothing else: a table read whole thousands of times, and rarely through
an index, is the classic shape of a missing index, and it needs no extension
to see.

``last_seq_scan`` arrived in PostgreSQL 16; before that the column is
supplied as NULL so the shape of the result does not depend on the server.
"""

from __future__ import annotations


def sequential_scans_sql(include_last_scan: bool) -> str:
    """Tables read sequentially at least once, largest reading first.

    Args:
        include_last_scan: Whether the server has ``last_seq_scan`` (16+).

    The one parameter is the smallest table size, in bytes, worth reporting.
    """
    last_scan = "s.last_seq_scan" if include_last_scan else "NULL::timestamptz AS last_seq_scan"
    return f"""
SELECT
    s.schemaname AS schema,
    s.relname AS table,
    pg_table_size(s.relid) AS size_bytes,
    c.relpages AS pages,
    s.n_live_tup AS live_rows,
    s.seq_scan AS seq_scans,
    s.seq_tup_read AS seq_rows_read,
    COALESCE(s.idx_scan, 0) AS index_scans,
    {last_scan},
    d.stats_reset AS counting_since
FROM pg_stat_user_tables s
JOIN pg_class c ON c.oid = s.relid
CROSS JOIN (SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()) d
WHERE s.seq_scan > 0
  AND pg_table_size(s.relid) >= {{}}
ORDER BY s.seq_scan::numeric * c.relpages DESC
LIMIT 200
"""


__all__ = ["sequential_scans_sql"]

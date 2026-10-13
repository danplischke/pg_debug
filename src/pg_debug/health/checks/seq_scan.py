"""Health check for tables read whole, over and over.

A sequential scan is the right way to read a small table or most of a large
one, and the wrong way to find a few rows in a large one. The statistics
cannot see which queries did the reading, but they can see the shape: a
table of some size, read sequentially far more often than through an index,
with each scan returning most of its rows. That is what a predicate with no
index behind it looks like from the outside, and it is visible with no
extension installed.

The check argues in bytes. A count of scans says little on its own — a
reporting table scanned once a day is fine, a lookup table scanned ten
thousand times is tiny — so each table is charged the data its scans read:
scans times size, scaled down when a scan returned only part of the table,
which is what a LIMIT that stopped early looks like. A table is reported only
when that reading is large and sequential reads outnumber index reads.
"""

from __future__ import annotations

from typing import Any
from typing import LiteralString
from typing import cast

import humanize

from ...db import SafeSqlDriver
from ...db import SqlDriver
from ...db import get_postgres_version
from ...queries.seq_scan import sequential_scans_sql
from ..result import HealthCheckResult

SEQ_SCAN_HEALTH = "Sequential scan health"

#: Below this a table is read whole in a handful of pages, and an index would
#: not be chosen anyway.
DEFAULT_MIN_TABLE_BYTES = 10 * 1024 * 1024

#: The data a table's sequential scans have to have read, in total, before
#: they are worth a line in the report.
DEFAULT_MIN_BYTES_READ = 1024 * 1024 * 1024

#: Sequential scans as a share of all scans of the table. A table that is
#: mostly read through indexes and occasionally in full is being used as
#: intended.
DEFAULT_MIN_SEQ_SHARE = 0.5

#: pg_stat_user_tables.last_seq_scan was added in PostgreSQL 16.
LAST_SCAN_MIN_VERSION = 16


class SeqScanHealthCalc:
    """Flags tables whose sequential scans have read far more than their indexes have."""

    def __init__(
        self,
        sql_driver: SqlDriver | SafeSqlDriver,
        min_table_bytes: int = DEFAULT_MIN_TABLE_BYTES,
        min_bytes_read: int = DEFAULT_MIN_BYTES_READ,
        min_seq_share: float = DEFAULT_MIN_SEQ_SHARE,
    ):
        self.sql_driver = sql_driver
        self.min_table_bytes = min_table_bytes
        self.min_bytes_read = min_bytes_read
        self.min_seq_share = min_seq_share

    async def sequential_scan_check(self) -> HealthCheckResult:
        """Report tables read sequentially often enough, and whole enough, to want an index.

        Each finding carries the counters it rests on: scans of each kind,
        rows returned per sequential scan against the table's rows, the
        estimated bytes those scans read, and when the counting started. The
        queries behind the scans are not visible here; ``top-queries`` lists
        them and ``tune-queries`` says which index serves them.
        """
        thresholds = {
            "min_table_bytes": self.min_table_bytes,
            "min_bytes_read": self.min_bytes_read,
            "min_seq_share": self.min_seq_share,
        }
        include_last_scan = await get_postgres_version(cast(SqlDriver, self.sql_driver)) >= LAST_SCAN_MIN_VERSION
        query = cast(LiteralString, sequential_scans_sql(include_last_scan))
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query, [self.min_table_bytes])
        tables = [row.cells for row in rows] if rows else []

        counting_since = tables[0].get("counting_since") if tables else None
        metrics: dict[str, Any] = {**thresholds, "counting_since": str(counting_since) if counting_since else None}

        findings = [finding for finding in (self._assess(row) for row in tables) if finding is not None]
        findings.sort(key=lambda f: -f["estimated_bytes_read"])

        if not findings:
            return HealthCheckResult(
                name=SEQ_SCAN_HEALTH,
                healthy=True,
                summary="No table is being read sequentially more than its size and index use justify.",
                metrics=metrics,
            )

        lines = ["Tables read sequentially far more than through an index:"]
        for finding in findings:
            lines.append(self._describe(finding))
        lines.append(
            "The queries behind these scans are what `pg-debug top-queries` lists; `pg-debug tune-queries` will say which index serves them."
        )

        return HealthCheckResult(
            name=SEQ_SCAN_HEALTH,
            healthy=False,
            summary="\n".join(lines),
            findings=findings,
            metrics=metrics,
        )

    def _assess(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """The finding for one table, or None when its reading is within reason."""
        seq_scans = int(row.get("seq_scans") or 0)
        index_scans = int(row.get("index_scans") or 0)
        size_bytes = int(row.get("size_bytes") or 0)
        live_rows = int(row.get("live_rows") or 0)
        rows_read = int(row.get("seq_rows_read") or 0)
        if seq_scans <= 0 or size_bytes < self.min_table_bytes:
            return None

        rows_per_scan = rows_read / seq_scans
        # A scan that returned a fraction of the table stopped early — a
        # LIMIT satisfied, an EXISTS that found its row — and read about that
        # fraction of the pages. Charge it for what it read.
        scanned_fraction = min(1.0, rows_per_scan / live_rows) if live_rows > 0 else 1.0
        estimated_bytes_read = int(seq_scans * size_bytes * scanned_fraction)
        seq_share = seq_scans / (seq_scans + index_scans)

        if estimated_bytes_read < self.min_bytes_read or seq_share < self.min_seq_share:
            return None

        return {
            "schema": row.get("schema"),
            "table": row.get("table"),
            "size_bytes": size_bytes,
            "pages": int(row.get("pages") or 0),
            "live_rows": live_rows,
            "seq_scans": seq_scans,
            "seq_rows_read": rows_read,
            "rows_per_scan": round(rows_per_scan, 1),
            "scanned_fraction": round(scanned_fraction, 4),
            "index_scans": index_scans,
            "seq_share": round(seq_share, 4),
            "estimated_bytes_read": estimated_bytes_read,
            "last_seq_scan": str(row["last_seq_scan"]) if row.get("last_seq_scan") else None,
        }

    @staticmethod
    def _describe(finding: dict[str, Any]) -> str:
        qualified = f"{finding['schema']}.{finding['table']}"
        size = humanize.naturalsize(finding["size_bytes"], binary=True)
        read = humanize.naturalsize(finding["estimated_bytes_read"], binary=True)
        text = (
            f"Table '{qualified}' ({size}) was read sequentially {finding['seq_scans']:,} times against {finding['index_scans']:,} "
            f"index scans ({finding['seq_share']:.0%} sequential), about {read} of reading"
        )
        if finding["live_rows"]:
            text += f"; each scan returned about {finding['rows_per_scan']:,.0f} of its {finding['live_rows']:,} rows"
        if finding["last_seq_scan"]:
            text += f"; last at {finding['last_seq_scan']}"
        return text


__all__ = [
    "DEFAULT_MIN_BYTES_READ",
    "DEFAULT_MIN_SEQ_SHARE",
    "DEFAULT_MIN_TABLE_BYTES",
    "SEQ_SCAN_HEALTH",
    "SeqScanHealthCalc",
]

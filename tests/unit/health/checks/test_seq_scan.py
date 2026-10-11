"""The sequential scan check: charged in bytes, quiet when the reading is justified."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import SqlDriver
from pg_debug.health.checks.seq_scan import SEQ_SCAN_HEALTH
from pg_debug.health.checks.seq_scan import SeqScanHealthCalc

MiB = 1024 * 1024
GiB = 1024 * MiB


def table(
    name="orders",
    size_bytes=200 * MiB,
    live_rows=2_000_000,
    seq_scans=5_000,
    seq_rows_read=None,
    index_scans=10,
    last_seq_scan="2026-10-07 09:00:00+00",
):
    return {
        "schema": "public",
        "table": name,
        "size_bytes": size_bytes,
        "pages": size_bytes // 8192,
        "live_rows": live_rows,
        "seq_scans": seq_scans,
        "seq_rows_read": seq_rows_read if seq_rows_read is not None else seq_scans * live_rows,
        "index_scans": index_scans,
        "last_seq_scan": last_seq_scan,
        "counting_since": "2026-09-01 00:00:00+00",
    }


def driver(rows=()):
    d = MagicMock(spec=SqlDriver)
    d.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=row) for row in rows])
    return d


@pytest.fixture
def pg16():
    with patch("pg_debug.health.checks.seq_scan.get_postgres_version", AsyncMock(return_value=16)):
        yield


async def check(rows, **thresholds):
    return await SeqScanHealthCalc(driver(rows), **thresholds).sequential_scan_check()


@pytest.mark.asyncio
async def test_nothing_scanned_is_healthy(pg16):
    result = await check([])

    assert result.healthy is True
    assert result.name == SEQ_SCAN_HEALTH
    assert result.findings == []
    assert result.metrics["min_bytes_read"] == GiB


@pytest.mark.asyncio
async def test_a_table_read_whole_thousands_of_times_is_reported_in_bytes(pg16):
    result = await check([table()])

    assert result.healthy is False
    (finding,) = result.findings
    assert finding["estimated_bytes_read"] == 5_000 * 200 * MiB
    assert finding["seq_share"] == pytest.approx(5_000 / 5_010, abs=1e-4)
    assert finding["scanned_fraction"] == 1.0
    assert "public.orders" in result.summary
    assert "5,000 times against 10 index scans" in result.summary
    assert "top-queries" in result.summary


@pytest.mark.asyncio
async def test_scans_that_stop_early_are_charged_for_what_they_read(pg16):
    """Each scan returned a thousandth of the table: a LIMIT satisfied early.
    Five thousand of them read five tables' worth, not five thousand."""
    rows = [table(seq_rows_read=5_000 * 2_000, size_bytes=200 * MiB)]

    result = await check(rows)

    assert result.healthy is True, "5 x 200 MiB is under a GiB"
    result = await check(rows, min_bytes_read=500 * MiB)
    (finding,) = result.findings
    assert finding["scanned_fraction"] == pytest.approx(0.001)
    assert finding["estimated_bytes_read"] == pytest.approx(5 * 200 * MiB, rel=0.01)


@pytest.mark.asyncio
async def test_a_table_mostly_read_through_indexes_is_fine(pg16):
    result = await check([table(seq_scans=100, index_scans=100_000)])

    assert result.healthy is True


@pytest.mark.asyncio
async def test_a_small_table_is_not_reported_however_often_it_is_read(pg16):
    """The query already filters on size; the check filters again so a caller
    passing rows straight in gets the same answer."""
    result = await check([table(size_bytes=2 * MiB, seq_scans=1_000_000)])

    assert result.healthy is True


@pytest.mark.asyncio
async def test_findings_are_ordered_by_bytes_read(pg16):
    result = await check([table("small", size_bytes=20 * MiB, seq_scans=100), table("big", size_bytes=1 * GiB, seq_scans=100)])

    assert [f["table"] for f in result.findings] == ["big", "small"]


@pytest.mark.asyncio
async def test_the_size_threshold_is_passed_to_the_query(pg16):
    d = driver([])

    await SeqScanHealthCalc(d, min_table_bytes=12345).sequential_scan_check()

    query = d.execute_query.await_args.args[0]
    assert "pg_table_size(s.relid) >= 12345" in query
    assert "s.last_seq_scan" in query


@pytest.mark.asyncio
async def test_last_scan_is_null_before_postgres_16():
    d = driver([])
    with patch("pg_debug.health.checks.seq_scan.get_postgres_version", AsyncMock(return_value=15)):
        await SeqScanHealthCalc(d).sequential_scan_check()

    query = d.execute_query.await_args.args[0]
    assert "NULL::timestamptz AS last_seq_scan" in query
    assert "s.last_seq_scan" not in query


@pytest.mark.asyncio
async def test_the_report_says_when_counting_started(pg16):
    result = await check([table()])

    assert result.metrics["counting_since"] == "2026-09-01 00:00:00+00"
    assert "last at 2026-10-07 09:00:00+00" in result.summary

"""logs --from-database: the server's own log files read through log_fdw.

The driver is a fake that answers as log_fdw's source
(github.com/aws/postgresql-logfdw) says the extension does: a stderr file is a
table with one ``log_entry`` per line, a csvlog file a table with csvlog's
columns, typed. It serves the RDS-prefix fixtures, so the report read through
it is compared with the report read from the same files directly.
"""

import csv
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.errors import ExtensionRequiredError
from pg_debug.errors import InvalidArgumentError
from pg_debug.logs import analyze_logs
from pg_debug.logs.server import LogFile
from pg_debug.logs.server import choose_files
from pg_debug.logs.server import read_server_logs

FIXTURES = Path(__file__).parent / "fixtures"

CSV_COLUMNS = [
    "log_time",
    "user_name",
    "database_name",
    "process_id",
    "connection_from",
    "session_id",
    "session_line_num",
    "command_tag",
    "session_start_time",
    "virtual_transaction_id",
    "transaction_id",
    "error_severity",
    "sql_state_code",
    "message",
    "detail",
    "hint",
    "internal_query",
    "internal_query_pos",
    "context",
    "query",
    "query_pos",
    "location",
    "application_name",
    "backend_type",
    "leader_pid",
    "query_id",
]
TYPED = {name: int for name in ("process_id", "session_line_num", "transaction_id", "internal_query_pos", "query_pos", "leader_pid", "query_id")}


def csv_rows() -> list[dict[str, Any]]:
    """The csvlog fixture as log_fdw's table returns it: typed, NULL for an empty field."""
    rows = []
    with open(FIXTURES / "rds-pg16.csv", newline="", encoding="utf-8") as f:
        for record in csv.reader(f):
            row: dict[str, Any] = {}
            for name, value in zip(CSV_COLUMNS, record + [""] * (len(CSV_COLUMNS) - len(record)), strict=True):
                if value == "":
                    row[name] = None
                elif name in ("log_time", "session_start_time"):
                    row[name] = datetime.strptime(value.replace(" UTC", "+0000"), "%Y-%m-%d %H:%M:%S.%f%z" if "." in value else "%Y-%m-%d %H:%M:%S%z")
                elif name in TYPED:
                    row[name] = TYPED[name](value)
                else:
                    row[name] = value
            rows.append(row)
    return rows


class LogFdwServer:
    def __init__(self, schema="public", server="log_server", files=None):
        self.schema, self.server = schema, server
        self.files = files if files is not None else {"postgresql.log.2026-11-21-18": 1, "postgresql.log.2026-11-21-18.csv": 1}
        self.statements: list[str] = []
        self.rolled_back: list[str] = []

    async def execute_query(self, query, params=None, force_readonly=False, *, in_context=False, cleanup=None):
        self.statements.append(query)
        if "pg_foreign_server" in query:
            return [SqlDriver.RowResult(cells={"schema": self.schema, "server": self.server, "available": True})]
        if "list_postgres_log_files()" in query:
            return [SqlDriver.RowResult(cells={"file_name": name, "file_size_bytes": size}) for name, size in self.files.items()]
        raise AssertionError(query)

    async def execute_query_and_rollback(self, query, params=None, *, in_context=False):
        self.rolled_back.append(query)
        if "-18.csv'" in query:
            return [SqlDriver.RowResult(cells=row) for row in csv_rows()]
        lines = (FIXTURES / "rds-pg16.log").read_text(encoding="utf-8").splitlines()
        return [SqlDriver.RowResult(cells={"log_entry": line}) for line in lines]


def summary(report):
    return (
        report.entries,
        report.unparsed,
        [(p.query, p.runs, len(p.findings)) for p in report.plans],
        [(c.kind, c.count, c.by) for c in report.connections],
    )


@pytest.mark.asyncio
async def test_the_csv_file_is_read_and_gives_the_report_the_file_gives():
    server = LogFdwServer()
    report = await read_server_logs(server)  # type: ignore[arg-type]

    assert list(report.sources) == ["postgresql.log.2026-11-21-18.csv (log_fdw)"]
    assert summary(report) == summary(analyze_logs([FIXTURES / "rds-pg16.csv"]))
    [statement] = server.rolled_back
    assert "public.create_foreign_table_for_log_file('pg_debug_log_" in statement
    assert "'log_server', 'postgresql.log.2026-11-21-18.csv'); SELECT * FROM pg_debug_log_" in statement


@pytest.mark.asyncio
async def test_a_stderr_file_is_read_line_by_line_with_rds_s_prefix():
    server = LogFdwServer(files={"postgresql.log.2026-11-21-18": 1})
    report = await read_server_logs(server)  # type: ignore[arg-type]

    assert summary(report) == summary(analyze_logs([FIXTURES / "rds-pg16.log"]))


@pytest.mark.asyncio
async def test_restricted_mode_refuses_before_reading_anything():
    with pytest.raises(InvalidArgumentError, match="restricted"):
        await read_server_logs(SafeSqlDriver(MagicMock(spec=SqlDriver)))


@pytest.mark.asyncio
async def test_without_log_fdw_or_its_server_the_statements_that_set_it_up_are_given():
    with pytest.raises(ExtensionRequiredError, match="CREATE EXTENSION log_fdw"):
        await read_server_logs(LogFdwServer(schema=None))  # type: ignore[arg-type]
    with pytest.raises(InvalidArgumentError, match="CREATE SERVER log_server FOREIGN DATA WRAPPER log_fdw"):
        await read_server_logs(LogFdwServer(server=None))  # type: ignore[arg-type]


def test_the_latest_files_are_chosen_csv_over_its_stderr_twin_oldest_first():
    files = [
        LogFile("postgresql.log.2026-11-21-17", 10),
        LogFile("postgresql.log.2026-11-21-18", 10),
        LogFile("postgresql.log.2026-11-21-18.csv", 10),
        LogFile("postgresql.log.2026-11-21-16", 10),
    ]
    assert [f.name for f in choose_files(files, None, 2)] == ["postgresql.log.2026-11-21-17", "postgresql.log.2026-11-21-18.csv"]
    assert [f.name for f in choose_files(files, ["postgresql.log.2026-11-21-16"], 1)] == ["postgresql.log.2026-11-21-16"]
    with pytest.raises(InvalidArgumentError, match="Not in the server's log directory: nope"):
        choose_files(files, ["nope"], 1)

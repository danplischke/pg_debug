"""Server logs read over SQL, through ``log_fdw``, where no file can be reached.

On Amazon RDS and Aurora the log is not a file anyone can open: it is
downloaded from the console or the API, which needs AWS credentials, or read
inside the database with ``log_fdw``. ``log_fdw`` lists the files in the log
directory (``list_postgres_log_files()``) and makes a foreign table of one
(``create_foreign_table_for_log_file(table, server, file)``): a csvlog file
becomes a table with csvlog's columns, a stderr file a table with one
``log_entry`` column per line. pg-debug creates that table, reads it and rolls
the transaction back in one call, so nothing is left in the database and a
transaction pooler is never asked to keep the table between statements.

Creating the table is a write, so restricted mode refuses. ``log_fdw``'s
functions are owned by ``rds_superuser``, who grants them to other roles; the
extension and its foreign server are created once, by that role:
``CREATE EXTENSION log_fdw; CREATE SERVER log_server FOREIGN DATA WRAPPER
log_fdw;``. This reader follows AWS's documentation and the extension's
source (github.com/aws/postgresql-logfdw) and has not been run on RDS.
"""

from __future__ import annotations

import csv
import io
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from typing import LiteralString
from typing import cast

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..errors import ExtensionRequiredError
from ..errors import InvalidArgumentError
from .parse import ParsedLog
from .parse import read_log_text
from .report import LogReport
from .report import report

LOG_FDW_DOC = "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_PostgreSQL.Extensions.log_fdw.html"

SETUP = "CREATE EXTENSION log_fdw; CREATE SERVER log_server FOREIGN DATA WRAPPER log_fdw;"

#: Where log_fdw is, its foreign server, and whether the server could have it,
#: in one read of the catalog.
PRESENCE_SQL = """
SELECT
    (SELECT quote_ident(n.nspname) FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
      WHERE e.extname = 'log_fdw') AS schema,
    (SELECT s.srvname FROM pg_foreign_server s JOIN pg_foreign_data_wrapper w ON w.oid = s.srvfdw
      WHERE w.fdwname = 'log_fdw' ORDER BY s.srvname LIMIT 1) AS server,
    EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'log_fdw') AS available
"""


@dataclass(frozen=True)
class LogFile:
    """One file in the server's log directory, as ``list_postgres_log_files()`` lists it."""

    name: str
    size_bytes: int

    @property
    def is_csv(self) -> bool:
        return self.name.endswith((".csv", ".csv.gz"))


def choose_files(available: list[LogFile], names: list[str] | None, latest: int) -> list[LogFile]:
    """The files to read: the ones named, or the latest ``latest``, oldest first.

    RDS names its files by the hour they start (``postgresql.log.2026-11-21-18``),
    so their names sort in time. Where a server writes both formats, the
    stderr file and the ``.csv`` beside it hold the same messages; the csv one
    is read, since its columns need no prefix to split.

    Raises:
        InvalidArgumentError: For a name not in the log directory, or a
            ``latest`` below 1.
    """
    by_name = {file.name: file for file in available}
    if names:
        missing = [name for name in names if name not in by_name]
        if missing:
            listed = ", ".join(sorted(by_name)[-5:]) or "none"
            raise InvalidArgumentError(f"Not in the server's log directory: {', '.join(missing)}. The latest files there: {listed}.")
        return [by_name[name] for name in names]
    if latest < 1:
        raise InvalidArgumentError("--latest takes at least 1.")
    chosen: list[LogFile] = []
    for file in sorted(available, key=lambda f: f.name):
        if not file.is_csv and (f"{file.name}.csv" in by_name or f"{file.name.removesuffix('.log')}.csv" in by_name):
            continue
        chosen.append(file)
    return chosen[-latest:]


def rows_as_log_text(rows: list[dict[str, Any]], is_csv: bool) -> str:
    """A foreign table's rows as the text of the file they came from.

    A stderr table has one row per line in ``log_entry``. A csvlog table has
    csvlog's columns in csvlog's order, typed; they are written back as a CSV
    record each, a timestamp as csvlog writes it.
    """
    if not is_csv:
        return "\n".join(str(row.get("log_entry") or "") for row in rows)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for row in rows:
        writer.writerow(["" if value is None else _csv_value(value) for value in row.values()])
    return out.getvalue()


def _csv_value(value: Any) -> str:
    if isinstance(value, datetime):
        text = value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return f"{text} {value.tzname() or ''}".rstrip()
    return str(value)


async def read_server_logs(
    sql_driver: SqlDriver | SafeSqlDriver,
    files: list[str] | None = None,
    latest: int = 1,
    min_ratio: float | None = None,
) -> LogReport:
    """Read the server's own log files through ``log_fdw``, and report what they say happened.

    Args:
        sql_driver: The connection. A restricted one is refused.
        files: Log files by name, as ``list_postgres_log_files()`` lists them;
            None reads the latest.
        latest: Without ``files``, how many of the latest to read.
        min_ratio: As :func:`~pg_debug.logs.analyze_logs` takes it.

    Raises:
        InvalidArgumentError: In restricted mode, without a ``log_fdw``
            foreign server, or for a file not in the log directory.
        ExtensionRequiredError: If ``log_fdw`` is not installed.
    """
    if isinstance(sql_driver, SafeSqlDriver):
        raise InvalidArgumentError(
            "Reading the server's logs through log_fdw creates a foreign table for each file, which is a write, so restricted "
            "mode does not do it; the table is rolled back with the transaction it is read in. Use --access-mode unrestricted, "
            "or download the files (on RDS: aws rds download-db-log-file-portion) and pass them to logs."
        )
    rows = await sql_driver.execute_query(PRESENCE_SQL)
    found = rows[0].cells if rows else {}
    schema, server = found.get("schema"), found.get("server")
    if not schema:
        where = "The server has it" if found.get("available") else "The server does not list it in pg_available_extensions"
        raise ExtensionRequiredError(
            "log_fdw",
            f"Reading the logs over SQL needs log_fdw. {where}; a member of rds_superuser installs it once with {SETUP} ({LOG_FDW_DOC})",
        )
    if not server:
        raise InvalidArgumentError(f"log_fdw is installed but has no foreign server. A member of rds_superuser creates one: {SETUP.split('; ')[1]}")

    listed = await sql_driver.execute_query(cast(LiteralString, f"SELECT file_name, file_size_bytes FROM {schema}.list_postgres_log_files()"))
    available = [LogFile(str(row.cells["file_name"]), int(row.cells.get("file_size_bytes") or 0)) for row in listed or []]
    if not available:
        raise InvalidArgumentError("The server's log directory has no files (list_postgres_log_files() returned none).")

    parsed = ParsedLog()
    for file in choose_files(available, files, latest):
        # A name of our own for the table, so no table of the user's is met;
        # it lives only inside the transaction that is rolled back.
        table = f"pg_debug_log_{secrets.token_hex(6)}"
        statement = SafeSqlDriver.param_sql_to_query(
            f"SELECT {schema}.create_foreign_table_for_log_file({{}}, {{}}, {{}}); SELECT * FROM {table}", [table, server, file.name]
        )
        result = await sql_driver.execute_query_and_rollback(cast(LiteralString, statement))
        text = rows_as_log_text([dict(row.cells) for row in result or []], file.is_csv)
        read_log_text(text, f"{file.name} (log_fdw)", parsed, log_format="csvlog" if file.is_csv else None)
    return report(parsed, min_ratio=min_ratio)


__all__ = ["LOG_FDW_DOC", "SETUP", "LogFile", "choose_files", "read_server_logs", "rows_as_log_text"]

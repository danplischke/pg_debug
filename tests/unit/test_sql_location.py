"""Where the SQL pg_debug runs may be written.

The goal is one place: ``pg_debug/queries/``. Until every query has moved
there, :data:`STILL_WRITING_SQL` pins the files that still contain some, so
that no new file starts to. The list only shrinks — a file that no longer
writes any SQL has to come off it, which is how progress stays visible.

A string counts as SQL when it starts with a statement keyword in upper case,
as every query in this package is written. Docstrings and exception messages
are not SQL, whatever they start with. Fragments that are not statements on
their own (a predicate, a column list) are not caught; they move with the
queries that use them.
"""

import ast
import re
from pathlib import Path
from typing import Iterator
from typing import Set

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "pg_debug"

#: Everything under this directory may write SQL; it is where SQL belongs.
QUERIES_DIRECTORY = "queries"

_STATEMENT = re.compile(r"^\s*(?:/\*.*?\*/\s*)?(SELECT|WITH|SHOW|EXPLAIN)\b", re.DOTALL)

STILL_WRITING_SQL = {
    "activity.py",
    "configuration.py",
    "database_health/autovacuum_calc.py",
    "database_health/buffer_health_calc.py",
    "database_health/connection_health_calc.py",
    "database_health/constraint_health_calc.py",
    "database_health/index_health_calc.py",
    "database_health/replication_calc.py",
    "database_health/sequence_health_calc.py",
    "database_health/table_bloat_calc.py",
    "database_health/vacuum_health_calc.py",
    "explain/explain_plan.py",
    "index/dta_calc.py",
    "index/index_opt_base.py",
    "index/llm_opt.py",
    "migration/lint.py",
    "model/columns.py",
    "model/keys.py",
    "model/layout.py",
    "partition/advisor.py",
    "plan/diagnoser.py",
    "schema.py",
    "parsing/bind_params.py",
    "db/extensions.py",
    "statistics.py",
    "top_queries/top_queries_calc.py",
    "tuple_layout.py",
}


def _not_sql(tree: ast.Module) -> Set[int]:
    """Ids of the strings that may start like a statement without being one."""
    ignored: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ignored.add(id(first.value))
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name.endswith(("Error", "Exception")):
                ignored.update(id(argument) for argument in node.args)
        elif isinstance(node, ast.JoinedStr):
            # The pieces of an f-string are judged as part of the whole.
            ignored.update(id(value) for value in node.values)
    return ignored


def _text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else "{}" for value in node.values)
    return None


def _writes_sql(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    ignored = _not_sql(tree)
    for node in ast.walk(tree):
        text = None if id(node) in ignored else _text(node)
        if text is not None and _STATEMENT.match(text):
            return True
    return False


def _files_writing_sql() -> Iterator[str]:
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).as_posix()
        if not relative.startswith(f"{QUERIES_DIRECTORY}/") and _writes_sql(path):
            yield relative


def test_no_new_file_writes_sql():
    added = sorted(set(_files_writing_sql()) - STILL_WRITING_SQL)
    assert added == [], f"These files write SQL; it belongs in pg_debug/{QUERIES_DIRECTORY}/: {added}"


def test_files_that_stopped_writing_sql_come_off_the_list():
    cleaned = sorted(STILL_WRITING_SQL - set(_files_writing_sql()))
    assert cleaned == [], f"These files no longer write SQL; remove them from STILL_WRITING_SQL: {cleaned}"


def test_the_check_recognises_sql():
    source = '"""SELECT is what this module is about."""\nQUERY = """\n    SELECT 1\n"""\nraise ValueError("EXPLAIN failed")\n'
    tree = ast.parse(source)
    ignored = _not_sql(tree)
    found = [text for node in ast.walk(tree) if id(node) not in ignored and (text := _text(node)) and _STATEMENT.match(text)]
    assert found == ["\n    SELECT 1\n"]

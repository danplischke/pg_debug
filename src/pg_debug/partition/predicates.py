"""Which columns a statement actually restricts on.

Partition pruning is decided by the predicates, not by the columns a query
mentions: ``SELECT created_at FROM events`` reads every partition, while
``WHERE created_at > $1`` reads one. So this collects only the columns that
appear in a WHERE clause or a join condition, and records whether they were
compared for equality or over a range — because a HASH partition prunes on the
first and not on the second.

Deliberately separate from :class:`~pg_debug.index.dta_calc.ConditionColumnCollector`,
which is built around ``visit_SelectStmt`` and so never sees a DELETE. Deletes
are the ones worth seeing here: a table whose old rows are deleted by date is
the strongest case for partitioning there is, because the delete becomes a
DROP.
"""

from __future__ import annotations

from typing import Any
from typing import Dict
from typing import List
from typing import Set
from typing import Tuple

from pglast import ast

from ..sql import TableAliasVisitor

#: Operators that let PostgreSQL prune to a subset of partitions. Equality
#: prunes under every strategy; the range operators only under RANGE and LIST.
EQUALITY_OPERATORS = frozenset({"=", "IN"})
RANGE_OPERATORS = frozenset({"<", "<=", ">", ">=", "BETWEEN"})


class Predicates:
    """The columns a statement restricts on, by table."""

    def __init__(self) -> None:
        #: table -> {(column, "equality" | "range")}
        self.by_table: Dict[str, Set[Tuple[str, str]]] = {}

    def add(self, table: str, column: str, kind: str) -> None:
        self.by_table.setdefault(table, set()).add((column, kind))

    def columns(self, table: str) -> Set[str]:
        return {column for column, _kind in self.by_table.get(table, set())}

    def kinds(self, table: str, column: str) -> Set[str]:
        return {kind for name, kind in self.by_table.get(table, set()) if name == column}


def extract_predicates(stmt: Any) -> Predicates:
    """Collect the columns a statement restricts on, resolved to their tables.

    Handles SELECT, UPDATE and DELETE. An unqualified column is attributed to
    the statement's only table; where a statement joins several and the column
    is written without a qualifier, it is skipped rather than guessed at.

    A CTE name is not a table. Counting it as one would leave a statement that
    reads a single real table looking like a join, and the unqualified columns
    in it would then be skipped — losing exactly the predicates that decide
    this, since ``WITH recent AS (SELECT … WHERE created_at > $1)`` is how a
    lot of reporting is written.
    """
    predicates = Predicates()

    aliases = TableAliasVisitor()
    aliases(stmt)
    cte_names = _cte_names(stmt)
    tables = sorted(name for name in aliases.tables if name not in cte_names)
    alias_map = {alias: table for alias, table in aliases.aliases.items() if table not in cte_names}
    only_table = tables[0] if len(tables) == 1 else None

    for qual in _restricting_clauses(stmt):
        for column, qualifier, kind in _comparisons(qual):
            table = alias_map.get(qualifier) if qualifier else only_table
            if table is None and qualifier in tables:
                table = qualifier
            if table:
                predicates.add(table, column, kind)

    return predicates


def _cte_names(node: Any) -> Set[str]:
    """The names bound by every WITH clause in the statement."""
    names: Set[str] = set()

    if isinstance(node, (list, tuple)):
        for item in node:
            names |= _cte_names(item)
        return names

    if not isinstance(node, ast.Node):
        return names

    if isinstance(node, ast.CommonTableExpr) and node.ctename:
        names.add(str(node.ctename))

    for attribute in getattr(node, "__slots__", ()) or ():
        names |= _cte_names(getattr(node, attribute, None))

    return names


def _restricting_clauses(node: Any) -> List[Any]:
    """Every clause whose predicates can prune: WHERE, and each join's ON.

    Walks the whole statement rather than its top level, because the clauses
    that matter are not always there: each arm of a UNION carries its own
    WHERE, and so does a subquery in FROM. Missing those understates how much
    of the workload prunes, which pushes the advisor towards recommending
    against a partition that would in fact have paid.

    Only these two kinds of clause are collected. A comparison in a SELECT list
    or a HAVING restricts nothing the planner can use to skip a partition, so
    walking the tree indiscriminately for comparisons would count time as
    pruning that does not.
    """
    clauses: List[Any] = []

    if isinstance(node, (list, tuple)):
        for item in node:
            clauses.extend(_restricting_clauses(item))
        return clauses

    if not isinstance(node, ast.Node):
        return clauses

    where = getattr(node, "whereClause", None)
    if where is not None:
        clauses.append(where)
    if isinstance(node, ast.JoinExpr) and node.quals is not None:
        clauses.append(node.quals)

    for attribute in getattr(node, "__slots__", ()) or ():
        # The clauses already collected are predicates, not places to find more.
        if attribute in ("whereClause", "quals"):
            continue
        clauses.extend(_restricting_clauses(getattr(node, attribute, None)))

    return clauses


def _comparisons(node: Any) -> List[Tuple[str, str, str]]:
    """Walk a clause, yielding (column, table_qualifier, kind) per comparison."""
    found: List[Tuple[str, str, str]] = []

    if isinstance(node, ast.A_Expr):
        operator = ".".join(part.sval for part in (node.name or ()))
        kind = "equality" if operator in EQUALITY_OPERATORS else "range" if operator in RANGE_OPERATORS else None
        if kind:
            for side in (node.lexpr, node.rexpr):
                reference = _column_reference(side)
                if reference:
                    found.append((*reference, kind))
        return found

    if isinstance(node, ast.NullTest):
        # IS NULL restricts, but never to a partition worth having.
        return found

    for attribute in getattr(node, "__slots__", ()) or ():
        value = getattr(node, attribute, None)
        if isinstance(value, ast.Node):
            found.extend(_comparisons(value))
        elif isinstance(value, tuple):
            for item in value:
                if isinstance(item, ast.Node):
                    found.extend(_comparisons(item))
    return found


def _column_reference(node: Any) -> Tuple[str, str] | None:
    """``(column, qualifier)`` for a column reference, or None for anything else."""
    if not isinstance(node, ast.ColumnRef) or not node.fields:
        return None
    parts = [str(field.sval) for field in node.fields if isinstance(field, ast.String)]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0], ""
    return parts[-1], parts[-2]


__all__ = ["EQUALITY_OPERATORS", "RANGE_OPERATORS", "Predicates", "extract_predicates"]

"""The restrictions a query places on its columns, read from the SQL.

An index serves a predicate, not a query, so the question "why is this index
not used" is answered predicate by predicate: which column each one restricts,
whether the column stands bare or inside an expression, which operator it
uses, and whether it sits under an OR or a NOT. Those are facts about the
statement's text, so they come from the parser rather than from the plan,
which renders conditions in its own notation and drops the ones it folded.

Only the top-level statement's WHERE clause and join conditions are read.
A subquery restricts its own tables; its predicates belong to a different
question.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from pglast import ast
from pglast import parse_sql
from pglast.enums import A_Expr_Kind
from pglast.enums import BoolExprType
from pglast.enums import NullTestType
from pglast.enums import SubLinkType
from pglast.parser import ParseError
from pglast.stream import RawStream

from ..errors import InvalidArgumentError
from ..parsing.visitors import TableAliasVisitor
from ..parsing.visitors import resolve_table_reference


@dataclass(frozen=True)
class Predicate:
    """One restriction on one column.

    Attributes:
        table: The table the column belongs to, as the query's FROM clause
            names it, or ``None`` when the column could not be attributed.
        column: The column name.
        expression: The column side of the comparison as written, with any
            table qualifier removed: ``lower(email)``,
            ``CAST(created_at AS date)``. Equal to ``column`` when the column
            stands bare. Compare it with an index column through
            :func:`normalise_expression`.
        bare: Whether the column stands on its own, with no function, cast or
            arithmetic around it.
        operator: ``=``, ``<``, ``~~`` (LIKE), ``IN``, ``BETWEEN``,
            ``IS NULL``, and so on, as PostgreSQL names them.
        pattern: The literal pattern of a LIKE, when it is a literal.
        negated: Whether a NOT applies to it, which turns a selection into an
            exclusion.
        or_group: An identifier shared by every predicate under the same OR,
            or ``None`` outside any OR.
        join_table: For a join condition, the table on the other side.
        text: The predicate as written.
    """

    table: str | None
    column: str
    expression: str
    bare: bool
    operator: str
    pattern: str | None = None
    negated: bool = False
    or_group: int | None = None
    join_table: str | None = None
    text: str = ""


@dataclass
class QueryPredicates:
    """Every predicate of a statement, with the tables it reads."""

    #: Table references as the query writes them, ``schema.name`` or ``name``.
    tables: set[str]
    #: Alias to table reference.
    aliases: dict[str, str]
    predicates: list[Predicate] = field(default_factory=list)

    def on(self, table: str) -> list[Predicate]:
        """The predicates restricting ``table``."""
        return [p for p in self.predicates if p.table == table]

    def or_group_tables(self, group: int) -> set[str]:
        """The tables restricted anywhere inside one OR."""
        return {p.table for p in self.predicates if p.or_group == group and p.table}


def read_predicates(sql: str, columns_by_table: dict[str, set[str]] | None = None) -> QueryPredicates:
    """Read the predicates of one SELECT, UPDATE or DELETE.

    Args:
        sql: One statement.
        columns_by_table: Each table's columns, keyed by the reference the
            query uses for it, so a bare column in a multi-table query can be
            attributed. Without it a bare column is attributed only when the
            query reads a single table.

    Raises:
        InvalidArgumentError: If the SQL does not parse or is not one
            statement.
    """
    try:
        statements = parse_sql(sql)
    except ParseError as e:
        raise InvalidArgumentError(f"Could not parse the query: {e}") from e
    parsed = [s.stmt for s in statements if s.stmt is not None]
    if len(parsed) != 1:
        raise InvalidArgumentError(f"Analyse one statement at a time. This is {len(parsed)} statements.")
    statement = parsed[0]

    aliases, tables = TableAliasVisitor()(statement)
    result = QueryPredicates(tables=set(tables), aliases=dict(aliases))
    reader = _Reader(result, columns_by_table or {})

    for clause in _restriction_clauses(statement):
        reader.read(clause, negated=False, or_group=None)
    return result


def _restriction_clauses(statement: Any) -> Iterator[Any]:
    """The WHERE clause and every join condition of the statement."""
    where = getattr(statement, "whereClause", None)
    if where is not None:
        yield where
    for item in getattr(statement, "fromClause", None) or ():
        yield from _join_quals(item)
    # UPDATE/DELETE keep their FROM/USING under other names.
    for attribute in ("fromClause", "usingClause"):
        for item in getattr(statement, attribute, None) or ():
            if attribute == "usingClause":
                yield from _join_quals(item)


def _join_quals(node: Any) -> Iterator[Any]:
    if isinstance(node, ast.JoinExpr):
        if node.quals is not None:
            yield node.quals
        yield from _join_quals(node.larg)
        yield from _join_quals(node.rarg)


_WHITESPACE = re.compile(r"\s+")
_PG_CATALOG = re.compile(r"\bpg_catalog\.")
_ALIAS_PREFIX = re.compile(r"\b[a-z_][a-z0-9_]*\.(?=[a-z_])")
_TEXT_LITERAL_CAST = re.compile(r"('(?:[^']|'')*')::(?:text|bpchar|varchar|character varying)")
_PARENS = re.compile(r"[()]")


class _Reader:
    def __init__(self, result: QueryPredicates, columns_by_table: dict[str, set[str]]):
        self.result = result
        self.columns_by_table = columns_by_table
        self._next_or_group = 0

    def read(self, node: Any, negated: bool, or_group: int | None) -> None:
        if isinstance(node, ast.BoolExpr):
            if node.boolop == BoolExprType.AND_EXPR:
                for arg in node.args or ():
                    self.read(arg, negated, or_group)
            elif node.boolop == BoolExprType.OR_EXPR:
                group = or_group if or_group is not None else self._new_or_group()
                for arg in node.args or ():
                    self.read(arg, negated, group)
            elif node.boolop == BoolExprType.NOT_EXPR:
                for arg in node.args or ():
                    self.read(arg, not negated, or_group)
            return
        if isinstance(node, ast.A_Expr):
            self._read_comparison(node, negated, or_group)
            return
        if isinstance(node, ast.NullTest):
            operator = "IS NULL" if node.nulltesttype == NullTestType.IS_NULL else "IS NOT NULL"
            self._emit(node.arg, operator, None, negated, or_group, node)
            return
        if isinstance(node, ast.SubLink) and node.subLinkType == SubLinkType.ANY_SUBLINK and node.testexpr is not None:
            # ``x IN (SELECT ...)`` and ``x = ANY (SELECT ...)``: a restriction
            # on x, with the subquery as the set of values.
            name = "".join(str(part.sval) for part in (node.operName or ()) if isinstance(part, ast.String))
            operator = "IN" if name in ("", "=") else f"{name} ANY"
            self._emit(node.testexpr, operator, None, negated, or_group, node)
            return
        if isinstance(node, ast.ColumnRef):
            # A bare boolean column: ``WHERE vip`` restricts vip to true.
            self._emit(node, "IS TRUE", None, negated, or_group, node)
            return
        # Anything else — a subquery, a function returning boolean — restricts
        # nothing an index can be matched against.

    def _new_or_group(self) -> int:
        self._next_or_group += 1
        return self._next_or_group

    def _read_comparison(self, node: ast.A_Expr, negated: bool, or_group: int | None) -> None:
        operator = _operator_name(node)
        if operator is None:
            return
        pattern = None
        if node.kind in (A_Expr_Kind.AEXPR_LIKE, A_Expr_Kind.AEXPR_ILIKE):
            pattern = _string_literal(node.rexpr)

        left_columns = list(_column_refs(node.lexpr))
        right_columns = list(_column_refs(node.rexpr))

        if left_columns and right_columns:
            # A join condition, or a comparison of two columns of one table.
            # Each side is a restriction on its own column, with the other
            # side's table recorded for the join case.
            left_table = self._table_of(left_columns[0])
            right_table = self._table_of(right_columns[0])
            self._emit(node.lexpr, operator, pattern, negated, or_group, node, join_table=right_table if right_table != left_table else None)
            self._emit(node.rexpr, _mirror(operator), pattern, negated, or_group, node, join_table=left_table if right_table != left_table else None)
            return
        if left_columns:
            self._emit(node.lexpr, operator, pattern, negated, or_group, node)
        elif right_columns:
            self._emit(node.rexpr, _mirror(operator), pattern, negated, or_group, node)

    def _emit(
        self,
        side: Any,
        operator: str,
        pattern: str | None,
        negated: bool,
        or_group: int | None,
        whole: Any,
        join_table: str | None = None,
    ) -> None:
        refs = list(_column_refs(side))
        if len(refs) != 1:
            # Two columns of one table on one side (``a + b = 1``) is an
            # expression no single-column match describes; leave it out.
            return
        column_ref = refs[0]
        table = self._table_of(column_ref)
        column = column_ref[-1]
        bare = isinstance(side, ast.ColumnRef)
        expression = column if bare else _ALIAS_PREFIX.sub("", RawStream()(side))
        self.result.predicates.append(
            Predicate(
                table=table,
                column=column,
                expression=expression,
                bare=bare,
                operator=operator,
                pattern=pattern,
                negated=negated,
                or_group=or_group,
                join_table=join_table,
                text=RawStream()(whole),
            )
        )

    def _table_of(self, column_ref: Sequence[str]) -> str | None:
        """Which table a column reference belongs to."""
        if len(column_ref) >= 2:
            return resolve_table_reference(column_ref[-2], self.result.aliases, self.result.tables)
        if len(self.result.tables) == 1:
            return next(iter(self.result.tables))
        owners = [table for table, columns in self.columns_by_table.items() if column_ref[-1] in columns and table in self.result.tables]
        return owners[0] if len(owners) == 1 else None


def _operator_name(node: ast.A_Expr) -> str | None:
    """PostgreSQL's name for the comparison, or None for one no index serves."""
    name = "".join(str(part.sval) for part in (node.name or ()) if isinstance(part, ast.String))
    if node.kind == A_Expr_Kind.AEXPR_OP:
        return name or None
    if node.kind == A_Expr_Kind.AEXPR_OP_ANY:
        return f"{name} ANY"
    if node.kind == A_Expr_Kind.AEXPR_IN:
        return "NOT IN" if name == "<>" else "IN"
    if node.kind in (A_Expr_Kind.AEXPR_LIKE, A_Expr_Kind.AEXPR_ILIKE, A_Expr_Kind.AEXPR_SIMILAR):
        return name
    if node.kind in (A_Expr_Kind.AEXPR_BETWEEN, A_Expr_Kind.AEXPR_BETWEEN_SYM):
        return "BETWEEN"
    if node.kind in (A_Expr_Kind.AEXPR_NOT_BETWEEN, A_Expr_Kind.AEXPR_NOT_BETWEEN_SYM):
        return "NOT BETWEEN"
    if node.kind == A_Expr_Kind.AEXPR_DISTINCT:
        return "IS DISTINCT FROM"
    if node.kind == A_Expr_Kind.AEXPR_NOT_DISTINCT:
        return "IS NOT DISTINCT FROM"
    return None


_MIRROR = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}


def _mirror(operator: str) -> str:
    """The operator as seen from the other side of the comparison."""
    return _MIRROR.get(operator, operator)


def _column_refs(node: Any) -> Iterator[tuple[str, ...]]:
    """Every column reference under a node, as tuples of name parts."""
    if isinstance(node, ast.ColumnRef):
        parts = tuple(str(part.sval) for part in (node.fields or ()) if isinstance(part, ast.String))
        if parts:
            yield parts
        return
    if isinstance(node, ast.SubLink):
        # A subquery's columns are its own.
        return
    if isinstance(node, ast.Node):
        for attribute in node:
            yield from _column_refs(getattr(node, attribute))
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _column_refs(item)


def _string_literal(node: Any) -> str | None:
    if isinstance(node, ast.A_Const) and isinstance(node.val, ast.String):
        return str(node.val.sval)
    if isinstance(node, ast.TypeCast):
        return _string_literal(node.arg)
    return None


def normalise_expression(text: str) -> str:
    """Reduce an expression to a form two renderings of it agree on.

    The parser prints ``lower(u.email)`` and ``CAST(created_at AS date)``; the
    catalog prints ``lower(email)`` and ``(created_at)::date``. None of that
    changes which key the expression is, so all of it is removed: the CAST
    spelling becomes ``::``, then whitespace, parentheses, the ``pg_catalog.``
    the catalog adds to its own functions, a table or alias qualifier, and
    the ``::text`` a literal acquires all go. What is left is compared as
    lower case.
    """
    text = _rewrite_casts(text.strip().lower())
    text = _PG_CATALOG.sub("", text)
    text = _TEXT_LITERAL_CAST.sub(r"\1", text)
    text = _ALIAS_PREFIX.sub("", text)
    text = _WHITESPACE.sub("", text)
    return _PARENS.sub("", text)


def _rewrite_casts(text: str) -> str:
    """``cast(x as t)`` to ``x::t``, innermost first, so nesting survives."""
    while True:
        start = text.find("cast(")
        if start < 0 or (start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_")):
            return text
        depth = 0
        end = -1
        for i in range(start + 4, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            return text
        inner = text[start + 5 : end]
        split = inner.rfind(" as ")
        if split < 0:
            return text
        text = text[:start] + f"({inner[:split].strip()})::{inner[split + 4 :].strip()}" + text[end + 1 :]


__all__ = ["Predicate", "QueryPredicates", "normalise_expression", "read_predicates"]

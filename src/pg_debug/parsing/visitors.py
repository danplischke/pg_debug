"""pglast visitors that read the tables and columns a statement refers to."""

import logging
from typing import Any

from pglast.ast import A_Expr
from pglast.ast import ColumnRef
from pglast.ast import JoinExpr
from pglast.ast import Node
from pglast.ast import RangeVar
from pglast.ast import SelectStmt
from pglast.ast import SortBy
from pglast.ast import SortGroupClause
from pglast.visitors import Visitor

logger = logging.getLogger(__name__)


def resolve_table_reference(name: str, aliases: dict[str, str], tables: Any) -> str:
    """Resolve a table alias or bare name to the reference used in FROM.

    A query may qualify a column with the bare table name even when the FROM
    clause is schema-qualified (``FROM reporting.events WHERE events.id = 1``),
    so fall back to the unique schema-qualified table with that name.
    """
    if name in aliases:
        return aliases[name]
    if name in tables:
        return name

    matches = [table for table in tables if table.rpartition(".")[2] == name]
    return matches[0] if len(matches) == 1 else name


class TableAliasVisitor(Visitor):
    """Extracts table aliases and names from the SQL AST."""

    def __init__(self) -> None:
        super().__init__()  # Initialize the base Visitor class
        self.aliases: dict[str, str] = {}
        self.tables: set[str] = set()

    def __call__(self, node):
        super().__call__(node)
        return self.aliases, self.tables

    def visit_RangeVar(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """Visit table references, including those in FROM clause."""
        if isinstance(node, RangeVar):  # Type narrowing for RangeVar
            if node.relname is not None:
                # Keep the schema qualifier when the query gives one, so the
                # table can be resolved without relying on the search_path.
                name = f"{node.schemaname}.{node.relname}" if node.schemaname else str(node.relname)
                self.tables.add(name)
                if node.alias and node.alias.aliasname is not None:
                    self.aliases[node.alias.aliasname] = name

    def visit_JoinExpr(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """Visit both sides of JOIN expressions."""
        if isinstance(node, JoinExpr):  # Type narrowing for JoinExpr
            if node.larg is not None:
                self(node.larg)  # type: ignore
            if node.rarg is not None:
                self(node.rarg)  # type: ignore


class ColumnCollector(Visitor):
    """
    Collects columns used in WHERE, JOIN, ORDER BY, GROUP BY, HAVING, and SELECT clauses.
    With improved handling of column aliases.
    """

    def __init__(self) -> None:
        super().__init__()
        self.context_stack = []  # Stack of (tables, aliases) for each scope
        self.columns = {}  # Collected columns, keyed by table
        self.target_list = None
        self.inside_select = False
        self.column_aliases = {}  # Track column aliases and their definitions
        self.current_query_level = 0  # Track nesting level for subqueries

    def __call__(self, node):
        super().__call__(node)
        return self.columns

    def visit_SelectStmt(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """Visit a SelectStmt node and process its targetList for column aliases."""
        if isinstance(node, SelectStmt):
            self.inside_select = True
            self.current_query_level += 1
            query_level = self.current_query_level

            # Collect tables and aliases
            alias_visitor = TableAliasVisitor()
            if hasattr(node, "fromClause") and node.fromClause:
                for from_item in node.fromClause:
                    alias_visitor(from_item)
            scope_tables = alias_visitor.tables
            scope_aliases = alias_visitor.aliases

            # Push new context for this scope
            self.context_stack.append((scope_tables, scope_aliases))

            # First pass: collect column aliases from targetList
            if hasattr(node, "targetList") and node.targetList:
                self.target_list = node.targetList
                for target_entry in self.target_list:
                    if hasattr(target_entry, "name") and target_entry.name:
                        # This is a column alias
                        col_alias = target_entry.name
                        # Store the expression node for this alias
                        if hasattr(target_entry, "val"):
                            self.column_aliases[col_alias] = {
                                "node": target_entry.val,
                                "level": query_level,
                            }

            # Second pass: process the rest of the query
            self._process_query_clauses(node)

            # Pop context after processing
            self.context_stack.pop()
            self.inside_select = False
            self.current_query_level -= 1

    def _process_query_clauses(self, node):
        """Process various query clauses for column collection."""
        # Process targetList expressions
        if hasattr(node, "targetList") and node.targetList:
            self.target_list = node.targetList
            for target_entry in self.target_list:
                if hasattr(target_entry, "val"):
                    self(target_entry.val)

        # Handle GROUP BY clause
        if hasattr(node, "groupClause") and node.groupClause:
            for group_item in node.groupClause:
                if isinstance(group_item, SortGroupClause) and isinstance(group_item.tleSortGroupRef, int):
                    ref_index = group_item.tleSortGroupRef
                    if self.target_list and ref_index <= len(self.target_list):
                        target_entry = self.target_list[ref_index - 1]  # 1-based index
                        if hasattr(target_entry, "val"):
                            self(target_entry.val)
                        if hasattr(target_entry, "expr"):
                            self(target_entry.expr)  # Visit the expression

        # Process WHERE clause (including subqueries)
        if hasattr(node, "whereClause") and node.whereClause:
            self(node.whereClause)

        # Process FROM clause (may contain subqueries)
        if hasattr(node, "fromClause") and node.fromClause:
            for from_item in node.fromClause:
                self(from_item)

        # Process HAVING clause
        if hasattr(node, "havingClause") and node.havingClause:
            self(node.havingClause)

        # Handle ORDER BY clause
        if hasattr(node, "sortClause") and node.sortClause:
            for sort_item in node.sortClause:
                self._process_sort_item(sort_item)

    def _process_sort_item(self, sort_item):
        """Process a sort item, resolving column aliases if needed."""
        if not hasattr(sort_item, "node"):
            return

        # If it's a simple column reference, it might be an alias
        if isinstance(sort_item.node, ColumnRef) and hasattr(sort_item.node, "fields") and sort_item.node.fields:
            fields = [f.sval for f in sort_item.node.fields if hasattr(f, "sval")]
            if len(fields) == 1:
                col_name = fields[0]
                # Check if this is a known alias
                if col_name in self.column_aliases:
                    # Process the original expression instead
                    alias_info = self.column_aliases[col_name]
                    if alias_info["level"] == self.current_query_level:
                        self(alias_info["node"])
                        return

        # Regular processing for non-alias sort items
        self(sort_item.node)

    def visit_ColumnRef(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """Visit a ColumnRef node and collect column names, skipping aliases."""
        if isinstance(node, ColumnRef) and self.inside_select:
            if not hasattr(node, "fields") or not node.fields:
                return

            fields = [f.sval if hasattr(f, "sval") else "*" for f in node.fields]

            # Skip collecting if this is a reference to a column alias
            if len(fields) == 1 and (fields[0] == "*" or fields[0] in self.column_aliases):
                return

            if len(fields) == 2 and fields[1] == "*":
                return

            # Use current scope's tables and aliases
            current_tables, current_aliases = self.context_stack[-1] if self.context_stack else ({}, {})

            if len(fields) == 2:  # Qualified column (e.g., u.name)
                table_or_alias, column = fields
                table = resolve_table_reference(table_or_alias, current_aliases, current_tables)
                if table not in self.columns:
                    self.columns[table] = set()
                self.columns[table].add(column)
            elif len(fields) == 1:  # Unqualified column
                column = fields[0]
                if len(current_tables) == 1:  # Only one table in scope
                    table = next(iter(current_tables))
                    if table not in self.columns:
                        self.columns[table] = set()
                    self.columns[table].add(column)
                else:
                    # Try to find which table this column belongs to
                    for table in current_tables:
                        if self._column_exists(table, column):
                            if table not in self.columns:
                                self.columns[table] = set()
                            self.columns[table].add(column)
                            break

    def _column_exists(self, table: str, column: str) -> bool:
        """Check if column exists in table."""
        # This is a placeholder. We'd query the schema
        # Ideally this would be cached to avoid repeated queries
        return True  # Default to True for the sake of collecting all possibilities

    def visit_A_Expr(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """
        Visit an A_Expr node (arithmetic or comparison expression).
        """
        if isinstance(node, A_Expr) and self.inside_select:
            # Process left expression
            if hasattr(node, "lexpr") and node.lexpr:
                self(node.lexpr)
                if isinstance(node.lexpr, SelectStmt):
                    alias_visitor = TableAliasVisitor()
                    alias_visitor(node.lexpr)
                    self.context_stack.append((alias_visitor.tables, alias_visitor.aliases))
                    self(node.lexpr)
                    self.context_stack.pop()

            # Process right expression
            if hasattr(node, "rexpr") and node.rexpr:
                if isinstance(node.rexpr, SelectStmt):
                    alias_visitor = TableAliasVisitor()
                    alias_visitor(node.rexpr)
                    self.context_stack.append((alias_visitor.tables, alias_visitor.aliases))
                    self(node.rexpr)
                    self.context_stack.pop()
                else:
                    self(node.rexpr)

            # Special handling for IN clauses with subqueries
            if hasattr(node, "kind") and node.kind == 0:  # 0 is the kind for IN operator
                if hasattr(node, "rexpr") and node.rexpr and isinstance(node.rexpr, SelectStmt):
                    # Process the subquery in the IN clause
                    alias_visitor = TableAliasVisitor()
                    alias_visitor(node.rexpr)
                    self.context_stack.append((alias_visitor.tables, alias_visitor.aliases))
                    self(node.rexpr)
                    self.context_stack.pop()

    def visit_JoinExpr(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """
        Visit a JoinExpr node to handle JOIN conditions.
        """
        if isinstance(node, JoinExpr) and self.inside_select:  # Type narrowing for JoinExpr
            if hasattr(node, "larg") and node.larg:
                self(node.larg)
            if hasattr(node, "rarg") and node.rarg:
                self(node.rarg)
            if hasattr(node, "quals") and node.quals:
                self(node.quals)

    def visit_SortBy(self, ancestors: list[Node], node: Node) -> None:  # noqa: N802
        """
        Visit a SortBy node (ORDER BY expression).
        """
        if isinstance(node, SortBy) and self.inside_select:  # Type narrowing for SortBy
            if hasattr(node, "node") and node.node:
                self(node.node)

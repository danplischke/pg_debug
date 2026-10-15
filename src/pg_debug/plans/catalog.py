"""What the catalog says about the objects a plan touches, beyond its statistics.

The plan shows a function called in a filter, a predicate that did not become
an index condition, a trigger that took most of the time. Whether that
function is STABLE, whether a policy put it there, whether an index exists
that the predicate could not use and whether the foreign key behind a trigger
has one: those are catalog facts, read here once for the whole plan and
handed to the rules in their :class:`~pg_debug.plans.rules.RuleContext`.

Everything here reads the catalog as the connecting user, never in the query
context: the context's role may not be allowed to read ``pg_policy``, and
what is wanted is the database's definition of the objects, not one role's
view of them.

The text-reading half — which calls in a policy's expression could be made
once, and the expression rewritten to make them so — is pure, so it is
tested without a database.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from typing import LiteralString

from pglast import ast
from pglast import enums
from pglast import parse_sql
from pglast.parser import ParseError
from pglast.stream import RawStream

from ..db import SafeSqlDriver

if TYPE_CHECKING:
    from ..db import SqlDriver

logger = logging.getLogger(__name__)

_VOLATILITY = {"i": "immutable", "s": "stable", "v": "volatile"}

#: A function's name, possibly schema-qualified and quoted, followed by its
#: opening parenthesis, in a condition PostgreSQL rendered.
_CALL_RE = re.compile(r'(?<![\w$."])(?:("?)([A-Za-z_][\w$]*)\1\.)?("?)([A-Za-z_][\w$]*)\3\s*\(')

#: Words that are followed by a parenthesis in a rendered condition and are
#: not functions pg_proc holds.
_NOT_FUNCTIONS = frozenset(
    {"any", "all", "array", "row", "nullif", "coalesce", "greatest", "least", "exists", "and", "or", "not", "in", "case", "cast", "values"}
)

#: An operator as EXPLAIN renders one: symbols, with a space either side.
_OPERATOR_RE = re.compile(r"(?<=\s)([~!@#%^&|`?<>=+\-*/]+)(?=\s)")

_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def mask_literals(text: str) -> str:
    """The text with every string literal's content blanked, at the same length.

    A literal can hold anything, parentheses and names included, and nothing
    in it is a call or a column. Blanking rather than removing keeps every
    position, so what is found in the masked text can be cut from the original.
    """
    return _LITERAL_RE.sub(lambda m: "'" + " " * (len(m.group(0)) - 2) + "'", text)


@dataclass(frozen=True)
class Call:
    """A function call found in a rendered condition."""

    schema: str | None
    name: str
    #: The arguments as the condition rendered them, without the parentheses.
    arguments: str
    #: The whole call, ``lower((users.email)::text)``.
    text: str


def calls_in(text: str) -> list[Call]:
    """Every function call in a rendered condition, outermost first."""
    masked = mask_literals(text)
    found: list[Call] = []
    for match in _CALL_RE.finditer(masked):
        name = match.group(4)
        if name.lower() in _NOT_FUNCTIONS and not match.group(2):
            continue
        start, depth = match.end(), 1
        end = start
        while end < len(masked) and depth:
            if masked[end] == "(":
                depth += 1
            elif masked[end] == ")":
                depth -= 1
            end += 1
        if depth:
            continue
        found.append(Call(schema=match.group(2), name=name, arguments=text[start : end - 1], text=text[match.start() : end]))
    return found


def operators_in(text: str) -> list[str]:
    """The operator symbols in a rendered condition, each once."""
    seen: list[str] = []
    for symbol in _OPERATOR_RE.findall(mask_literals(text)):
        if symbol not in seen:
            seen.append(symbol)
    return seen


def conjuncts(text: str) -> list[str]:
    """A rendered condition split at its top-level ANDs.

    ``((NOT t.done) AND (t.user_id = auth.uid()))`` is two conjuncts. A
    condition with no top-level AND is one.
    """
    masked = mask_literals(text).strip()
    original = text.strip()
    while masked.startswith("(") and _closing(masked, 0) == len(masked) - 1:
        masked, original = masked[1:-1].strip(), original[1:-1].strip()
    parts: list[str] = []
    depth, start = 0, 0
    i = 0
    while i < len(masked):
        char = masked[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and masked.startswith(" AND ", i):
            parts.append(original[start:i].strip())
            start = i + len(" AND ")
            i = start
            continue
        i += 1
    parts.append(original[start:].strip())
    return [part for part in parts if part]


def _closing(text: str, opening: int) -> int:
    """The index of the parenthesis closing the one at ``opening``, or -1."""
    depth = 0
    for i in range(opening, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


_QUALIFIER_RE = re.compile(r'(?:"[^"]+"|\b[A-Za-z_][\w$]*)\.(?=["A-Za-z_])')


def normalized(text: str) -> str:
    """A condition reduced for comparing a policy's expression with a plan's.

    The plan qualifies columns with the alias (``todos.user_id``) where
    ``pg_get_expr`` does not, and parenthesises differently. Dropping every
    qualifier, parenthesis and space leaves what both agree on. Two different
    conditions can only meet here if they differ in nothing but those.
    """
    return re.sub(r"[()\s]", "", _QUALIFIER_RE.sub("", mask_literals(text))).lower()


# -- Functions and operators ----------------------------------------------


@dataclass(frozen=True)
class FunctionFacts:
    """One function, as ``pg_proc`` defines it."""

    schema: str
    name: str
    volatility: str
    leakproof: bool
    language: str
    #: Whether the planner can inline it into the expression that calls it:
    #: a plain SQL function, not SECURITY DEFINER, with no SET clause, not
    #: returning a set. Inlined, its body appears in the plan instead of it.
    inlinable: bool
    #: The functions its body calls, by name, when it is inlinable.
    body_calls: tuple[str, ...] = ()

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass(frozen=True)
class NameFacts:
    """What every function or operator going by one name has in common.

    A rendered condition names a function or an operator without its argument
    types, so it cannot be told apart from its overloads. A property all of
    them share is known; one they disagree on is None.
    """

    name: str
    leakproof: bool | None
    immutable: bool | None


FUNCTIONS_SQL = """
SELECT
    n.nspname AS schema,
    p.proname AS name,
    p.provolatile AS volatility,
    p.proleakproof AS leakproof,
    l.lanname AS language,
    p.prosecdef AS security_definer,
    p.proconfig IS NOT NULL AS has_config,
    p.proretset AS returns_set,
    CASE WHEN l.lanname = 'sql' THEN p.prosrc END AS source
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_language l ON l.oid = p.prolang
WHERE p.proname = ANY({}::text[])
"""

OPERATORS_SQL = """
SELECT o.oprname AS name, bool_and(p.proleakproof) AS all_leakproof, bool_or(p.proleakproof) AS any_leakproof,
       bool_and(p.provolatile = 'i') AS all_immutable, bool_or(p.provolatile = 'i') AS any_immutable
FROM pg_operator o
JOIN pg_proc p ON p.oid = o.oprcode
WHERE o.oprname = ANY({}::text[])
GROUP BY o.oprname
"""


def _body_calls(source: str | None) -> tuple[str, ...]:
    """The functions an SQL function's body calls, by lower-case name."""
    if not source:
        return ()
    try:
        statements = parse_sql(source)
    except ParseError:
        return ()
    names: list[str] = []
    for statement in statements:
        for node in _walk(statement):
            if isinstance(node, ast.FuncCall) and node.funcname:
                name = str(getattr(node.funcname[-1], "sval", "")).lower()
                if name and name not in names:
                    names.append(name)
    return tuple(names)


def function_facts(row: dict[str, Any]) -> FunctionFacts:
    """One row of :data:`FUNCTIONS_SQL`, read."""
    language = str(row.get("language") or "")
    inlinable = language == "sql" and not row.get("security_definer") and not row.get("has_config") and not row.get("returns_set")
    return FunctionFacts(
        schema=str(row["schema"]),
        name=str(row["name"]),
        volatility=_VOLATILITY.get(str(row.get("volatility")), "volatile"),
        leakproof=bool(row.get("leakproof")),
        language=language,
        inlinable=inlinable,
        body_calls=_body_calls(row.get("source")) if inlinable else (),
    )


def name_facts(functions: Iterable[FunctionFacts]) -> dict[str, NameFacts]:
    """What the functions sharing each name agree on, by lower-case name."""
    grouped: dict[str, list[FunctionFacts]] = {}
    for function in functions:
        grouped.setdefault(function.name.lower(), []).append(function)
    return {
        name: NameFacts(
            name=name,
            leakproof=_agreed([f.leakproof for f in group]),
            immutable=_agreed([f.volatility == "immutable" for f in group]),
        )
        for name, group in grouped.items()
    }


def _agreed(values: Sequence[bool]) -> bool | None:
    return values[0] if values and all(v == values[0] for v in values) else None


# -- Row-level security ---------------------------------------------------


@dataclass(frozen=True)
class PolicyCall:
    """A call in a policy's expression that could be made once per statement.

    STABLE, so its result cannot change within the statement; outside any
    subquery, so nothing already makes it once; and with no column among its
    arguments, so the result does not depend on the row.
    """

    function: FunctionFacts
    #: The call as the policy's expression renders it, ``auth.uid()``.
    text: str


@dataclass(frozen=True)
class Policy:
    """One row-level security policy and what could be made once in it."""

    name: str
    command: str
    permissive: bool
    roles: tuple[str, ...]
    qual: str | None
    with_check: str | None
    #: Calls in ``qual`` and ``with_check`` that could be made once.
    calls: tuple[PolicyCall, ...] = ()
    #: ``qual`` and ``with_check`` with those calls wrapped as ``(SELECT …)``,
    #: or None where there was nothing to wrap.
    rewritten_qual: str | None = None
    rewritten_check: str | None = None


@dataclass(frozen=True)
class RelationSecurity:
    """A table's row-level security: whether it is on, and its policies."""

    relation: str
    enabled: bool
    forced: bool
    policies: tuple[Policy, ...] = ()


POLICIES_SQL = """
SELECT
    n.nspname AS schema,
    c.relname AS table,
    c.relrowsecurity AS enabled,
    c.relforcerowsecurity AS forced,
    p.polname AS policy,
    p.polcmd AS command,
    p.polpermissive AS permissive,
    ARRAY(
        SELECT CASE WHEN r.role = 0 THEN 'public' ELSE pg_get_userbyid(r.role) END
        FROM unnest(p.polroles) AS r(role)
    ) AS roles,
    pg_get_expr(p.polqual, p.polrelid) AS qual,
    pg_get_expr(p.polwithcheck, p.polrelid) AS with_check
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_policy p ON p.polrelid = c.oid
WHERE c.relrowsecurity
  AND (n.nspname || '.' || c.relname) = ANY({}::text[])
ORDER BY n.nspname, c.relname, p.polname
"""

_COMMANDS = {"r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE", "*": "ALL"}


def policy_function_names(rows: Iterable[dict[str, Any]]) -> list[str]:
    """The names of every function the policies' expressions call."""
    names: list[str] = []
    for row in rows:
        for text in (row.get("qual"), row.get("with_check")):
            for call in calls_in(text or ""):
                if call.name not in names:
                    names.append(call.name)
    return names


def relation_security(rows: Iterable[dict[str, Any]], functions: Sequence[FunctionFacts]) -> dict[str, RelationSecurity]:
    """The rows of :data:`POLICIES_SQL`, read into one entry per table."""
    tables: dict[str, tuple[dict[str, Any], list[Policy]]] = {}
    for row in rows:
        relation = f"{row['schema']}.{row['table']}"
        _, policies = tables.setdefault(relation, (row, []))
        if row.get("policy") is None:
            continue
        qual, check = row.get("qual"), row.get("with_check")
        qual_calls, rewritten_qual = hoist_calls(qual, functions)
        check_calls, rewritten_check = hoist_calls(check, functions)
        calls = list(qual_calls)
        calls.extend(call for call in check_calls if call.text not in {c.text for c in calls})
        policies.append(
            Policy(
                name=str(row["policy"]),
                command=_COMMANDS.get(str(row.get("command")), str(row.get("command"))),
                permissive=bool(row.get("permissive", True)),
                roles=tuple(str(role) for role in row.get("roles") or ()),
                qual=qual,
                with_check=check,
                calls=tuple(calls),
                rewritten_qual=rewritten_qual,
                rewritten_check=rewritten_check,
            )
        )
    return {
        relation: RelationSecurity(relation=relation, enabled=bool(first.get("enabled")), forced=bool(first.get("forced")), policies=tuple(policies))
        for relation, (first, policies) in tables.items()
    }


def _walk(node: Any) -> Iterable[Any]:
    """Every pglast node under ``node``, ``node`` included."""
    if isinstance(node, ast.Node):
        yield node
        for attribute in node.__slots__:
            yield from _walk(getattr(node, attribute, None))
    elif isinstance(node, (tuple, list)):
        for item in node:
            yield from _walk(item)


def _resolve(call: ast.FuncCall, functions: Sequence[FunctionFacts]) -> FunctionFacts | None:
    """The function a call names, when the name leaves no doubt about its volatility.

    A qualified name is matched exactly. An unqualified one is matched by
    name, and is resolved only when every function of that name has the same
    volatility: the rendered expression does not carry argument types.
    """
    parts = [str(getattr(part, "sval", "")) for part in call.funcname or ()]
    if not parts:
        return None
    name = parts[-1]
    if len(parts) > 1:
        return next((f for f in functions if f.schema == parts[-2] and f.name == name), None)
    candidates = [f for f in functions if f.name == name]
    if not candidates or any(f.volatility != candidates[0].volatility for f in candidates):
        return None
    return candidates[0]


def _mentions_column(node: Any) -> bool:
    return any(isinstance(inner, ast.ColumnRef) for inner in _walk(node))


def _wrapped(call: ast.FuncCall) -> ast.SubLink:
    """``call`` as ``(SELECT call)``, which PostgreSQL runs once, as an InitPlan."""
    select = ast.SelectStmt(
        targetList=(ast.ResTarget(val=call),),
        op=enums.SetOperation.SETOP_NONE,
        limitOption=enums.LimitOption.LIMIT_OPTION_DEFAULT,
    )
    return ast.SubLink(subLinkType=enums.SubLinkType.EXPR_SUBLINK, subselect=select)


def hoist_calls(expression: str | None, functions: Sequence[FunctionFacts]) -> tuple[list[PolicyCall], str | None]:
    """The calls in a policy expression that could be made once, and the expression rewritten so they are.

    Returns:
        The calls, and the expression with each wrapped as ``(SELECT …)``;
        an empty list and None when there is nothing to wrap, or the
        expression could not be parsed.
    """
    if not expression:
        return [], None
    try:
        statement = parse_sql(f"SELECT 1 WHERE {expression}")[0].stmt
    except (ParseError, IndexError):
        logger.debug("Could not parse the policy expression %r", expression)
        return [], None
    if not isinstance(statement, ast.SelectStmt) or statement.whereClause is None:
        return [], None

    found: list[PolicyCall] = []

    def rewrite(node: Any) -> Any:
        if isinstance(node, ast.SubLink):
            # Already a subquery: whatever it calls is made once, or is
            # correlated with the row on purpose.
            return node
        if isinstance(node, ast.FuncCall):
            function = _resolve(node, functions)
            if function is not None and function.volatility == "stable" and not _mentions_column(node.args) and not node.agg_star:
                text = RawStream()(node)
                if all(call.text != text for call in found):
                    found.append(PolicyCall(function=function, text=text))
                return _wrapped(node)
        if isinstance(node, ast.Node):
            for attribute in node.__slots__:
                value = getattr(node, attribute, None)
                if isinstance(value, ast.Node):
                    setattr(node, attribute, rewrite(value))
                elif isinstance(value, tuple):
                    setattr(node, attribute, tuple(rewrite(item) for item in value))
        return node

    rewritten = rewrite(statement.whereClause)
    if not found:
        return [], None
    return found, RawStream()(rewritten)


# -- Indexes and foreign keys ---------------------------------------------


@dataclass(frozen=True)
class IndexFacts:
    """One valid index: its name, and its key columns or expressions in order."""

    name: str
    keys: tuple[str, ...]
    method: str
    partial: bool = False


INDEXES_SQL = """
SELECT
    n.nspname AS schema,
    c.relname AS table,
    i.relname AS index,
    am.amname AS method,
    x.indpred IS NOT NULL AS partial,
    ARRAY(
        SELECT pg_get_indexdef(x.indexrelid, k.position::int, true)
        FROM unnest(x.indkey::int2[]) WITH ORDINALITY AS k(attnum, position)
        WHERE k.position <= x.indnkeyatts
        ORDER BY k.position
    ) AS keys
FROM pg_index x
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_class c ON c.oid = x.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_am am ON am.oid = i.relam
WHERE x.indisvalid
  AND (n.nspname || '.' || c.relname) = ANY({}::text[])
ORDER BY i.relname
"""


@dataclass(frozen=True)
class ForeignKeyFacts:
    """The foreign key behind a referential-integrity trigger.

    Attributes:
        constraint: Its name, as the plan's Triggers section gives it.
        referencing: The table holding the foreign key, schema-qualified.
        referenced: The table it points at, schema-qualified.
        columns: The referencing columns, in the key's order.
        indexed: Whether a valid, non-partial index on the referencing table
            leads with exactly those columns. Without one, every change to a
            referenced row scans the referencing table to find its rows.
    """

    constraint: str
    referencing: str
    referenced: str
    columns: tuple[str, ...]
    indexed: bool


FOREIGN_KEYS_SQL = """
SELECT
    con.conname AS constraint,
    rn.nspname || '.' || r.relname AS referencing,
    fn.nspname || '.' || f.relname AS referenced,
    ARRAY(
        SELECT a.attname
        FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, position)
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
        ORDER BY k.position
    ) AS columns,
    EXISTS (
        SELECT 1
        FROM pg_index x
        WHERE x.indrelid = con.conrelid
          AND x.indisvalid
          AND x.indpred IS NULL
          AND (x.indkey::int2[])[0:cardinality(con.conkey) - 1] @> con.conkey
          AND (x.indkey::int2[])[0:cardinality(con.conkey) - 1] <@ con.conkey
    ) AS indexed
FROM pg_constraint con
JOIN pg_class r ON r.oid = con.conrelid
JOIN pg_namespace rn ON rn.oid = r.relnamespace
JOIN pg_class f ON f.oid = con.confrelid
JOIN pg_namespace fn ON fn.oid = f.relnamespace
WHERE con.contype = 'f'
  AND con.conname = ANY({}::text[])
"""


# -- Reading it -----------------------------------------------------------


@dataclass
class CatalogFacts:
    """Everything this module reads, for one plan."""

    security: dict[str, RelationSecurity] = field(default_factory=dict)
    functions: dict[str, NameFacts] = field(default_factory=dict)
    operators: dict[str, NameFacts] = field(default_factory=dict)
    function_details: list[FunctionFacts] = field(default_factory=list)
    indexes: dict[str, list[IndexFacts]] = field(default_factory=dict)
    foreign_keys: dict[str, list[ForeignKeyFacts]] = field(default_factory=dict)


async def _rows(driver: SqlDriver | SafeSqlDriver, query: LiteralString, values: Sequence[str]) -> list[dict[str, Any]]:
    if not values:
        return []
    rows = await SafeSqlDriver.execute_param_query(driver, query, [list(values)])  # type: ignore[arg-type]
    return [row.cells for row in rows] if rows else []


async def read_catalog(
    driver: SqlDriver | SafeSqlDriver,
    relations: Sequence[str],
    conditions: Sequence[str],
    constraints: Sequence[str],
) -> CatalogFacts:
    """Read the catalog facts for a plan's relations, conditions and triggers.

    Args:
        driver: Reads as the connecting user. Restricted mode allows all of it.
        relations: The plan's relations, schema-qualified.
        conditions: Every rendered condition in the plan, which is where the
            function and operator names come from.
        constraints: The constraint names its referential-integrity triggers
            give.
    """
    facts = CatalogFacts()

    policy_rows = await _rows(driver, POLICIES_SQL, relations)
    names: list[str] = policy_function_names(policy_rows)
    operators: list[str] = []
    for text in conditions:
        for call in calls_in(text):
            if call.name not in names:
                names.append(call.name)
        for symbol in operators_in(text):
            if symbol not in operators:
                operators.append(symbol)

    facts.function_details = [function_facts(row) for row in await _rows(driver, FUNCTIONS_SQL, names)]
    # A plan renders an unquoted name in lower case already; one that
    # needed quotes is matched as written.
    facts.functions = name_facts(facts.function_details)
    facts.security = relation_security(policy_rows, facts.function_details)

    for row in await _rows(driver, OPERATORS_SQL, operators):
        facts.operators[str(row["name"])] = NameFacts(
            name=str(row["name"]),
            leakproof=bool(row["all_leakproof"]) if row.get("all_leakproof") == row.get("any_leakproof") else None,
            immutable=bool(row["all_immutable"]) if row.get("all_immutable") == row.get("any_immutable") else None,
        )

    for row in await _rows(driver, INDEXES_SQL, relations):
        facts.indexes.setdefault(f"{row['schema']}.{row['table']}", []).append(
            IndexFacts(
                name=str(row["index"]),
                keys=tuple(str(k) for k in row.get("keys") or ()),
                method=str(row["method"]),
                partial=bool(row["partial"]),
            )
        )

    for row in await _rows(driver, FOREIGN_KEYS_SQL, constraints):
        facts.foreign_keys.setdefault(str(row["constraint"]), []).append(
            ForeignKeyFacts(
                constraint=str(row["constraint"]),
                referencing=str(row["referencing"]),
                referenced=str(row["referenced"]),
                columns=tuple(str(c) for c in row.get("columns") or ()),
                indexed=bool(row["indexed"]),
            )
        )
    return facts


__all__ = [
    "FOREIGN_KEYS_SQL",
    "FUNCTIONS_SQL",
    "INDEXES_SQL",
    "OPERATORS_SQL",
    "POLICIES_SQL",
    "Call",
    "CatalogFacts",
    "ForeignKeyFacts",
    "FunctionFacts",
    "IndexFacts",
    "NameFacts",
    "Policy",
    "PolicyCall",
    "RelationSecurity",
    "calls_in",
    "conjuncts",
    "function_facts",
    "hoist_calls",
    "mask_literals",
    "name_facts",
    "normalized",
    "operators_in",
    "read_catalog",
    "relation_security",
]

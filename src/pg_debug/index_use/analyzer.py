"""Why the planner did not use an index for a query.

The question has two halves, and they need different evidence. *Can* the
planner use the index: that is settled by the query's text against the
index's definition — which column each predicate restricts, with what
operator, bare or inside an expression — and by the catalog, which says
whether the index is valid and what it is on. *Did it choose to*: that is
settled by asking the planner the same question with sequential scans
disabled. If it then uses the index, nothing stopped it; it judged the other
plan cheaper, and the plan's row estimate says why. If it still does not, the
reason is structural, and the first half has usually already named it.

Everything here is read from the statement, the catalog and plans. Nothing is
executed: ``EXPLAIN`` without ``ANALYZE`` plans a query and does not run it.
The one thing restricted mode cannot do is the forced plan, which needs a
``SET`` for the session, and the verdict says so rather than guessing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any
from typing import LiteralString
from typing import cast

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..errors import ExplainPlanError
from ..errors import InvalidArgumentError
from ..model.result import Confidence
from ..plans.explain import ExplainPlanTool
from ..plans.metrics import conditions
from ..plans.metrics import walk
from ..plans.tree import ErrorResult
from ..plans.tree import ExplainPlanArtifact
from ..plans.tree import PlanNode
from ..statistics import StatisticsInspector
from .catalog import COLUMNS_FOR_TABLES_SQL
from .catalog import DATABASE_COLLATION_SQL
from .catalog import DISABLE_SEQSCAN_SQL
from .catalog import INDEXES_FOR_TABLES_SQL
from .catalog import RESET_SEQSCAN_SQL
from .catalog import predicate_as_statement
from .predicates import Predicate
from .predicates import QueryPredicates
from .predicates import normalise_expression
from .predicates import read_predicates
from .result import IndexUse
from .result import IndexUseFinding
from .result import IndexUseReason

logger = logging.getLogger(__name__)

#: A table this small is read whole faster than through any index.
TINY_TABLE_PAGES = 8

#: Below this share of the table, an index is normally the cheaper way in.
INDEX_WORTHWHILE_SHARE = 0.05

_IDENTIFIER = re.compile(r'^(?:"[^"]+"|[a-z_][a-z0-9_$]*)$', re.IGNORECASE)
_WORDS = re.compile(r"[a-z_][a-z0-9_$]*", re.IGNORECASE)

#: Operators that exclude rather than select. A btree finds what equals or
#: lies between; it cannot be walked for "everything except".
_EXCLUSIONS = frozenset({"<>", "!=", "NOT IN", "NOT BETWEEN", "IS DISTINCT FROM", "!~~", "!~~*", "!~", "!~*"})

#: Case-insensitive and regular-expression matches, which a btree cannot do
#: and a trigram index can.
_NEEDS_TRIGRAM = frozenset({"~~*", "~", "~*"})

#: Reasons that rest on the planner's row estimate, which stale statistics
#: can falsify; the structural ones hold whatever the estimate says.
_RESTS_ON_ESTIMATE = frozenset({IndexUseReason.PLANNER_PREFERRED_OTHER, IndexUseReason.UNEXPLAINED})

#: The collations PostgreSQL treats as byte order for LIKE. ``C.UTF-8`` is
#: not one of them through libc: only these two names are recognised.
_COLLATIONS_WITH_BYTE_ORDER = frozenset({"C", "POSIX"})


class IndexUseAnalyzer:
    """Says whether a query's plan uses each index on the tables it reads, and why not."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def analyze(self, sql: str, index: str | None = None) -> list[IndexUse]:
        """Report each index on the query's tables: used, or why not.

        Args:
            sql: One SELECT, UPDATE or DELETE. Bind parameters (``$1``) are
                planned generically or given sample values, as ``explain``
                does.
            index: Restrict the report to one index, by name.

        Returns:
            One :class:`IndexUse` per index, the ones not used first. An index
            that is used carries no findings. One that is not carries the
            reasons, each marked proven or candidate.

        Raises:
            InvalidArgumentError: If the SQL does not parse, reads no table
                with an index, or names an index that is not on its tables.
            ExplainPlanError: If the query could not be planned.
        """
        parsed = read_predicates(sql)
        if not parsed.tables:
            raise InvalidArgumentError("The query reads no table, so there is no index to use.")

        qualified = sorted(t for t in parsed.tables if "." in t)
        bare = sorted(t for t in parsed.tables if "." not in t)
        index_rows = await self._rows(INDEXES_FOR_TABLES_SQL, [qualified, bare])
        column_rows = await self._rows(COLUMNS_FOR_TABLES_SQL, [qualified, bare])

        resolved = _resolve_tables(parsed.tables, column_rows)
        columns_by_ref = {ref: {str(r["column"]) for r in column_rows if f"{r['schema']}.{r['table']}" == name} for ref, name in resolved.items()}
        column_types = {(f"{r['schema']}.{r['table']}", str(r["column"])): str(r["type"]) for r in column_rows}
        parsed = read_predicates(sql, columns_by_ref)

        if index is not None:
            wanted = index.rpartition(".")[2]
            index_rows = [r for r in index_rows if r["index"] == wanted]
            if not index_rows:
                tables = ", ".join(sorted(resolved.values())) or ", ".join(sorted(parsed.tables))
                raise InvalidArgumentError(f"No index named {index} on the tables this query reads ({tables}).")
        if not index_rows:
            raise InvalidArgumentError(
                f"The tables this query reads have no indexes: {', '.join(sorted(resolved.values()) or sorted(parsed.tables))}."
            )

        plan = await self._plan(self.sql_driver, sql)
        used_by = {node.index_name: node.node_type for node in walk(plan.plan_tree) if node.index_name}

        stale = await self._stale_tables()
        collation = await self._collation() if any(p.operator == "~~" for p in parsed.predicates) else None
        forced: ExplainPlanArtifact | None = None
        forced_note: str | None = None
        if any(r["index"] not in used_by for r in index_rows):
            forced, forced_note = await self._forced_plan(sql)

        verdicts: list[IndexUse] = []
        for row in index_rows:
            table = f"{row['schema']}.{row['table']}"
            refs = [ref for ref, name in resolved.items() if name == table]
            predicates = [p for ref in refs for p in parsed.on(ref)]
            verdict = _verdict(row, table)
            if row["index"] in used_by:
                verdict.used = True
                verdict.node_type = used_by[row["index"]]
                verdicts.append(verdict)
                continue

            findings = _structural_findings(verdict, row, parsed, predicates, plan.plan_tree, column_types, collation)
            forced_node = _node_using(forced.plan_tree, row["index"]) if forced is not None else None
            if forced is not None and forced_node is not None and not any(f.reason is IndexUseReason.INVALID for f in findings):
                # The planner can use it. Whatever the text comparison
                # thought, the planner's own answer wins: the reason is cost.
                findings = [_planner_preferred(verdict, row, plan, forced, forced_node)]
            elif forced is not None and not findings:
                findings = [_other_index_or_unexplained(verdict, row, plan, forced)]
            if table in stale and any(f.reason in _RESTS_ON_ESTIMATE for f in findings):
                findings.append(_stale(table, stale[table]))
            if forced_note:
                verdict.notes.append(forced_note)
            verdict.findings = findings
            verdicts.append(verdict)

        verdicts.sort(key=lambda v: (v.used, v.table, v.index))
        return verdicts

    async def _rows(self, query: LiteralString, params: list[Any]) -> list[dict[str, Any]]:
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query, params)
        return [row.cells for row in rows] if rows else []

    async def _plan(self, driver: SqlDriver | SafeSqlDriver, sql: str) -> ExplainPlanArtifact:
        result = await ExplainPlanTool(cast(SqlDriver, driver)).explain(sql)
        if isinstance(result, ErrorResult):
            raise ExplainPlanError(f"Could not plan the query: {result.value}")
        return result

    async def _forced_plan(self, sql: str) -> tuple[ExplainPlanArtifact | None, str | None]:
        """The plan with sequential scans disabled, or why there is none."""
        if isinstance(self.sql_driver, SafeSqlDriver):
            return None, (
                "Whether the planner *could* have used the index was not tested: that takes a SET for the session, "
                "which restricted mode does not allow. Run in unrestricted mode to see the plan with sequential scans disabled."
            )
        try:
            async with self.sql_driver.session() as pinned:
                try:
                    await pinned.execute_query(cast(LiteralString, DISABLE_SEQSCAN_SQL))
                    return await self._plan(pinned, sql), None
                finally:
                    await pinned.execute_query(cast(LiteralString, RESET_SEQSCAN_SQL))
        except Exception as e:
            logger.debug("Could not plan with sequential scans disabled: %s", e)
            return None, f"The plan with sequential scans disabled could not be taken: {e}"

    async def _stale_tables(self) -> dict[str, dict[str, Any]]:
        try:
            rows = await StatisticsInspector(self.sql_driver).stale_tables()
        except Exception as e:
            logger.debug("Could not read statistics freshness: %s", e)
            return {}
        return {f"{row['schema']}.{row['table']}": row for row in rows}

    async def _collation(self) -> str | None:
        try:
            rows = await self.sql_driver.execute_query(cast(LiteralString, DATABASE_COLLATION_SQL))
        except Exception as e:
            logger.debug("Could not read the database collation: %s", e)
            return None
        return str(rows[0].cells["collation"]) if rows else None


# -- Assembling a verdict ---------------------------------------------------


def _resolve_tables(refs: set[str], column_rows: Sequence[dict[str, Any]]) -> dict[str, str]:
    """Query table reference to ``schema.table``, from what the catalog returned."""
    present = {f"{r['schema']}.{r['table']}" for r in column_rows}
    by_name: dict[str, set[str]] = {}
    for name in present:
        by_name.setdefault(name.rpartition(".")[2], set()).add(name)

    resolved: dict[str, str] = {}
    for ref in refs:
        if "." in ref and ref in present:
            resolved[ref] = ref
        elif "." not in ref and len(by_name.get(ref, ())) == 1:
            resolved[ref] = next(iter(by_name[ref]))
    return resolved


def _verdict(row: dict[str, Any], table: str) -> IndexUse:
    return IndexUse(
        index=str(row["index"]),
        table=table,
        definition=str(row["definition"]),
        method=str(row["method"]),
        columns=[str(c) for c in row.get("columns") or []],
        key_columns=int(row.get("key_columns") or len(row.get("columns") or [])),
        predicate=row.get("predicate"),
        valid=bool(row.get("valid")) and bool(row.get("ready", True)),
        size_bytes=int(row.get("size_bytes") or 0),
        used=False,
    )


def _node_using(root: PlanNode, index: str) -> PlanNode | None:
    return next((node for node in walk(root) if node.index_name == index), None)


def _scan_of(root: PlanNode, relation: str) -> PlanNode | None:
    """The node that reads the table, if the plan reads it directly."""
    return next((node for node in walk(root) if node.relation_name == relation), None)


def _describe(node: PlanNode | None) -> str:
    if node is None:
        return "no scan of the table"
    return f"{node.node_type} using {node.index_name}" if node.index_name else node.node_type


def _is_identifier(text: str) -> bool:
    return bool(_IDENTIFIER.match(text.strip()))


def _bare_name(text: str) -> str:
    return text.strip().strip('"')


def _structural_findings(
    verdict: IndexUse,
    row: dict[str, Any],
    parsed: QueryPredicates,
    predicates: list[Predicate],
    plan: PlanNode,
    column_types: dict[tuple[str, str], str],
    collation: str | None,
) -> list[IndexUseFinding]:
    """Everything that stops the planner using the index, read from text and catalog."""
    findings: list[IndexUseFinding] = []

    if not verdict.valid:
        findings.append(
            IndexUseFinding(
                reason=IndexUseReason.INVALID,
                confidence=Confidence.PROVEN,
                summary=f"{verdict.index} is not valid, so the planner never considers it.",
                detail=(
                    "An index is marked invalid when CREATE INDEX CONCURRENTLY fails partway, or while it is still being built. "
                    "It is maintained on every write and used for nothing."
                ),
                evidence={"valid": bool(row.get("valid")), "ready": bool(row.get("ready", True))},
                remediation=[f"REINDEX INDEX CONCURRENTLY {verdict.index};"],
            )
        )
        return findings

    keys = verdict.columns[: verdict.key_columns]
    if not keys:
        return findings
    leading = keys[0]
    leading_norm = normalise_expression(leading)
    leading_is_column = _is_identifier(leading)
    leading_name = _bare_name(leading) if leading_is_column else None
    attributed = any(p.table is None for p in parsed.predicates)
    text_confidence = Confidence.CANDIDATE if attributed else Confidence.PROVEN

    matching = [p for p in predicates if normalise_expression(p.expression) == leading_norm]

    if not matching:
        findings.append(_no_match(verdict, row, keys, leading, leading_name, predicates, text_confidence))
        return findings

    exclusions = [p for p in matching if p.negated or p.operator in _EXCLUSIONS]
    trigram = [p for p in matching if p.operator in _NEEDS_TRIGRAM] if verdict.method == "btree" else []
    wildcard = [p for p in matching if p.operator == "~~" and p.pattern is not None and p.pattern[:1] in ("%", "_")]
    usable = [p for p in matching if p not in exclusions and p not in trigram and p not in wildcard]

    if exclusions and not usable:
        p = exclusions[0]
        findings.append(
            IndexUseFinding(
                reason=IndexUseReason.OPERATOR_NOT_INDEXABLE,
                confidence=Confidence.PROVEN,
                summary=f"The restriction on {p.column} excludes rows ({p.text}), and an index finds rows, it does not avoid them.",
                detail=(
                    "A btree answers 'equal to', 'less than', 'between'. 'Not equal' matches almost every row, so the index "
                    "would be walked in full and the table read anyway; the planner skips it."
                ),
                evidence={"predicate": p.text, "operator": p.operator},
                remediation=[
                    "Add a positive restriction on an indexed column alongside it, or accept the scan: it is reading most of the table either way."
                ],
            )
        )
    if (trigram or wildcard) and not usable:
        p = (wildcard or trigram)[0]
        what = "a pattern starting with a wildcard" if p in wildcard else f"{'ILIKE' if p.operator == '~~*' else 'a regular expression'}"
        findings.append(
            IndexUseFinding(
                reason=IndexUseReason.OPERATOR_NOT_INDEXABLE,
                confidence=Confidence.PROVEN,
                summary=f"The restriction on {p.column} is {what} ({p.text}), which a btree cannot search.",
                detail=(
                    "A btree is ordered by whole values from their first character, so it can find a prefix and nothing else. "
                    "Matching inside a value, or regardless of case, needs an index of the trigrams the values contain."
                ),
                evidence={"predicate": p.text, "operator": p.operator, "pattern": p.pattern},
                remediation=[
                    "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
                    f"CREATE INDEX ON {verdict.table} USING gin ({_bare_name(leading)} gin_trgm_ops);",
                ]
                + (
                    [f"-- or, for case only: CREATE INDEX ON {verdict.table} (lower({_bare_name(leading)})); and compare lower({p.column})"]
                    if p.operator == "~~*"
                    else []
                ),
            )
        )
    prefix = [p for p in usable if p.operator == "~~"]
    if prefix and verdict.method == "btree" and not [p for p in usable if p.operator != "~~"]:
        opclass = str((row.get("opclasses") or [""])[0])
        if not opclass.endswith("_pattern_ops") and collation and collation not in _COLLATIONS_WITH_BYTE_ORDER:
            p = prefix[0]
            findings.append(
                IndexUseFinding(
                    reason=IndexUseReason.PATTERN_NEEDS_OPCLASS,
                    confidence=Confidence.PROVEN,
                    summary=(
                        f"LIKE '{p.pattern}' can use a btree on {p.column} only under the C collation or a pattern operator class; "
                        f"this index has {opclass} under {collation}."
                    ),
                    detail=(
                        f"Under {collation}, the order a btree keeps is not byte order, so a prefix does not map to one range of the "
                        "index. text_pattern_ops orders by bytes and makes it one range again; equality lookups keep working, but "
                        "ordering comparisons (<, >) do not use such an index."
                    ),
                    evidence={"predicate": p.text, "opclass": opclass, "collation": collation},
                    remediation=[f"CREATE INDEX ON {verdict.table} ({_bare_name(leading)} text_pattern_ops);"],
                )
            )

    if usable and all(p.or_group is not None and len(parsed.or_group_tables(p.or_group)) > 1 for p in usable):
        p = usable[0]
        findings.append(
            IndexUseFinding(
                reason=IndexUseReason.OR_ACROSS_TABLES,
                confidence=Confidence.PROVEN,
                summary=(
                    f"The restriction on {p.column} is one arm of an OR whose other arm is on another table, "
                    f"which no index on {verdict.table} can serve."
                ),
                detail=(
                    "An OR can use indexes when every arm restricts the same table, as a bitmap of each arm. An arm on another "
                    "table has to be decided per joined row, so the whole OR is applied after the join and the table is read in full."
                ),
                evidence={"predicate": p.text, "tables_in_or": sorted(parsed.or_group_tables(p.or_group or 0))},
                remediation=["Rewrite as a UNION of one query per arm, each restricting its own table."],
            )
        )

    if leading_name is not None:
        type_mismatch = _type_mismatch(verdict, plan, leading_name, column_types.get((verdict.table, leading_name)))
        if type_mismatch:
            findings.append(type_mismatch)

    if verdict.predicate:
        partial = _partial_not_matched(verdict, predicates)
        if partial:
            findings.append(partial)

    return findings


def _no_match(
    verdict: IndexUse,
    row: dict[str, Any],
    keys: list[str],
    leading: str,
    leading_name: str | None,
    predicates: list[Predicate],
    confidence: Confidence,
) -> IndexUseFinding:
    """Nothing in the query restricts the index's first column as written. Say what does."""
    restricted = sorted({p.expression for p in predicates})

    if leading_name is not None:
        wrapped = [p for p in predicates if p.column == leading_name and not p.bare]
        if wrapped:
            expressions = sorted({p.expression for p in wrapped})
            return IndexUseFinding(
                reason=IndexUseReason.EXPRESSION_MISMATCH,
                confidence=Confidence.CANDIDATE,
                summary=f"The query restricts {', '.join(expressions)}, and the index is on {leading_name} itself.",
                detail=(
                    f"An index is on exactly the expression it was created with. A function, a cast or arithmetic around "
                    f"{leading_name} produces a different value, which the index does not hold, so the planner cannot look it up. "
                    "Either index the expression the query uses, or restate the restriction on the bare column — a cast to a date "
                    "becomes a range on the timestamp, for instance."
                ),
                evidence={"index_column": leading, "query_expressions": expressions, "predicates": [p.text for p in wrapped]},
                remediation=[f"CREATE INDEX ON {verdict.table} ({expression});" for expression in expressions],
            )
    else:
        underlying = {_bare_name(w) for w in _WORDS.findall(leading)}
        bare = [p for p in predicates if p.bare and p.column in underlying]
        if bare:
            return IndexUseFinding(
                reason=IndexUseReason.EXPRESSION_MISMATCH,
                confidence=Confidence.CANDIDATE,
                summary=f"The index is on the expression {leading}, and the query restricts {', '.join(sorted({p.column for p in bare}))} directly.",
                detail=(
                    "An expression index holds the expression's values, not the column's, so it answers only a predicate written "
                    "as that expression. Write the restriction as the index does, or index the bare column."
                ),
                evidence={"index_column": leading, "predicates": [p.text for p in bare]},
                remediation=[
                    f"-- compare {leading} = ... instead",
                    f"CREATE INDEX ON {verdict.table} ({', '.join(sorted({p.column for p in bare}))});",
                ],
            )

    for position, key in enumerate(keys[1:], start=2):
        key_norm = normalise_expression(key)
        later = [p for p in predicates if normalise_expression(p.expression) == key_norm]
        if later:
            return IndexUseFinding(
                reason=IndexUseReason.NOT_LEADING_COLUMN,
                confidence=confidence,
                summary=(
                    f"The query restricts {key}, the index's column {position}, and not {leading}, its first; "
                    "a btree is entered through its first column."
                ),
                detail=(
                    f"A multi-column btree is sorted by {leading} first and by {key} only within equal values of {leading}. Without a "
                    f"restriction on {leading} there is no range of the index to read; the planner would have to scan all of it."
                ),
                evidence={"index_columns": keys, "restricted_column": key, "predicates": [p.text for p in later]},
                remediation=[
                    f"CREATE INDEX ON {verdict.table} ({_bare_name(key)});",
                    f"-- or lead with it: CREATE INDEX ON {verdict.table} ({_bare_name(key)}, {_bare_name(leading)});",
                ],
            )

    if not predicates:
        detail = f"The query places no restriction on {verdict.table} at all, so every row is wanted and the table is read whole."
    else:
        detail = f"The query restricts {', '.join(restricted)} on {verdict.table}; none of those is a column of this index ({', '.join(keys)})."
    return IndexUseFinding(
        reason=IndexUseReason.NO_PREDICATE,
        confidence=confidence,
        summary=f"The query does not restrict {leading}, the index's first column.",
        detail=detail + " An index serves a predicate; with none on its columns it has nothing to look up.",
        evidence={"index_columns": keys, "restricted": restricted},
        remediation=[f"CREATE INDEX ON {verdict.table} ({expression});" for expression in restricted[:3]],
    )


_CAST_OF = r"\(\s*(?:[a-z_][a-z0-9_$]*\.)?{column}\s*\)::([a-z_][a-z0-9_ ]*)"


def _type_mismatch(verdict: IndexUse, plan: PlanNode, column: str, column_type: str | None) -> IndexUseFinding | None:
    """The plan casts the column before comparing it, so the index's values are not what is compared."""
    scan = _scan_of(plan, verdict.table.rpartition(".")[2])
    if scan is None:
        return None
    pattern = re.compile(_CAST_OF.format(column=re.escape(column)), re.IGNORECASE)
    for text in conditions(scan):
        match = pattern.search(text)
        if match:
            cast_to = match.group(1).strip()
            return IndexUseFinding(
                reason=IndexUseReason.TYPE_MISMATCH,
                confidence=Confidence.PROVEN,
                summary=(
                    f"The planner casts {column} to {cast_to} before comparing it, and the index holds {column} as {column_type or 'its own type'}."
                ),
                detail=(
                    f"The value the query compares against is a {cast_to}, so PostgreSQL converts the column to match it on every "
                    "row. The index is on the unconverted column and cannot be searched for the converted value. Convert the value "
                    "to the column's type instead; the index then applies."
                ),
                evidence={"condition": text, "cast_to": cast_to, "column_type": column_type},
                remediation=[f"-- compare {column} with a value of type {column_type or 'the column'}: cast the parameter, not the column"],
            )
    return None


def _partial_not_matched(verdict: IndexUse, predicates: list[Predicate]) -> IndexUseFinding | None:
    """A partial index the query gives the planner no way to prove it may use."""
    try:
        required = read_predicates(predicate_as_statement(verdict.table, str(verdict.predicate)))
    except InvalidArgumentError:
        return None
    needed = {p.column for p in required.predicates}
    if not needed:
        return None
    restricted = {p.column for p in predicates}
    if needed & restricted:
        # The query does restrict the same columns. Whether its restriction
        # implies the index's is for the planner to decide, and the forced
        # plan reports what it decided.
        return None
    return IndexUseFinding(
        reason=IndexUseReason.PARTIAL_PREDICATE_NOT_MATCHED,
        confidence=Confidence.CANDIDATE,
        summary=(
            f"The index holds only rows WHERE {verdict.predicate}, and the query does not restrict {', '.join(sorted(needed))}, "
            "so the planner cannot know its rows are in it."
        ),
        detail=(
            "A partial index can be used only when the query's WHERE clause implies the index's, and the planner proves that "
            "from the query's own restrictions on the same columns. A query that does not mention them may want rows the "
            "index does not hold."
        ),
        evidence={"index_predicate": verdict.predicate, "index_predicate_columns": sorted(needed), "query_columns": sorted(restricted)},
        remediation=[f"-- add WHERE {verdict.predicate} to the query if that is what it means, or index without the WHERE"],
    )


def _planner_preferred(
    verdict: IndexUse, row: dict[str, Any], plan: ExplainPlanArtifact, forced: ExplainPlanArtifact, forced_node: PlanNode
) -> IndexUseFinding:
    """Nothing stops the index; the planner costed the other plan lower, and here is by how much."""
    relation = verdict.table.rpartition(".")[2]
    chosen = _scan_of(plan.plan_tree, relation)
    chosen_cost = plan.plan_tree.total_cost
    forced_cost = forced.plan_tree.total_cost
    table_rows = float(row.get("table_rows") or 0)
    table_pages = int(row.get("table_pages") or 0)
    expected = chosen.plan_rows if chosen is not None else None
    share = (expected / table_rows) if expected is not None and table_rows > 0 else None

    detail = (
        f"With sequential scans disabled the planner reads {verdict.table} by {_describe(forced_node)} at an estimated total cost "
        f"of {forced_cost:,.0f}, against {chosen_cost:,.0f} for the plan it chose, which reads the table by {_describe(chosen)}."
    )
    remediation: list[str] = []
    if table_pages and table_pages <= TINY_TABLE_PAGES:
        detail += (
            f" The table is {table_pages} page{'s' if table_pages != 1 else ''}: reading it whole is cheaper than any index "
            "lookup, and no index will be chosen until it grows."
        )
    elif share is not None:
        detail += f" It expects the scan to return {expected:,} of the table's {table_rows:,.0f} rows ({share:.1%})."
        if share >= INDEX_WORTHWHILE_SHARE:
            detail += (
                " At that share the rows are spread across most of the table's pages, and visiting them through the index costs "
                "more than reading the pages in order. If the estimate is right, this index is the wrong tool for this query; "
                "if it is wrong, ANALYZE and look again."
            )
            remediation.append(f"ANALYZE {verdict.table};")
        else:
            detail += (
                " That is a small share, which normally favours the index, so the margin is elsewhere: the cost of random reads "
                "(random_page_cost against seq_page_cost), or the rows being nearly as many as the estimate on a table small "
                "enough to read whole."
            )
            remediation.append("-- compare the two costs above; if storage is solid state, random_page_cost = 1.1 is the usual setting")
    return IndexUseFinding(
        reason=IndexUseReason.PLANNER_PREFERRED_OTHER,
        confidence=Confidence.PROVEN,
        summary=(
            f"Nothing stops the planner using {verdict.index}; it judged {_describe(chosen)} cheaper ({chosen_cost:,.0f} against {forced_cost:,.0f})."
        ),
        detail=detail,
        evidence={
            "chosen_cost": chosen_cost,
            "forced_cost": forced_cost,
            "chosen_scan": _describe(chosen),
            "forced_scan": _describe(forced_node),
            "expected_rows": expected,
            "table_rows": table_rows,
            "share": share,
            "table_pages": table_pages,
        },
        remediation=remediation,
    )


def _other_index_or_unexplained(verdict: IndexUse, row: dict[str, Any], plan: ExplainPlanArtifact, forced: ExplainPlanArtifact) -> IndexUseFinding:
    """Forcing changed nothing: either another index is the planner's choice either way, or nothing here explains it."""
    relation = verdict.table.rpartition(".")[2]
    forced_scan = _scan_of(forced.plan_tree, relation)
    chosen = _scan_of(plan.plan_tree, relation)
    if forced_scan is not None and forced_scan.index_name and forced_scan.index_name != verdict.index:
        other = forced_scan.index_name
        return IndexUseFinding(
            reason=IndexUseReason.PLANNER_PREFERRED_OTHER,
            confidence=Confidence.PROVEN,
            summary=f"The planner reads {verdict.table} through {other} rather than {verdict.index}, with and without sequential scans available.",
            detail=(
                f"The chosen plan reads the table by {_describe(chosen)} at an estimated total cost of {plan.plan_tree.total_cost:,.0f}; "
                f"with sequential scans disabled it is {_describe(forced_scan)} at {forced.plan_tree.total_cost:,.0f}. In the planner's "
                f"estimate {other} answers this query's restrictions at least as cheaply, so this index is not wrong, it is second. "
                "A planner cannot be made to prefer one index over another; if this one should win, the question is why the "
                "estimate for the other is lower, and column-stats on the columns of each will show it."
            ),
            evidence={
                "chosen_cost": plan.plan_tree.total_cost,
                "forced_cost": forced.plan_tree.total_cost,
                "chosen_scan": _describe(chosen),
                "forced_scan": _describe(forced_scan),
                "preferred_index": other,
            },
            remediation=[],
        )
    return IndexUseFinding(
        reason=IndexUseReason.UNEXPLAINED,
        confidence=Confidence.CANDIDATE,
        summary=f"Even with sequential scans disabled the planner does not use {verdict.index}, and nothing here says why.",
        detail=(
            f"It reads {verdict.table} by {_describe(forced_scan)} instead. The checks here cover the common reasons; what is left "
            "is usually an operator the index's operator class does not support, or a restriction the planner could not push "
            "down to the scan. Compare the plan against a hypothetical index with the same definition to see whether a fresh "
            "one would be chosen."
        ),
        evidence={"forced_scan": _describe(forced_scan), "definition": verdict.definition},
        remediation=[f"pg-debug explain -i '{verdict.table}({', '.join(_bare_name(c) for c in verdict.columns[: verdict.key_columns])})' \"...\""],
    )


def _stale(table: str, stale: dict[str, Any]) -> IndexUseFinding:
    if stale.get("never_analyzed"):
        reason = "it has never been analyzed, so the planner is working from a default guess"
    else:
        reason = f"{int(stale.get('changed_rows') or 0):,} rows have changed since the last ANALYZE at {stale.get('last_analyzed')}"
    return IndexUseFinding(
        reason=IndexUseReason.STALE_STATISTICS,
        confidence=Confidence.CANDIDATE,
        summary=f"{table}'s statistics no longer describe it: {reason}.",
        detail=(
            "The choice between the index and a scan rests on the row estimate, and the estimate rests on these statistics. "
            "Refresh them and look again before acting on anything else here."
        ),
        evidence={k: stale.get(k) for k in ("last_analyzed", "changed_rows", "changed_fraction", "never_analyzed")},
        remediation=[f"ANALYZE {table};"],
    )


__all__ = ["INDEX_WORTHWHILE_SHARE", "TINY_TABLE_PAGES", "IndexUseAnalyzer"]

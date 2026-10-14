"""Rendering helpers shared by the CLI.

Everything the library returns is JSON-friendly Python data; these helpers turn
it into something readable in a terminal, or into what ``--json`` prints. Each
command names its own two in ``build_parser``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any

import humanize

from ..baseline import Baseline
from ..baseline import Key
from ..baseline import Partition
from ..baseline import describe_key
from ..baseline import health_finding_key

if TYPE_CHECKING:
    from ..plans.tree import ExplainPlanArtifact

MAX_CELL_WIDTH = 60


def to_json(value: Any, indent: int = 2) -> str:
    """Serialize a value as JSON: a result through its ``to_dict()``, and
    anything else JSON cannot represent as its string."""
    return json.dumps(value, indent=indent, default=_jsonable, sort_keys=False)


def _jsonable(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    return to_dict() if callable(to_dict) else str(value)


# -- What --json prints -------------------------------------------------------
#
# Each takes the command's result and the baseline it is compared against, if
# any. Most commands print their result as it is.


def as_is(result: Any, baseline: Baseline | None) -> Any:
    return result


def plan_document(plan: ExplainPlanArtifact, baseline: Baseline | None) -> Any:
    """The plan EXPLAIN returned, as JSON rather than a JSON-encoded string."""
    try:
        return json.loads(plan.value)
    except ValueError:
        return plan.value


def marked_against(key: Callable[[Any], Key]) -> Callable[[Sequence[Any], Baseline | None], list[dict[str, Any]]]:
    """Findings as dicts, each marked ``known`` or ``new`` when there is a baseline."""

    def payload(findings: Sequence[Any], baseline: Baseline | None) -> list[dict[str, Any]]:
        return [_with_baseline(finding.to_dict(), key(finding), baseline) for finding in findings]

    return payload


def health_checks_json(results: Mapping[str, Any], baseline: Baseline | None) -> dict[str, Any]:
    return {name: _health_check_json(name, check, baseline) for name, check in results.items()}


def _with_baseline(payload: dict[str, Any], key: Key, baseline: Baseline | None) -> dict[str, Any]:
    if baseline is not None:
        payload["baseline"] = "known" if key in baseline else "new"
    return payload


def _health_check_json(name: str, check: Any, baseline: Baseline | None) -> dict[str, Any]:
    payload = check.to_dict()
    if baseline is None or check.healthy or check.error is not None:
        # A check that could not run found nothing, so there is nothing to
        # mark known or new; its "error" says why.
        return payload
    if not check.findings:
        payload["baseline"] = "known" if {"check": name} in baseline else "new"
        return payload
    payload["findings"] = [_with_baseline(dict(finding), health_finding_key(name, finding), baseline) for finding in check.findings]
    return payload


def render_baseline_trailer(path: str, split: Partition) -> str:
    """The line after the report that says what the baseline changed."""
    if not split.new:
        return f"Baseline {path}: {len(split.known)} known finding(s), nothing new."
    lines = [f"Baseline {path}: {len(split.known)} known finding(s) ignored, {len(split.new)} new:"]
    lines.extend(f"  {describe_key(key)}" for key in split.new)
    return "\n".join(lines)


def render_plan(plan: ExplainPlanArtifact) -> str:
    return plan.to_text()


def _cell(value: Any, max_width: int = MAX_CELL_WIDTH) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        # Before the numeric branch: a bool is an int, and True would otherwise
        # print as 1.
        text = str(value)
    elif isinstance(value, (float, Decimal)):
        # Raw repr is noise in a table — a float shows as 332.48437299999995,
        # and a numeric division that came out zero shows as 0E-20. The JSON
        # output keeps full precision for anything that needs it.
        #
        # Integral Decimals are left whole: wal_bytes is a numeric because it
        # can exceed a bigint, not because it has a fractional part, and
        # printing it as 4198.000 would be worse than useless.
        text = str(int(value)) if isinstance(value, Decimal) and value == value.to_integral_value() else f"{value:.3f}"
    else:
        text = str(value)
    text = " ".join(text.split())
    if max_width and len(text) > max_width:
        text = text[: max_width - 1] + "…"
    return text


def render_rows(rows: Sequence[Mapping[str, Any]], empty: str = "(no rows)", max_width: int = MAX_CELL_WIDTH) -> str:
    """Render a list of uniform dicts as an aligned text table."""
    if not rows:
        return empty

    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    cells = [[_cell(row.get(col), max_width) for col in columns] for row in rows]
    widths = [max(len(col), *(len(row[i]) for row in cells)) for i, col in enumerate(columns)]

    lines = [
        "  ".join(col.ljust(widths[i]) for i, col in enumerate(columns)).rstrip(),
        "  ".join("-" * widths[i] for i in range(len(columns))),
    ]
    lines.extend("  ".join(row[i].ljust(widths[i]) for i in range(len(columns))).rstrip() for row in cells)
    lines.append(f"\n({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(lines)


def render_mapping(mapping: Mapping[str, Any], empty: str = "(nothing to report)") -> str:
    """Render a name/value mapping as one ``name: value`` line per entry."""
    if not mapping:
        return empty
    return "\n".join(f"{name}: {value}" for name, value in mapping.items())


def format_duration(seconds: Any) -> str:
    """Render a duration in the largest unit that keeps it readable."""
    if seconds is None:
        return ""
    seconds = float(seconds)
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m"


def render_activity(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render backends as a scannable table, keeping the query text wide."""
    if not rows:
        return "(nothing running)"

    projected = [
        {
            "pid": row.get("pid"),
            "user": row.get("usename"),
            "state": row.get("state") or row.get("backend_type"),
            "wait": ":".join(p for p in (row.get("wait_event_type"), row.get("wait_event")) if p),
            "for": format_duration(row.get("state_seconds")),
            "blocked_by": ",".join(str(pid) for pid in row.get("blocked_by") or []),
            "query": row.get("query"),
        }
        for row in rows
    ]
    return render_rows(projected, max_width=80)


def render_lock_tree(roots: Sequence[Mapping[str, Any]]) -> str:
    """Render each root blocker with the backends waiting on it nested beneath."""
    if not roots:
        return "(nothing is blocked)"

    lines: list[str] = []
    for root in roots:
        _lock_tree_lines(root, 0, lines)
        lines.append("")
    return "\n".join(lines).rstrip()


def _lock_tree_lines(node: Mapping[str, Any], depth: int, lines: list[str]) -> None:
    indent = "  " * depth
    state = node.get("state") or node.get("backend_type") or "?"
    parts = [f"{indent}pid {node.get('pid')}:"]
    waits_for = node.get("waits_for")
    if waits_for:
        parts.append(f"waits for {waits_for.get('description')};")
    parts.append(f"{state} for {format_duration(node.get('state_seconds'))}")
    if node.get("transaction_seconds") is not None and state != "active":
        parts.append(f"in a transaction open {format_duration(node.get('transaction_seconds'))}")
    who = f"{node.get('usename') or '?'}@{node.get('datname') or '?'}"
    if node.get("application_name"):
        who += f" {node['application_name']}"
    parts.append(f"({who})")
    if depth == 0:
        parts.append(f"blocking {node.get('blocked_total', 0)}")
    if node.get("cycle"):
        parts.append("[deadlock: waits on something that waits on it]")
    lines.append(" ".join(parts))

    query = " ".join(str(node.get("query") or "").split())
    if query:
        lines.append(f"{indent}    {query[:MAX_QUERY_WIDTH]}{'…' if len(query) > MAX_QUERY_WIDTH else ''}")

    for child in node.get("blocking") or []:
        _lock_tree_lines(child, depth + 1, lines)


MAX_QUERY_WIDTH = 120


def render_vacuum_progress(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render running vacuums with their progress through the heap."""
    if not rows:
        return "(no vacuum running)"

    projected = [
        {
            "pid": row.get("pid"),
            "table": ".".join(p for p in (row.get("schema"), row.get("table")) if p),
            "phase": row.get("phase"),
            "scanned": f"{row.get('percent_scanned')}%" if row.get("percent_scanned") is not None else "",
            "heap_blks": f"{row.get('heap_blks_scanned')}/{row.get('heap_blks_total')}",
            "index_passes": row.get("index_vacuum_count"),
            "running": format_duration(row.get("running_seconds")),
            "autovacuum": row.get("autovacuum"),
        }
        for row in rows
    ]
    return render_rows(projected)


def render_column_stats(result: Mapping[str, Any]) -> str:
    """Render per-column planner statistics, with any extended statistics."""
    columns = result.get("columns") or []
    if not columns:
        return "(no statistics — the table has never been analyzed, or does not exist)"

    sections = ["Column statistics", render_rows(columns)]

    extended = result.get("extended_statistics") or []
    if extended:
        sections += [
            "",
            "Extended statistics",
            render_rows([{**row, "columns": ", ".join(row.get("columns") or []), "kinds": ", ".join(row.get("kinds") or [])} for row in extended]),
        ]
    return "\n".join(sections)


def render_health_checks(results: Mapping[str, Any], empty: str = "No health checks were performed.") -> str:
    """Render health check results as one ``name: summary`` line per check.

    A check that could not run is listed in its place, its summary saying so
    and why, so the report shows every check that was asked for.
    """
    if not results:
        return empty
    return "\n".join(f"{name}: {result.summary}" for name, result in results.items())


def render_incomplete_health_run(results: Mapping[str, Any]) -> str | None:
    """The line for stderr that says which checks could not run, if any did not."""
    failed = [name for name, result in results.items() if result.error is not None]
    if not failed:
        return None
    noun = "check" if len(results) == 1 else "checks"
    return f"{len(failed)} of {len(results)} health {noun} could not run, so this run is incomplete: {', '.join(failed)}."


def render_object_details(details: Mapping[str, Any]) -> str:
    """Render the output of ``get_object_details`` as a readable description."""
    if not details:
        return "(object not found)"

    if "basic" not in details:
        return render_mapping(details)

    basic = details["basic"]
    sections = [f"{basic.get('type', 'object').title()} {basic.get('schema')}.{basic.get('name')}", ""]

    sections.append("Columns")
    sections.append(render_rows(details.get("columns", []), empty="(no columns)"))
    sections.append("")

    sections.append("Constraints")
    constraints = [{**c, "columns": ", ".join(c.get("columns", []))} for c in details.get("constraints", [])]
    sections.append(render_rows(constraints, empty="(no constraints)"))
    sections.append("")

    sections.append("Indexes")
    sections.append(render_rows(details.get("indexes", []), empty="(no indexes)", max_width=0))
    return "\n".join(sections)


def render_index_recommendations(result: Mapping[str, Any]) -> str:
    """Render the output of the index tuning commands."""
    recommendations = result.get("recommendations")
    if not recommendations or isinstance(recommendations, str):
        return str(recommendations or "No index recommendations found.")

    summary = result.get("summary", {})
    lines = [
        f"{summary.get('total_recommendations', len(recommendations))} recommended index(es), "
        f"estimated total size {summary.get('total_size_bytes', 'unknown')}",
        f"Estimated workload cost: {summary.get('base_cost', '?')} -> {summary.get('new_cost', '?')} "
        f"({summary.get('improvement_multiple', '?')}x improvement)",
        "",
    ]

    for rec in recommendations:
        lines.append(f"{rec['index_apply_order']}. {rec['index_definition']}")
        individual = rec.get("benefit_of_this_index_only", {})
        progressive = rec.get("benefit_after_previous_indexes", {})
        lines.append(f"   size: {rec.get('index_estimated_size', 'unknown')}")
        lines.append(f"   on its own:            {individual.get('improvement_multiple', '?')}x improvement")
        lines.append(f"   after earlier indexes: {progressive.get('improvement_multiple', '?')}x improvement")
        if rec.get("warning"):
            lines.append(f"   warning: {rec['warning']}")
        lines.append("")

    query_impact: Iterable[dict[str, Any]] = result.get("query_impact", [])
    for impact in query_impact:
        lines.append(f"Query: {impact['query']}")
        lines.append(f"  cost {impact['base_cost']} -> {impact['new_cost']} ({impact['improvement_multiple']}x)")
        lines.append("")

    return "\n".join(lines).rstrip()


def render_index_use(verdicts: Sequence[Any]) -> str:
    """Render each index with its verdict, and the reasons beneath the ones not used."""
    if not verdicts:
        return "(no indexes on the tables this query reads)"

    lines: list[str] = []
    for verdict in verdicts:
        size = humanize.naturalsize(verdict.size_bytes, binary=True) if verdict.size_bytes else ""
        head = f"{verdict.table}: {verdict.index}  {verdict.description}" + (f"  {size}" if size else "")
        if verdict.used:
            lines.append(f"{head}  -- used by {verdict.node_type}")
        else:
            lines.append(f"{head}  -- NOT used")
        for finding in verdict.findings:
            lines.append(f"  [{finding.confidence.value}] {finding.reason.value.replace('_', ' ')}: {finding.summary}")
            lines.append(f"      {finding.detail}")
            for step in finding.remediation:
                lines.append(f"      {step}")
        for note in verdict.notes:
            lines.append(f"  note: {note}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_model_findings(findings: Sequence[Any]) -> str:
    """Render a data-model review, grouped by the table each finding is about.

    A finding is only worth reading if it can be acted on, so each one shows
    its evidence and the statements that fix it. Candidates are marked as such
    and carry the query that confirms them first.
    """
    if not findings:
        return "No data model findings."

    lines: list[str] = []
    current_table = None
    for finding in findings:
        if finding.qualified_table != current_table:
            current_table = finding.qualified_table
            lines.append("")
            lines.append(current_table)

        marker = "" if finding.confidence.value == "proven" else "  (candidate)"
        lines.append(f"  [{finding.severity.value}] {finding.rule.value}{marker}")
        lines.append(f"    {finding.summary}")
        lines.append(f"    {finding.detail}")
        if finding.impact_bytes:
            lines.append(f"    saves: {humanize.naturalsize(finding.impact_bytes)}")
        if finding.verify:
            lines.append(f"    verify: {finding.verify}")
        for statement in finding.remediation:
            lines.append(f"    {statement}")

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
    summary = ", ".join(f"{count} {severity}" for severity, count in counts.items())
    lines.append("")
    lines.append(f"{len(findings)} findings ({summary})")

    return "\n".join(lines).strip("\n")


def render_configuration_review(findings: Sequence[Any]) -> str:
    """Render a configuration review, most severe first."""
    if not findings:
        return "Nothing measurable is wrong with the configuration."

    lines: list[str] = []
    for finding in findings:
        lines.append("")
        lines.append(f"[{finding.severity.value}] {finding.setting} = {finding.current}")
        lines.append(f"    {finding.summary}")
        lines.append(f"    {finding.detail}")
        if finding.recommended:
            lines.append(f"    suggested: {finding.recommended}")

    lines.append("")
    lines.append(f"{len(findings)} setting{'' if len(findings) == 1 else 's'} worth looking at")
    return "\n".join(lines).strip("\n")


def render_migration_review(reviews: Sequence[Any]) -> str:
    """Render a migration review, worst statement first.

    The lock and how long it is held are the two halves of the answer, so they
    are always shown together — a lock level on its own tells the reader
    nothing about whether to worry.
    """
    if not reviews:
        return "Nothing in this migration takes a lock worth reporting."

    lines: list[str] = []
    for review in reviews:
        lines.append("")
        lines.append(f"[{review.severity.value}] {review.statement}")
        lines.append(f"    lock:     {review.lock.value} — blocks {review.blocks}")
        lines.append(f"    held for: {review.duration.value}")
        lines.append(f"    {review.summary}")
        lines.append(f"    {review.detail}")
        if review.safer:
            lines.append("    safer:")
            for statement in review.safer:
                lines.append(f"      {statement}")

    counts: dict[str, int] = {}
    for review in reviews:
        counts[review.severity.value] = counts.get(review.severity.value, 0) + 1
    summary = ", ".join(f"{count} {severity}" for severity, count in counts.items())
    lines.append("")
    noun = "statement" if len(reviews) == 1 else "statements"
    lines.append(f"{len(reviews)} {noun} ({summary})")
    return "\n".join(lines).strip("\n")


def render_plan_findings(findings: Sequence[Any]) -> str:
    """Render a plan diagnosis, the costliest finding first.

    The percentages are shares of the query's work per finding, and they do not
    add up to one: two findings can be about the same node, and a bad estimate
    is charged for the whole subtree it misled. The closing line says so, since
    a column of percentages otherwise reads as a breakdown.
    """
    if not findings:
        return "Nothing wrong with this plan: the estimates match what the query found, and nothing spilled to disk."

    lines: list[str] = []
    for finding in findings:
        where = f" on {finding.relation}" if finding.relation else ""
        lines.append("")
        lines.append(f"[{finding.severity.value}] {finding.issue.value} — {finding.node_type}{where}")
        lines.append(f"    accounts for {finding.share_of_time:.0%} of the query's work ({format_duration(finding.cost_ms / 1000)})")
        lines.append(f"    {finding.summary}")
        lines.append(f"    {finding.detail}")
        if finding.remediation:
            lines.append("")
            for statement in finding.remediation:
                lines.append(f"      {statement}")

    lines.append("")
    noun = "finding" if len(findings) == 1 else "findings"
    lines.append(f"{len(findings)} {noun}. Shares are per finding and overlap — they are not a breakdown of the total.")
    return "\n".join(lines).strip("\n")


def render_partition_candidates(candidates: Sequence[Any]) -> str:
    """Render the partitioning verdicts, recommended tables first.

    The tables advised against are printed too, and with the number behind the
    verdict, because "we looked and it would not pay" is the answer more often
    and is worth as much as a recommendation. They come after a divider so that
    nobody skims the top of the output and reads a rejection as advice.
    """
    if not candidates:
        return "No table is both large enough and busy enough to be worth partitioning."

    recommended = [candidate for candidate in candidates if candidate.recommended]
    against = [candidate for candidate in candidates if not candidate.recommended]

    lines: list[str] = []
    for candidate in recommended:
        lines.extend(_partition_candidate_lines(candidate))

    if against:
        lines.append("")
        lines.append("-- Examined and not recommended " + "-" * 42)
        for candidate in against:
            lines.extend(_partition_candidate_lines(candidate))

    lines.append("")
    lines.append(f"{len(recommended)} of {len(candidates)} table{'' if len(candidates) == 1 else 's'} examined would gain from partitioning")
    return "\n".join(lines).strip("\n")


def _partition_candidate_lines(candidate: Any) -> list[str]:
    verdict = "partition" if candidate.recommended else "leave as it is"
    lines = [
        "",
        f"[{verdict}] {candidate.qualified_table} — {humanize.naturalsize(candidate.table_bytes)}, {candidate.row_count:,} rows",
        f"    key:      {candidate.strategy.value.upper()} ({candidate.column}) {candidate.column_type}",
        f"    prunes:   {candidate.pruning_share:.0%} of the recorded query time against this table",
    ]
    for reason in candidate.reasons:
        lines.append(f"    {reason}")

    alternatives = candidate.evidence.get("alternatives") or []
    others = [a for a in alternatives if a["column"] != candidate.column]
    if others:
        shown = ", ".join(f"{a['column']} ({a['pruning_share']:.0%}, {a['strategy']})" for a in others[:3])
        lines.append(f"    also considered: {shown}")

    for blocker in candidate.blockers:
        lines.append(f"    ! {blocker}")

    if candidate.ddl:
        lines.append("")
        for statement in candidate.ddl:
            lines.append(f"      {statement}")

    return lines


__all__ = [
    "format_duration",
    "render_activity",
    "render_column_stats",
    "render_configuration_review",
    "render_health_checks",
    "render_incomplete_health_run",
    "render_index_recommendations",
    "render_index_use",
    "render_lock_tree",
    "render_mapping",
    "render_migration_review",
    "render_model_findings",
    "render_object_details",
    "render_partition_candidates",
    "render_plan_findings",
    "render_rows",
    "render_vacuum_progress",
    "to_json",
]

"""Rendering helpers shared by the CLI.

Everything the library returns is JSON-friendly Python data; these helpers turn
it into something readable in a terminal.
"""

from __future__ import annotations

import json
from typing import Any
from typing import Dict
from typing import Iterable
from typing import List
from typing import Mapping
from typing import Sequence

import humanize

MAX_CELL_WIDTH = 60


def to_json(value: Any, indent: int = 2) -> str:
    """Serialize a value as JSON, stringifying anything JSON cannot represent."""
    return json.dumps(value, indent=indent, default=str, sort_keys=False)


def _cell(value: Any, max_width: int = MAX_CELL_WIDTH) -> str:
    if value is None:
        text = ""
    elif isinstance(value, float):
        # Raw float repr (332.48437299999995) is noise in a table; the JSON
        # output keeps full precision for anything that needs it.
        text = f"{value:.3f}"
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

    columns: List[str] = []
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
    """Render health check results as one ``name: summary`` line per check."""
    if not results:
        return empty
    return "\n".join(f"{name}: {result.summary}" for name, result in results.items())


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
    if "error" in result:
        return f"Index tuning failed: {result['error']}"

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

    query_impact: Iterable[Dict[str, Any]] = result.get("query_impact", [])
    for impact in query_impact:
        lines.append(f"Query: {impact['query']}")
        lines.append(f"  cost {impact['base_cost']} -> {impact['new_cost']} ({impact['improvement_multiple']}x)")
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

    lines: List[str] = []
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

    counts: Dict[str, int] = {}
    for finding in findings:
        counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
    summary = ", ".join(f"{count} {severity}" for severity, count in counts.items())
    lines.append("")
    lines.append(f"{len(findings)} findings ({summary})")

    return "\n".join(lines).strip("\n")


def render_migration_review(reviews: Sequence[Any]) -> str:
    """Render a migration review, worst statement first.

    The lock and how long it is held are the two halves of the answer, so they
    are always shown together — a lock level on its own tells the reader
    nothing about whether to worry.
    """
    if not reviews:
        return "Nothing in this migration takes a lock worth reporting."

    lines: List[str] = []
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

    counts: Dict[str, int] = {}
    for review in reviews:
        counts[review.severity.value] = counts.get(review.severity.value, 0) + 1
    summary = ", ".join(f"{count} {severity}" for severity, count in counts.items())
    lines.append("")
    noun = "statement" if len(reviews) == 1 else "statements"
    lines.append(f"{len(reviews)} {noun} ({summary})")
    return "\n".join(lines).strip("\n")


__all__ = [
    "format_duration",
    "render_activity",
    "render_column_stats",
    "render_health_checks",
    "render_index_recommendations",
    "render_mapping",
    "render_migration_review",
    "render_model_findings",
    "render_object_details",
    "render_rows",
    "render_vacuum_progress",
    "to_json",
]

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

MAX_CELL_WIDTH = 60


def to_json(value: Any, indent: int = 2) -> str:
    """Serialize a value as JSON, stringifying anything JSON cannot represent."""
    return json.dumps(value, indent=indent, default=str, sort_keys=False)


def _cell(value: Any, max_width: int = MAX_CELL_WIDTH) -> str:
    if value is None:
        text = ""
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


__all__ = [
    "render_health_checks",
    "render_index_recommendations",
    "render_mapping",
    "render_object_details",
    "render_rows",
    "to_json",
]

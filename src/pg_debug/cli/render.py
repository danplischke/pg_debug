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
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any

import humanize

from ..baseline import Baseline
from ..baseline import Key
from ..baseline import Partition
from ..baseline import audit_finding_key
from ..baseline import describe_key
from ..baseline import health_finding_key
from ..db.privileges import is_withheld
from ..waits import WaitProfile

if TYPE_CHECKING:
    from ..db.privileges import StatisticsAccess
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


def render_baseline_trailer(path: str, split: Partition, could_not_run: int = 0) -> str:
    """The line after the report that says what the baseline changed.

    ``could_not_run`` counts the checks that never looked. Nothing new among
    the others is not a pass, so the line says how many were not compared.
    """
    among = " among the checks that ran" if could_not_run else ""
    unrun = f"{could_not_run} check could not run and was" if could_not_run == 1 else f"{could_not_run} checks could not run and were"
    if not split.new:
        line = f"Baseline {path}: {len(split.known)} known finding(s), nothing new{among}."
        return f"{line} {unrun} not compared, so this is not a pass." if could_not_run else line
    lines = [f"Baseline {path}: {len(split.known)} known finding(s) ignored, {len(split.new)} new{among}:"]
    lines.extend(f"  {describe_key(key)}" for key in split.new)
    if could_not_run:
        lines.append(f"{unrun} not compared.")
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


#: How wide a cell of the activity table may be. What PostgreSQL still shows
#: of a backend it withheld the details of is not cut to it: the locks it holds
#: are the reason it is listed.
ACTIVITY_CELL_WIDTH = 80


def render_activity(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render backends as a scannable table, keeping the query text wide.

    A backend whose state, query and wait PostgreSQL withheld from the role
    reads as in a transaction or not, with what is still shown of it in place
    of the query: the locks it holds, the backends waiting on it, its database
    and application, when the rows carry them.
    """
    if not rows:
        return "(nothing running)"

    projected = [
        {
            "pid": row.get("pid"),
            "user": row.get("usename"),
            "state": _withheld_state(row) if is_withheld(row) else row.get("state") or row.get("backend_type"),
            "wait": ":".join(p for p in (row.get("wait_event_type"), row.get("wait_event")) if p),
            "for": format_duration(row.get("state_seconds")),
            "blocked_by": ",".join(str(pid) for pid in row.get("blocked_by") or []),
            "blocking": row.get("blocking_total") or "",
            "query": _withheld_details(row) if is_withheld(row) else row.get("query"),
        }
        for row in rows
    ]
    cells = [
        {column: value if column == "query" and is_withheld(row) else _cell(value, ACTIVITY_CELL_WIDTH) for column, value in shown.items()}
        for row, shown in zip(rows, projected, strict=True)
    ]
    return render_rows(cells, max_width=0)


def _withheld_state(row: Mapping[str, Any]) -> str:
    """What can still be said of a withheld backend's state: whether it is in a transaction."""
    in_transaction = row.get("in_transaction")
    if in_transaction is None:
        return ""
    return "in a transaction" if in_transaction else "not in a transaction"


def _withheld_details(row: Mapping[str, Any]) -> str:
    """What PostgreSQL still shows of a backend whose state, query and wait it withheld."""
    parts = ["state, query and wait withheld"]
    if row.get("locks"):
        parts.append(f"holds {', '.join(row['locks'])}")
    if row.get("blocking"):
        parts.append(f"blocking {', '.join(str(pid) for pid in row['blocking'])}")
    where = ", ".join(
        f"{label} {value}" for label, value in (("database", row.get("datname")), ("application", row.get("application_name"))) if value
    )
    if where:
        parts.append(where)
    return "; ".join(parts)


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
    parts = [f"{indent}pid {node.get('pid')}:"]
    waits_for = node.get("waits_for")
    if waits_for:
        parts.append(f"waits for {waits_for.get('description')};")
    if is_withheld(node):
        # Every role is shown every lock, so the backend is in the tree; its
        # state and timings are withheld, and only whether it is in a
        # transaction can be said.
        parts.append("; ".join(filter(None, [_withheld_state(node), "state, query and wait withheld"])))
    else:
        state = node.get("state") or node.get("backend_type") or "?"
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

    query = "" if is_withheld(node) else " ".join(str(node.get("query") or "").split())
    if query:
        lines.append(f"{indent}    {query[:MAX_QUERY_WIDTH]}{'…' if len(query) > MAX_QUERY_WIDTH else ''}")

    for child in node.get("blocking") or []:
        _lock_tree_lines(child, depth + 1, lines)


MAX_QUERY_WIDTH = 120


# -- Waits ---------------------------------------------------------------------


@dataclass(frozen=True)
class Diagnosed:
    """Rows to render, with what the waits in the same moment amount to beneath them.

    Only the text shows the diagnosis: ``--json`` keeps printing the rows
    the library returned, and :meth:`PgDebug.classify_waits` returns it.
    """

    rows: Any
    diagnosis: Any


def with_wait_diagnosis(render: Callable[[Any], str]) -> Callable[[Any], str]:
    """A renderer that also renders a :class:`Diagnosed` result's diagnosis, beneath the rows."""

    def rendered(result: Any) -> str:
        if isinstance(result, Diagnosed):
            diagnosis = render_wait_diagnosis(result.diagnosis)
            return f"{render(result.rows)}\n\n{diagnosis}" if diagnosis else render(result.rows)
        if isinstance(result, WaitProfile):
            return render_wait_profile(result)
        return render(result)

    rendered.__name__ = render.__name__
    rendered.__doc__ = render.__doc__
    return rendered


def render_wait_diagnosis(diagnosis: Any) -> str:
    """Render the patterns found in one snapshot's waits, with their evidence, and what was seen and not classified."""
    if not diagnosis.classifications and not diagnosis.declined:
        return ""
    lines = ["What the waits amount to:"]
    for found in diagnosis.classifications:
        lines.append(f"  [{found.severity.value}, {found.confidence.value}] {found.summary}")
        lines.append(f"      {found.detail}")
        if found.judgment:
            lines.append(f"      Judgment: {found.judgment}")
    for refusal in diagnosis.declined:
        lines.append(f"  Not classified: {refusal}")
    return "\n".join(lines)


def render_wait_profile(profile: Any) -> str:
    """Render a sampled wait profile: how it was sampled, AAS, then waits and queries by share."""
    timing = profile.timing
    achieved = f"{timing.achieved_interval_seconds}s" if timing.achieved_interval_seconds is not None else "not measured"
    lines = [
        f"{timing.samples} samples of {profile.source} over {timing.span_seconds}s, one every {achieved} "
        f"(asked: {timing.requested_interval_seconds}s; gaps {timing.shortest_gap_seconds}s to {timing.longest_gap_seconds}s).",
        f"Average active sessions: {profile.active_sessions}, {profile.active_client_sessions} of them client backends "
        f"(at most {profile.active_sessions_max} in one sample).",
        "",
    ]
    if profile.waits:
        lines.append(
            render_rows(
                [
                    {"wait": wait["wait"], "aas": wait["active_sessions"], "share": f"{wait['share'] * 100:.1f}%", "samples": wait["samples"]}
                    for wait in profile.waits
                ]
            )
        )
    else:
        lines.append("(no session was active in any sample)")
    if profile.queries:
        lines.append("")
        grouping = {
            "query_id": "query_id",
            "normalized_text": "text, constants replaced",
            "mixed": "query_id where the server reported one, text otherwise",
        }
        lines.append(f"By query (grouped by {grouping.get(profile.query_grouping, profile.query_grouping)}):")
        lines.append(
            render_rows(
                [
                    {
                        "aas": query["active_sessions"],
                        "share": f"{query['share'] * 100:.1f}%",
                        "top waits": ", ".join(f"{wait['wait']} {wait['share_of_query'] * 100:.0f}%" for wait in query["waits"]),
                        "query": query["query"] or f"({query['backend_type']})",
                    }
                    for query in profile.queries
                ],
                max_width=ACTIVITY_CELL_WIDTH,
            )
        )
    extension = profile.extension
    if extension:
        lines.append("")
        if extension.get("read"):
            lines.append(f"Also read from {extension['source']}:")
            aas = extension.get("active_sessions")
            if aas is not None:
                lines.append(f"  average active sessions {aas} over {extension.get('window_seconds')}s")
            for wait in extension.get("waits", [])[:10]:
                idle = " (idle)" if wait.get("idle") else ""
                lines.append(f"  {wait['wait']}: {wait['samples']} samples{idle}")
            lines.append(f"  {extension['note']}")
        else:
            lines.append(f"{extension['source']} is installed and was not read: {extension['reason']}.")
    lines.append("")
    if profile.query_grouping_reason:
        lines.append(profile.query_grouping_reason)
    lines.extend(profile.notes)
    lines.append("Every attribution above is a candidate: each sample is exact, the profile is a sample.")
    return "\n".join(lines)


def note_withheld_waits(access: StatisticsAccess, profile: Any) -> str | None:
    """What ``activity --sample`` could not count of the backends PostgreSQL withheld the state of, and why."""
    count = profile.withheld_per_sample
    if not count:
        return None
    return _noting(
        access,
        [
            f"Up to {count} backend{'s' if count != 1 else ''} in a sample {'was' if count == 1 else 'were'} shown without state, "
            "wait or query, so whether they were active cannot be told, and they are in no figure of the profile."
        ],
    )


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
            # Withheld, with the table and phase, from a role without
            # pg_read_all_stats: blank rather than "None/None".
            "heap_blks": f"{row.get('heap_blks_scanned')}/{row.get('heap_blks_total')}" if row.get("heap_blks_total") is not None else "",
            "index_passes": row.get("index_vacuum_count"),
            "running": format_duration(row.get("running_seconds")),
            "autovacuum": row.get("autovacuum"),
        }
        for row in rows
    ]
    return render_rows(projected)


def _rate_amount(name: str, value: float) -> str:
    """A counter's amount: bytes as a size, milliseconds as they are, the rest as a count."""
    if name.endswith("bytes"):
        sign = "-" if value < 0 else ""
        return sign + humanize.naturalsize(abs(value), binary=True)
    if name.endswith("_ms"):
        return f"{value:,.1f}ms"
    return f"{value:,.1f}" if value != int(value) else f"{int(value):,}"


def render_server_rates(rates: Any) -> str:
    """Render each view's counters as rates per second, and each refusal with its reason."""
    lines: list[str] = [f"Measured over {rates.interval_seconds:g}s (PostgreSQL {rates.server_version})."]
    for group in rates.groups:
        title = f"{group.measures}" + (f" {group.subject}" if group.subject else "") + f" ({group.source}"
        title += f", {group.interval_seconds:.1f}s)" if group.interval_seconds is not None else ")"
        details = [f"{key} {value}" for key, value in (group.details or {}).items() if value is not None]
        if details:
            title += f" [{', '.join(details)}]"
        lines.append("")
        lines.append(title)
        if group.refused:
            lines.append(f"  refused: {group.refused}")
            continue
        width = max((len(counter.name) for counter in group.counters), default=0)
        for counter in group.counters:
            if counter.refused:
                lines.append(f"  {counter.name.ljust(width)}  refused: {counter.refused}")
                continue
            rate, delta = _rate_amount(counter.name, counter.per_second), _rate_amount(counter.name, counter.delta)
            line = f"  {counter.name.ljust(width)}  {rate}/s  ({'+' if counter.delta >= 0 else ''}{delta})"
            if group.measures == "replication_slot":
                line += f", retaining {_rate_amount(counter.name, counter.after)}"
            lines.append(line)
    if rates.idle_io:
        lines.append("")
        lines.append(f"{rates.idle_io} pg_stat_io row{'s' if rates.idle_io != 1 else ''} did no I/O during the interval.")
    return "\n".join(lines)


def render_pooler_report(report: Any) -> str:
    """Render PgBouncer's pools, then the saturation they show, or that they show none."""
    config = ", ".join(f"{key} {value}" for key, value in report.config.items())
    lines = [f"{report.version or 'PgBouncer'}" + (f": {config}" if config else "")]
    lines.append("")
    pools = [
        {
            "pool": f"{pool.get('database')}/{pool.get('user')}",
            "mode": pool.get("pool_mode"),
            "cl_active": pool.get("cl_active"),
            "cl_waiting": pool.get("cl_waiting"),
            "sv_active": pool.get("sv_active"),
            "sv_idle": pool.get("sv_idle"),
            "sv_used": pool.get("sv_used"),
            "maxwait": format_duration(pool.get("maxwait")) if pool.get("maxwait") else "",
        }
        for pool in report.pools
    ]
    lines.append(render_rows(pools, empty="(no pools: no client has connected through PgBouncer since it started)"))
    if report.server:
        server = report.server
        lines.append(
            f"\nServer: max_connections {server.get('max_connections')}, {server.get('superuser_reserved_connections')} reserved for "
            f"superusers, {server.get('reserved_connections')} for pg_use_reserved_connections; "
            f"{server.get('client_backends')} client backends now."
        )
    for command, reason in report.unavailable.items():
        lines.append(f"\n{command} was refused: {reason}")
    lines.append("")
    if not report.findings:
        waiting = sum(pool.get("cl_waiting") or 0 for pool in report.pools)
        lines.append(f"No saturation: {len(report.pools)} pool(s) examined, {waiting} client(s) waiting.")
    for finding in report.findings:
        lines.append(f"[{finding.severity.value}] {finding.summary}")
    return "\n".join(lines)


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


#: How each check outcome reads in the list of what was examined.
_CHECK_STATUS_LABELS = {"clear": "clear", "found": "found", "unknown": "unknown", "not_applicable": "n/a"}


def render_audit_report(report: Any) -> str:
    """Render an upgrade check or a security audit: the findings, then what each check examined.

    The list of checks is the half that is easy to skip and must not be: a
    check that could not look is printed as such, and the last line says how
    many could not tell, so an empty list of findings never reads as a pass
    it was not.
    """
    if report.command == "upgrade-check":
        heading = f"Upgrade from PostgreSQL {report.server_version} to {report.target_version}"
    else:
        heading = f"Security audit of PostgreSQL {report.server_version}"
    lines: list[str] = [heading]

    for finding in report.findings:
        lines.append("")
        marker = "" if finding.confidence.value == "proven" else "  (candidate)"
        lines.append(f"[{finding.severity.value}] {finding.check}: {finding.subject}{marker}")
        lines.append(f"    {finding.summary}")
        lines.append(f"    {finding.detail}")
        if finding.verify:
            lines.append(f"    verify: {finding.verify}")
        for statement in finding.remediation:
            lines.append(f"    {statement}")

    lines.append("")
    lines.append("Checks:")
    for outcome in report.checks:
        label = _CHECK_STATUS_LABELS.get(outcome.status.value, outcome.status.value)
        lines.append(f"  {label:<8} {outcome.check}: {outcome.reason or outcome.examined}")
    for note in report.notes:
        lines.append("")
        lines.append(note)

    lines.append("")
    if report.findings:
        counts: dict[str, int] = {}
        for finding in report.findings:
            counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
        by_severity = ", ".join(f"{count} {severity}" for severity, count in counts.items())
        found = f"{len(report.findings)} finding{'' if len(report.findings) == 1 else 's'} ({by_severity})"
    else:
        found = "No findings"
    unknown = [outcome.check for outcome in report.unknown]
    if unknown:
        found += f"; {len(unknown)} check{' could' if len(unknown) == 1 else 's could'} not tell ({', '.join(unknown)}), so this is not a clean bill."
    else:
        found += "."
    lines.append(found)
    return "\n".join(lines)


def audit_report_json(report: Any, baseline: Baseline | None) -> dict[str, Any]:
    """The report as JSON, each finding marked ``known`` or ``new`` when there is a baseline."""
    payload = report.to_dict()
    payload["findings"] = [_with_baseline(finding.to_dict(), audit_finding_key(finding), baseline) for finding in report.findings]
    return payload


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
        lock_timeout = review.timeouts.get("lock_timeout")
        if review.lock_timeout_advice:
            lines.append(f"    {review.lock_timeout_advice}")
        elif lock_timeout and lock_timeout.get("milliseconds"):
            lines.append(f"    lock_timeout: {lock_timeout['value']} ({lock_timeout['source']})")
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
    shown = findings[:PLAN_FINDINGS_SHOWN]
    for finding in shown:
        where = f" on {finding.relation}" if finding.relation else ""
        confidence = getattr(finding, "confidence", None)
        marker = "  (candidate)" if confidence is not None and confidence.value == "candidate" else ""
        basis = str((finding.evidence or {}).get("share_of") or "")
        of_what = {"execution_time": "the execution time", "planning_and_execution_time": "planning plus execution"}.get(basis, "the query's work")
        lines.append("")
        lines.append(f"[{finding.severity.value}] {finding.issue.value} — {finding.node_type}{where}{marker}")
        lines.append(f"    accounts for {finding.share_of_time:.0%} of {of_what} ({format_duration(finding.cost_ms / 1000)})")
        lines.append(f"    {finding.summary}")
        lines.append(f"    {finding.detail}")
        if finding.remediation:
            lines.append("")
            for statement in finding.remediation:
                lines.append(f"      {statement}")
        plan_control = getattr(finding, "plan_control", None) or []
        if plan_control:
            lines.append("    to hold a plan while the cause is fixed (advice; pg-debug applies none of it):")
            for line in plan_control:
                lines.append(f"      - {line}")

    lines.append("")
    noun = "finding" if len(findings) == 1 else "findings"
    hidden = len(findings) - len(shown)
    more = f" The {hidden} smaller ones are not shown; --json lists every one." if hidden > 0 else ""
    lines.append(f"{len(findings)} {noun}. Shares are per finding and overlap — they are not a breakdown of the total.{more}")
    return "\n".join(lines).strip("\n")


#: How many plan findings the text output shows, costliest first. The rest
#: are in the JSON output.
PLAN_FINDINGS_SHOWN = 6


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


def render_shard_readiness(readiness: Any) -> str:
    """Render the shard readiness verdict: each refusal with its figure, then what was measured.

    The refusals come first because they are the answer; the measurements
    follow so that a reader can check them.
    """
    lines = [f"[{readiness.verdict.value}] {readiness.summary}"]
    for refusal in readiness.refusals:
        lines.append("")
        lines.append(f"  do not shard: {refusal.summary}")
        lines.append(f"    instead:    {refusal.instead}")
        lines.append(f"    changes if: {refusal.changes_if}")
    if readiness.pressure:
        lines.append("")
        lines.append("Pressing on the single primary:")
        lines.extend(f"  {line}" for line in readiness.pressure)

    measured = readiness.measurements
    lines.append("")
    lines.append(f"Measured over {measured.get('interval_seconds', 0):g} seconds:")
    wal = measured.get("wal") or {}
    if wal.get("source"):
        lines.append(f"  WAL written:        {humanize.naturalsize(wal.get('bytes_in_interval') or 0)} ({wal['source']})")
    io = measured.get("io") or {}
    if io.get("source"):
        lines.append(f"  buffers evicted:    {io.get('evictions_in_interval', 0):,}, blocks read {io.get('reads_in_interval', 0):,} (pg_stat_io)")
    checkpoints = measured.get("checkpoints") or {}
    if checkpoints:
        lines.append(
            f"  checkpoints:        {checkpoints.get('timed', 0):,} timed, {checkpoints.get('requested', 0):,} requested "
            f"since {checkpoints.get('counting_since') or 'an unrecorded time'}"
        )
    autovacuum = measured.get("autovacuum") or {}
    if autovacuum:
        lines.append(
            f"  autovacuum:         {'keeping up' if autovacuum.get('keeping_up') else 'behind'} ({autovacuum.get('tables_behind', 0)} tables behind)"
        )
    workload = measured.get("workload") or {}
    if workload:
        read_share, top_share = workload.get("read_share"), workload.get("top_share")
        lines.append(
            f"  workload:           {workload.get('statements_read', 0)} statements, counting since "
            f"{workload.get('counting_since') or 'an unrecorded time'}"
            + (f", {read_share:.0%} of the time in reads" if read_share is not None else "")
            + (f", top {workload.get('top_statements')} take {top_share:.0%}" if top_share is not None else "")
        )
    for key in readiness.routing_keys[:3]:
        lines.append(f"  routing key:        {key['column']} covers {key['share']:.0%} ({', '.join(key['tables'])})")

    if readiness.citus is not None:
        lines.append("")
        lines.append(f"Citus {readiness.citus.get('version')}:")
        findings = readiness.citus.get("findings") or []
        if not findings:
            lines.append("  nothing to report from its views")
        for finding in findings:
            lines.append(f"  [{finding.severity.value}, {finding.confidence.value}] {finding.summary}")
            if finding.verify:
                lines.append(f"    confirm with: {finding.verify}")
        for name, error in (readiness.citus.get("errors") or {}).items():
            lines.append(f"  could not read {name}: {error}")

    for note in readiness.notes:
        lines.append("")
        lines.append(f"Note: {note}")
    return "\n".join(lines)


def render_history(report: Any) -> str:
    """Render each history source: what it holds and how far back, or why it holds nothing."""
    lines = [f"Platform: {report.platform.get('platform')} ({report.platform.get('signal')})"]
    for source in report.sources:
        state = "not here" if not source.present else "capture off" if source.capturing is False else "read"
        lines.append("")
        lines.append(f"[{state}] {source.label}")
        if source.status:
            lines.append(f"    {source.status}")
        lines.append(f"    keeps: {source.retention}")
        if source.rows:
            lines.append(_indent(render_rows(source.rows), "    "))
    lines.append("")
    lines.append(report.note)
    return "\n".join(lines)


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# -- What PostgreSQL withheld from the role ---------------------------------
#
# A role without pg_read_all_stats is shown other roles' backends and
# statements, and the server's own processes, without their details. The
# library returns the rows as the server showed them. The command says what was
# withheld, and why, on standard error in both outputs, so that --json keeps
# printing the array scripts read.


@dataclass(frozen=True)
class Shown:
    """A command's rows as PostgreSQL showed them to the connecting role, and what it withheld.

    Attributes:
        rows: The rows as the library returned them, which ``--json`` prints.
        text: The rows the text renders when it has more to show than
            ``rows``: a withheld backend with the locks it holds and the
            backends waiting on it. None renders ``rows``.
        note: What was withheld and why, for standard error, or None.
    """

    rows: Any
    text: Any = None
    note: str | None = None


def _series(items: Iterable[Any]) -> str:
    """``1``, ``1 and 2``, ``1, 2 and 3``."""
    listed = [str(item) for item in items]
    return listed[0] if len(listed) == 1 else f"{', '.join(listed[:-1])} and {listed[-1]}"


#: How many roles a note names before it counts the rest.
NAMED_ROLES = 4


def _owners(rows: Iterable[Mapping[str, Any]]) -> str:
    """Whose backends these are: their roles, and the server itself for its own processes, which have none."""
    users = [row.get("usename") for row in rows]
    names = sorted({str(user) for user in users if user})
    if len(names) > NAMED_ROLES + 1:
        names = [*names[:NAMED_ROLES], f"{len(names) - NAMED_ROLES} other roles"]
    owners = [f"of {_series(names)}"] if names else []
    if any(not user for user in users):
        owners.append("of the server itself")
    return (", and " if len(names) > 1 else " and ").join(owners)


def _is(count: int) -> str:
    return "is" if count == 1 else "are"


def withheld_because(access: StatisticsAccess) -> str:
    """Why PostgreSQL withholds other roles' statistics from the role, and the statement that would show it them."""
    remedy = access.remedy or f"GRANT pg_monitor TO {access.role}"
    if access.member:
        # Granting it pg_monitor again would change nothing: from PostgreSQL
        # 15 the server asks for the privileges, not the membership.
        return (
            f"{access.role} is a member of pg_read_all_stats but does not inherit its privileges, which PostgreSQL 15 "
            f"and later ask for; {remedy} makes it inherit them"
        )
    return f"{access.role} lacks pg_read_all_stats, which pg_monitor includes; {remedy} grants it"


def _noting(access: StatisticsAccess, sentences: Sequence[str]) -> str | None:
    if not sentences:
        return None
    return " ".join([*sentences, f"That is because {withheld_because(access)}."])


def note_withheld_backends(
    access: StatisticsAccess,
    rows: Sequence[Mapping[str, Any]],
    withheld: Sequence[Mapping[str, Any]],
    *,
    include_idle: bool = False,
    min_duration: float | None = None,
    blocked_only: bool = False,
) -> str | None:
    """What ``activity`` could not show of the backends PostgreSQL withheld the details of, and why.

    Args:
        access: Whether the role is shown everything, and if not, why not.
        rows: The backends listed, as ``list_activity`` returned them.
        withheld: Every backend withheld from the role, as
            ``withheld_backends`` returned them.
        include_idle: Whether idle backends were asked for.
        min_duration: How long backends had to be in their state to be listed.
        blocked_only: Whether only backends waiting on a lock were asked for.
    """
    listed = [row for row in rows if is_withheld(row)]
    listed_pids = {row.get("pid") for row in listed}
    unlisted = [backend for backend in withheld if backend.get("pid") not in listed_pids]
    sentences: list[str] = []

    if blocked_only:
        if listed:
            sentences.append(f"{len(listed)} of these backends, {_owners(listed)}, {_is(len(listed))} shown without state, query, wait or timings.")
        # A blocker that does not wait itself is not listed; one whose
        # details are withheld is described here rather than left a pid.
        waited_on = {pid for row in rows for pid in row.get("blocked_by") or []}
        sentences.extend(_withheld_blocker(backend) for backend in unlisted if backend.get("pid") in waited_on)
        return _noting(access, sentences)

    if min_duration is not None:
        # How long a withheld backend has been in its state is withheld with
        # the state, so none can be listed as having been in it that long.
        if withheld:
            count = len(withheld)
            sentence = (
                f"{count} other backend{'s' if count != 1 else ''}, {_owners(withheld)}, {_is(count)} not listed: how long "
                f"{'it has' if count == 1 else 'they have'} been in {'its' if count == 1 else 'their'} state is withheld."
            )
            in_transaction = [backend.get("pid") for backend in withheld if backend.get("in_transaction")]
            if in_transaction:
                sentence += f" {'Pid' if len(in_transaction) == 1 else 'Pids'} {_series(in_transaction)} {_is(len(in_transaction))} in a transaction."
            sentences.append(sentence)
        return _noting(access, sentences)

    if include_idle:
        if listed:
            count = len(listed)
            quiet = sum(1 for row in listed if not row.get("in_transaction"))
            sentence = f"{count} of these backends, {_owners(listed)}, {_is(count)} shown without state, query, wait or timings"
            if not quiet:
                sentence += ", in a transaction." if count == 1 else ", all in a transaction."
            elif quiet == count:
                sentence += (
                    "; it shows no sign of a transaction, and whether it is running cannot be told."
                    if count == 1
                    else "; none shows a sign of a transaction, and whether they are running cannot be told."
                )
            else:
                sentence += (
                    f". {count - quiet} {_is(count - quiet)} in a transaction; for the other {quiet}, which show"
                    f"{'s' if quiet == 1 else ''} no sign of one, whether {'it is' if quiet == 1 else 'they are'} running "
                    "cannot be told."
                )
            sentences.append(sentence)
        return _noting(access, sentences)

    if listed:
        count = len(listed)
        their = "its" if count == 1 else "their"
        sentences.append(
            f"{count} of these backends, {_owners(listed)}, {_is(count)} in a transaction, and PostgreSQL shows {access.role} "
            f"{their} locks but not {their} state, query, wait or timings."
        )
    # Ones that entered a transaction between the two reads are neither
    # listed nor quiet, and are left to the next run.
    quiet = [backend for backend in unlisted if not backend.get("in_transaction")]
    if quiet:
        count = len(quiet)
        sentence = (
            f"{count} other backend{'s' if count != 1 else ''}, {_owners(quiet)}, {_is(count)} not listed: "
            f"{'it shows' if count == 1 else 'they show'} no sign of a transaction, and whether {'it is' if count == 1 else 'they are'} "
            f"running cannot be told; --all lists {'it' if count == 1 else 'them'}."
        )
        blockers = [backend for backend in quiet if backend.get("blocking")]
        if blockers:
            # A session-level advisory lock outlives the transaction that took it.
            sentence += " Even so, " + "; ".join(f"{backend.get('pid')} blocks {_series(backend['blocking'])}" for backend in blockers) + "."
        sentences.append(sentence)
    return _noting(access, sentences)


def _withheld_blocker(backend: Mapping[str, Any]) -> str:
    """A sentence on a withheld backend others wait on, with what PostgreSQL still shows of it."""
    who = f"{backend.get('usename') or 'the server'}@{backend.get('datname') or '?'}"
    if backend.get("application_name"):
        who += f" {backend['application_name']}"
    state = "in a transaction" if backend.get("in_transaction") else "not in a transaction"
    holds = f" and holds {', '.join(backend['locks'])}" if backend.get("locks") else ""
    return (
        f"Pid {backend.get('pid')} ({who}), which blocks {_series(backend.get('blocking') or [])}, is {state}{holds}; "
        "its state, query and wait are withheld."
    )


def note_withheld_lock_tree(access: StatisticsAccess, roots: Sequence[Mapping[str, Any]]) -> str | None:
    """What ``locks`` could not show of the backends in its trees, and why."""
    withheld = {node.get("pid"): node for node in lock_tree_nodes(roots) if is_withheld(node)}
    if not withheld:
        return None
    count = len(withheld)
    return _noting(
        access,
        [
            f"{count} of the backends in these trees, {_owners(withheld.values())}, {_is(count)} shown without state, query, "
            "wait or timings; who blocks whom is complete, since pg_locks shows every role's locks."
        ],
    )


def lock_tree_nodes(nodes: Iterable[Mapping[str, Any]]) -> Iterable[Mapping[str, Any]]:
    """Every node of these lock trees, each root before the backends waiting on it."""
    for node in nodes:
        yield node
        yield from lock_tree_nodes(node.get("blocking") or [])


def note_withheld_vacuums(access: StatisticsAccess, rows: Sequence[Mapping[str, Any]]) -> str | None:
    """What ``vacuum-progress`` could not show of the vacuums it lists, and why."""
    count = sum(1 for row in rows if is_withheld(row))
    if not count:
        return None
    return _noting(
        access,
        [
            f"{count} of these vacuums {_is(count)} shown without table, phase or progress, and whether "
            f"{'it is' if count == 1 else 'they are'} autovacuum cannot be told."
        ],
    )


def note_withheld_queries(access: StatisticsAccess, rows: Sequence[Mapping[str, Any]]) -> str | None:
    """What ``top-queries`` could not show of the statements it lists, and why."""
    count = sum(1 for row in rows if is_withheld(row))
    if not count:
        return None
    its = "its" if count == 1 else "their"
    whose = "is another role's" if count == 1 else "are other roles'"
    return _noting(access, [f"{count} of these queries {whose}: pg_stat_statements shows {access.role} {its} counters but not {its} text."])


def note_statement_window(windows: Mapping[str, Any]) -> str | None:
    """Since when ``top-queries`` totals have been counting, which they do not say themselves."""
    window = windows.get("statements")
    if window is None:
        return None
    advice = "--interval measures what runs now."
    if not window.get("counting_since"):
        return f"pg_stat_statements before 1.9 does not record when it was last reset, so these totals cover a span it cannot date; {advice}"
    since = datetime.fromisoformat(window["counting_since"])
    age = humanize.naturaldelta(timedelta(seconds=window.get("counting_seconds") or 0))
    note = f"These totals are counts since {since:%Y-%m-%d %H:%M %Z} ({age}), when pg_stat_statements was last reset; {advice}"
    evicted = window.get("statements_evicted")
    if evicted:
        note += (
            f" It has evicted statements {evicted:,} time{'s' if evicted != 1 else ''} since to make room "
            "(pg_stat_statements.max), so some of these have counted for less."
        )
    tables = windows.get("tables") or {}
    if tables.get("counting_began_with") == "restart" and tables.get("counting_since") and tables["counting_since"] > window["counting_since"]:
        # A crash does not move pg_stat_statements' stats_reset; it reloads
        # what it saved at the last clean shutdown, and loses what came after.
        discarded = datetime.fromisoformat(tables["counting_since"])
        note += (
            f" PostgreSQL discarded its statistics at {discarded:%Y-%m-%d %H:%M %Z} (a crash, an immediate shutdown, a failover "
            "or a restore), and pg_stat_statements then lost what it had recorded since its last clean shutdown."
        )
    return note


def sampled_queries_text(queries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The sampled queries with ``change`` in a word: ``new``, ``slower 4.2x``, ``faster 3.1x``, or blank."""

    def word(change: Mapping[str, Any] | None) -> str:
        if not change or not change.get("kind"):
            return ""
        if change["kind"] == "new":
            return "new"
        ratio = change.get("ratio") or 1
        return f"{change['kind']} {ratio if ratio >= 1 else 1 / ratio:.1f}x"

    return [{**row, "change": word(row.get("change"))} for row in queries]


def note_withheld_sample(access: StatisticsAccess, withheld: int, sort_by: str) -> str | None:
    """What ``top-queries --interval`` left out of its ranking, and why."""
    if not withheld:
        return None
    its = "its" if withheld == 1 else "their"
    sentence = (
        f"{withheld} statement{'s' if withheld != 1 else ''} of other roles ran during the interval and {_is(withheld)} not "
        f"ranked: pg_stat_statements shows {access.role} neither {its} text nor {its} queryid, so {its} counters cannot be "
        "followed from one snapshot to the next."
    )
    if sort_by == "resources":
        sentence += " The resource shares are of the ranked statements alone."
    return _noting(access, [sentence])


__all__ = [
    "Diagnosed",
    "Shown",
    "format_duration",
    "lock_tree_nodes",
    "note_withheld_backends",
    "note_withheld_lock_tree",
    "note_withheld_queries",
    "note_withheld_sample",
    "note_withheld_vacuums",
    "note_withheld_waits",
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
    "render_pooler_report",
    "render_rows",
    "render_server_rates",
    "render_vacuum_progress",
    "render_wait_diagnosis",
    "render_wait_profile",
    "sampled_queries_text",
    "to_json",
    "with_wait_diagnosis",
    "withheld_because",
]

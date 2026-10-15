"""What each pg-debug command does: one handler per command, calling the client."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from typing import TYPE_CHECKING
from typing import Any

from ..db.privileges import is_withheld
from ..errors import InvalidArgumentError
from .render import Diagnosed
from .render import Shown
from .render import lock_tree_nodes
from .render import note_statement_window
from .render import note_withheld_backends
from .render import note_withheld_lock_tree
from .render import note_withheld_queries
from .render import note_withheld_sample
from .render import note_withheld_vacuums
from .render import note_withheld_waits
from .streams import read_sql
from .streams import read_sql_or_file
from .streams import read_stdin

if TYPE_CHECKING:
    from ..client import PgDebug
    from ..db.privileges import StatisticsAccess
    from ..waits import WaitDiagnosis


async def cmd_schemas(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.list_schemas()


async def cmd_objects(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.list_objects(args.schema, args.object_type)


async def cmd_describe(pg: PgDebug, args: argparse.Namespace) -> Any:
    schema, _, name = args.name.rpartition(".")
    return await pg.get_object_details(schema or args.schema, name, args.object_type)


async def cmd_execute(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.execute_sql(read_sql(args.sql))


async def cmd_explain(pg: PgDebug, args: argparse.Namespace) -> Any:
    plan = await pg.explain_query(
        read_sql(args.sql),
        analyze=args.analyze,
        hypothetical_indexes=args.hypothetical_indexes,
    )
    return plan


async def cmd_diagnose(pg: PgDebug, args: argparse.Namespace) -> Any:
    if args.plan:
        return await pg.diagnose_plan(read_sql_or_file(args.sql), min_ratio=args.min_ratio)
    return await pg.diagnose_query(read_sql_or_file(args.sql), min_ratio=args.min_ratio)


async def cmd_index_use(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.explain_index_use(read_sql_or_file(args.sql), index=args.index)


async def cmd_column_stats(pg: PgDebug, args: argparse.Namespace) -> Any:
    return {
        "columns": await pg.column_stats(args.table),
        "extended_statistics": await pg.extended_statistics(args.table),
    }


async def cmd_activity(pg: PgDebug, args: argparse.Namespace) -> Any:
    if args.sample is not None:
        return await _sample_waits(pg, args)
    shown = await _list_activity(pg, args)
    return _diagnosed(shown, await pg.classify_waits())


async def _sample_waits(pg: PgDebug, args: argparse.Namespace) -> Any:
    if args.include_idle or args.min_duration is not None or args.blocked:
        raise InvalidArgumentError(
            "--sample reports what active sessions waited on over an interval; --all, --min-duration and --blocked choose "
            "backends for a list of them at one instant, and do not combine with it."
        )
    profile = await pg.sample_waits(args.sample, interval=args.sample_interval)
    access = profile.statistics_access
    if access is None or access.reads_all_stats:
        return profile
    return Shown(profile, note=note_withheld_waits(access, profile))


def _diagnosed(result: Any, diagnosis: WaitDiagnosis) -> Any:
    """The result, with what the waits in it amount to for the text to show beneath it."""
    if not diagnosis.classifications and not diagnosis.declined:
        return result
    if isinstance(result, Shown):
        return Shown(result.rows, text=Diagnosed(result.rows if result.text is None else result.text, diagnosis), note=result.note)
    return Shown(result, text=Diagnosed(result, diagnosis))


async def _list_activity(pg: PgDebug, args: argparse.Namespace) -> Any:
    rows = await pg.list_activity(
        include_idle=args.include_idle,
        min_duration_seconds=args.min_duration,
        blocked_only=args.blocked,
    )
    # Whether something was withheld shows in the rows only when every
    # backend is listed. Otherwise the server is asked, once; a role shown
    # everything is asked nothing more.
    if args.blocked and not rows:
        return rows
    if args.include_idle and args.min_duration is None and not args.blocked and not any(is_withheld(row) for row in rows):
        return rows
    access = await pg.statistics_access()
    if access.reads_all_stats:
        return rows
    withheld = await pg.withheld_backends()
    by_pid = {backend.get("pid"): backend for backend in withheld}
    # The text fills in what the server still shows of a withheld backend;
    # --json prints the rows as the library returned them.
    text = [
        {**row, "locks": by_pid[row["pid"]].get("locks"), "blocking": by_pid[row["pid"]].get("blocking")}
        if is_withheld(row) and row.get("pid") in by_pid
        else row
        for row in rows
    ]
    note = note_withheld_backends(access, rows, withheld, include_idle=args.include_idle, min_duration=args.min_duration, blocked_only=args.blocked)
    return Shown(rows, text=text, note=note)


async def cmd_locks(pg: PgDebug, args: argparse.Namespace) -> Any:
    roots = await pg.lock_tree()
    shown = await _noting(pg, roots, lock_tree_nodes(roots), note_withheld_lock_tree)
    return _diagnosed(shown, await pg.classify_waits())


async def cmd_vacuum_progress(pg: PgDebug, args: argparse.Namespace) -> Any:
    vacuums = await pg.vacuum_progress()
    return await _noting(pg, vacuums, vacuums, note_withheld_vacuums)


async def _noting(
    pg: PgDebug,
    result: Any,
    rows: Iterable[Mapping[str, Any]],
    note: Callable[[StatisticsAccess, Any], str | None],
) -> Any:
    """The result, with what PostgreSQL withheld of its rows from the role and why.

    The server is asked why only when a row shows it withheld something, so a
    role that is shown everything is asked nothing more.
    """
    if not any(is_withheld(row) for row in rows):
        return result
    access = await pg.statistics_access()
    if access.reads_all_stats:
        return result
    return Shown(result, note=note(access, result))


def _also_noting(result: Any, note: str | None) -> Any:
    """The result with one more note for standard error."""
    if note is None:
        return result
    if isinstance(result, Shown):
        return Shown(result.rows, text=result.text, note=f"{result.note}\n{note}" if result.note else note)
    return Shown(result, note=note)


async def cmd_health(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.analyze_db_health(args.health_type, min_window_days=args.min_window_days)


async def cmd_config(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.review_configuration()


async def cmd_upgrade_check(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.check_upgrade(to=args.to)


async def cmd_security(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.audit_security()


async def cmd_partitions(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.suggest_partitions(
        min_table_bytes=args.min_table_bytes,
        min_pruning_share=args.min_pruning_share,
        limit=args.limit,
    )


async def cmd_shard_readiness(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.assess_shard_readiness(interval_seconds=args.interval)


async def cmd_history(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.read_history(limit=args.limit)


async def cmd_check_migration(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.check_migration(read_sql_or_file(args.sql))


async def cmd_model(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.review_data_model(
        rules=args.rules,
        schema=args.schema,
        min_rows=args.min_rows,
        proven_only=args.proven_only,
    )


async def cmd_top_queries(pg: PgDebug, args: argparse.Namespace) -> Any:
    if args.interval is None:
        queries = await pg.get_top_queries(sort_by=args.sort_by, limit=args.limit)
        shown = await _noting(pg, queries, queries, note_withheld_queries)
        windows = await pg.counting_windows()
        return _also_noting(shown, note_statement_window(windows))
    sample = await pg.sample_top_queries(args.interval, sort_by=args.sort_by, limit=args.limit)
    if not sample.withheld:
        return sample.queries
    access = await pg.statistics_access()
    if access.reads_all_stats:
        return sample.queries
    return Shown(sample.queries, note=note_withheld_sample(access, sample.withheld, args.sort_by))


async def cmd_tune_workload(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.analyze_workload_indexes(max_index_size_mb=args.max_index_size_mb, method=args.method)


async def cmd_tune_queries(pg: PgDebug, args: argparse.Namespace) -> Any:
    queries: list[str] = []
    for query in args.queries:
        if query == "-":
            queries.extend(line.strip() for line in read_stdin().splitlines() if line.strip())
        else:
            queries.append(query)
    return await pg.analyze_query_indexes(queries, max_index_size_mb=args.max_index_size_mb, method=args.method)

"""What each pg-debug command does: one handler per command, calling the client."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING
from typing import Any

from .streams import read_sql
from .streams import read_sql_or_file
from .streams import read_stdin

if TYPE_CHECKING:
    from ..client import PgDebug


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
    return await pg.list_activity(
        include_idle=args.include_idle,
        min_duration_seconds=args.min_duration,
        blocked_only=args.blocked,
    )


async def cmd_locks(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.lock_tree()


async def cmd_vacuum_progress(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.vacuum_progress()


async def cmd_health(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.analyze_db_health(args.health_type)


async def cmd_config(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.review_configuration()


async def cmd_partitions(pg: PgDebug, args: argparse.Namespace) -> Any:
    return await pg.suggest_partitions(
        min_table_bytes=args.min_table_bytes,
        min_pruning_share=args.min_pruning_share,
        limit=args.limit,
    )


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
    return await pg.get_top_queries(sort_by=args.sort_by, limit=args.limit, interval_seconds=args.interval)


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

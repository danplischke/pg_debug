"""Reviewing a migration before it runs.

The other diagnostics in pg_debug look at a database as it is. This one looks
at a change you are about to make to it and says what the change will do to
everyone else using the database while it runs.

The interesting question is rarely "which lock" on its own. ACCESS EXCLUSIVE
held for a catalog update lasts microseconds and nobody notices; the same lock
held while a large table is rewritten is an outage. So each statement is
reported with the lock it takes, how long it holds it, and — where a
connection is available — how big the table it holds it on actually is. That
last part is what an offline linter cannot tell you, and it is usually what
decides whether a migration is fine or needs rewriting.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from pglast import ast
from pglast import parse_sql
from pglast.enums import AlterTableType
from pglast.enums import ConstrType
from pglast.enums import ObjectType
from pglast.parser import ParseError
from pglast.stream import RawStream

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..errors import InvalidArgumentError
from ..partition.recipes import attach_partition_recipe
from ..severity import SEVERITY_ORDER
from ..severity import Severity
from .locks import Duration
from .locks import LockLevel

logger = logging.getLogger(__name__)

#: Above this, a statement whose lock is held for a scan or a rewrite stops
#: being a slow moment and starts being an incident. Chosen to be roughly the
#: point at which a rewrite takes longer than a deployment is usually willing
#: to wait, not from any property of the server.
LARGE_TABLE_BYTES = 1024 * 1024 * 1024

#: How many partitions a partitioned index's recipe spells out before it says
#: "and the same for the rest".
RECIPE_PARTITIONS = 5

#: PostgreSQL truncates an identifier to this many bytes.
MAX_IDENTIFIER_LENGTH = 63

#: Types that can be widened without touching the rows, because the on-disk
#: representation does not change. varchar(n) -> varchar(m > n) and
#: varchar -> text are the two that come up constantly in real migrations.
_BINARY_COERCIBLE = {
    ("varchar", "text"),
    ("varchar", "varchar"),
    ("text", "varchar"),
    ("bpchar", "text"),
}

#: Subcommands that only touch the catalog. Measured: each takes ACCESS
#: EXCLUSIVE and leaves relfilenode alone, so the lock is held for no time at
#: all whatever the table's size.
_INSTANT_ALTERS: dict[Any, tuple[str, str]] = {
    AlterTableType.AT_DropColumn: ("drop_column", "DROP COLUMN"),
    AlterTableType.AT_DropNotNull: ("drop_not_null", "DROP NOT NULL"),
    AlterTableType.AT_ColumnDefault: ("set_default", "Setting a column default"),
    AlterTableType.AT_DropConstraint: ("drop_constraint", "DROP CONSTRAINT"),
}


@dataclass
class StatementReview:
    """What one statement in a migration will do while it runs.

    Attributes:
        statement: The statement, as written.
        kind: A stable identifier for what it is, e.g. ``alter_column_type``.
        lock: The lock it takes on its table.
        blocks: What that lock stops the application doing.
        duration: Whether the lock is held for an instant or for as long as the
            table takes to read or rewrite.
        rewrites_table: Whether the heap is written out again.
        table: The table it affects, as written, or ``None``.
        table_bytes: How big that table is, when a connection was available and
            the table already exists. ``None`` otherwise — a table created
            earlier in the same migration has no size yet.
        severity: How much this one warrants attention.
        summary: One sentence.
        detail: What happens, in terms of this table.
        safer: A sequence that achieves the same thing without the lock, where
            one exists. Empty where the statement is already safe, or where
            there is no way around it.
    """

    statement: str
    kind: str
    lock: LockLevel
    duration: Duration
    rewrites_table: bool
    severity: Severity
    summary: str
    detail: str
    table: str | None = None
    table_bytes: int | None = None
    safer: list[str] = field(default_factory=list)
    #: Functions the column default calls, when there is one and it is not a
    #: constant. Resolved against pg_proc to decide whether it really rewrites.
    default_functions: list[str] = field(default_factory=list)

    @property
    def blocks(self) -> str:
        return self.lock.blocks

    def to_dict(self) -> dict[str, Any]:
        """Return the review as a JSON-friendly dict."""
        result: dict[str, Any] = {
            "statement": self.statement,
            "kind": self.kind,
            "severity": self.severity.value,
            "lock": self.lock.value,
            "blocks": self.blocks,
            "duration": self.duration.value,
            "rewrites_table": self.rewrites_table,
            "summary": self.summary,
            "detail": self.detail,
        }
        if self.table is not None:
            result["table"] = self.table
        if self.table_bytes is not None:
            result["table_bytes"] = self.table_bytes
        if self.safer:
            result["safer"] = self.safer
        return result


class MigrationLinter:
    """Reads a migration and reports what each statement will lock, and for how long."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver | None = None):
        """
        Args:
            sql_driver: Optional. Given one, the review reports the real size of
                each table it touches, which is what decides whether a rewrite
                is a moment or an outage. Without one the review is still
                correct, just unquantified.
        """
        self.sql_driver = sql_driver
        # The statement behind each index review, to review it again once the
        # catalog says whether its table is partitioned.
        self._index_statements: dict[int, ast.IndexStmt] = {}
        # Each partitioned table's partitions, with whether each is itself
        # partitioned; a table not partitioned is not in it.
        self._partitions: dict[str, list[tuple[str, bool]]] = {}
        # The tables the catalog was asked about and answered for.
        self._known_tables: set[str] = set()
        # What the migration does before each statement, in the order written:
        # the tables it creates, the ones it writes rows into, and the ones
        # given a validated CHECK constraint, which is what lets ATTACH
        # PARTITION skip its scan. CHECK constraints added NOT VALID wait here
        # until a VALIDATE CONSTRAINT names them.
        self._created: set[str] = set()
        self._written: set[str] = set()
        self._validated_checks: set[str] = set()
        self._unvalidated_checks: dict[tuple[str, str], bool] = {}
        # Each partition statement's parent, its partition and its bound, to
        # weigh again once the catalog says whether the parent has a DEFAULT
        # partition: (parent, partition, bound, kind of statement).
        self._partition_statements: dict[int, tuple[str, str, Any, str]] = {}
        # Each partitioned parent's DEFAULT partition and its size, and its
        # partition key when that is one plain column of a range.
        self._defaults: dict[str, tuple[str, int]] = {}
        self._range_keys: dict[str, str] = {}

    async def review(self, sql: str) -> list[StatementReview]:
        """Review every statement in a migration.

        Args:
            sql: The migration, as SQL. Multiple statements are fine.

        Returns:
            One :class:`StatementReview` per statement, most severe first,
            keeping the order written for statements of equal severity.

        Raises:
            InvalidArgumentError: If the SQL cannot be parsed.
        """
        if not sql or not sql.strip():
            raise InvalidArgumentError("No SQL provided to review.")

        try:
            parsed = parse_sql(sql)
        except ParseError as e:
            raise InvalidArgumentError(f"Could not parse the migration: {e}") from e

        self._index_statements = {}
        self._partitions = {}
        self._known_tables = set()
        self._created, self._written, self._validated_checks = set(), set(), set()
        self._unvalidated_checks = {}
        self._partition_statements = {}
        self._defaults, self._range_keys = {}, {}
        reviews = [self._review_statement(raw, sql) for raw in parsed]
        reviews = [review for review in reviews if review is not None]
        await self._resolve_default_volatility(reviews)
        await self._add_table_sizes(reviews)
        reviews = [self._settle_index(review) for review in reviews]
        reviews = [self._settle_partition(review) for review in reviews]
        for review in reviews:
            self._weigh(review)

        # Stable within a severity, so the migration's own order survives.
        return sorted(reviews, key=lambda review: SEVERITY_ORDER[review.severity])

    async def _resolve_default_volatility(self, reviews: list[StatementReview]) -> None:
        """Settle whether a computed column default really rewrites the table.

        Only a *volatile* default forces the rewrite — ``now()`` is stable and
        costs nothing, ``clock_timestamp()`` and ``random()`` are volatile and
        cost everything. That distinction is in pg_proc, so with a connection
        the answer is definite instead of conditional.
        """
        if self.sql_driver is None:
            return

        names = sorted({name for review in reviews for name in review.default_functions})
        if not names:
            return

        try:
            rows = await SafeSqlDriver.execute_param_query(
                self.sql_driver,
                """
                SELECT p.proname AS name, bool_or(p.provolatile = 'v') AS volatile
                FROM pg_proc p
                WHERE p.proname = ANY({}::text[])
                GROUP BY p.proname
                """,
                [names],
            )
        except Exception as e:
            logger.debug("Could not resolve default volatility: %s", e)
            return

        volatility = {row.cells["name"]: bool(row.cells["volatile"]) for row in rows or []}
        for review in reviews:
            if not review.default_functions:
                continue
            unknown = [name for name in review.default_functions if name not in volatility]
            if unknown:
                continue
            called = ", ".join(f"{name}()" for name in sorted(review.default_functions))
            if any(volatility[name] for name in review.default_functions):
                review.detail = (
                    f"{called} is volatile, so the default has to be evaluated for every existing row, "
                    f"and the table is rewritten under an ACCESS EXCLUSIVE lock."
                )
            else:
                review.kind = "add_column"
                review.duration = Duration.INSTANT
                review.rewrites_table = False
                review.safer = []
                review.summary = "ADD COLUMN takes an ACCESS EXCLUSIVE lock, briefly."
                review.detail = (
                    f"No rewrite: {called} is not volatile, so PostgreSQL stores the default once in the catalog rather than evaluating it per row."
                )

    # -- Sizing -------------------------------------------------------------

    async def _add_table_sizes(self, reviews: list[StatementReview]) -> None:
        """Fill in how big each table actually is, if we can see the database."""
        if self.sql_driver is None:
            return

        parents = {parent for parent, _partition, _bound, _kind in self._partition_statements.values()}
        tables = sorted({review.table for review in reviews if review.table} | parents)
        if not tables:
            return

        try:
            # A partitioned table has no storage of its own: its size is its
            # partitions', and an index on it is built on each of them.
            rows = await SafeSqlDriver.execute_param_query(
                self.sql_driver,
                """
                SELECT t.name AS name,
                       CASE WHEN c.relkind = 'p'
                            THEN (SELECT coalesce(sum(pg_total_relation_size(p.relid)), 0)
                                  FROM pg_partition_tree(c.oid) AS p WHERE p.isleaf)
                            ELSE pg_total_relation_size(c.oid) END AS bytes,
                       c.relkind = 'p' AS partitioned,
                       (SELECT array_agg(i.inhrelid::regclass::text ORDER BY i.inhrelid::regclass::text)
                        FROM pg_inherits AS i WHERE i.inhparent = c.oid) AS partitions,
                       (SELECT array_agg(pc.relkind = 'p' ORDER BY i.inhrelid::regclass::text)
                        FROM pg_inherits AS i JOIN pg_class AS pc ON pc.oid = i.inhrelid
                        WHERE i.inhparent = c.oid) AS partitions_partitioned,
                       (SELECT format('%I.%I', dn.nspname, d.relname)
                        FROM pg_partitioned_table AS pt
                        JOIN pg_class AS d ON d.oid = pt.partdefid
                        JOIN pg_namespace AS dn ON dn.oid = d.relnamespace
                        WHERE pt.partrelid = c.oid) AS default_partition,
                       (SELECT pg_relation_size(pt.partdefid)
                        FROM pg_partitioned_table AS pt
                        WHERE pt.partrelid = c.oid AND pt.partdefid <> 0) AS default_bytes,
                       (SELECT quote_ident(a.attname)
                        FROM pg_partitioned_table AS pt
                        JOIN pg_attribute AS a ON a.attrelid = pt.partrelid AND a.attnum = pt.partattrs[0]
                        WHERE pt.partrelid = c.oid AND pt.partstrat = 'r' AND pt.partnatts = 1) AS range_key
                FROM unnest({}::text[]) AS t(name)
                JOIN pg_class c ON c.oid = to_regclass(t.name)
                """,
                [tables],
            )
        except Exception as e:
            # A migration is worth reviewing even against a database that will
            # not answer questions about it.
            logger.debug("Could not size the tables in the migration: %s", e)
            return

        sizes: dict[str, int] = {}
        for row in rows or []:
            name = row.cells["name"]
            sizes[name] = int(row.cells["bytes"])
            self._known_tables.add(name)
            if row.cells["partitioned"]:
                names = row.cells["partitions"] or []
                nested = row.cells["partitions_partitioned"] or [False] * len(names)
                self._partitions[name] = list(zip(names, nested, strict=True))
            if row.cells.get("default_partition"):
                self._defaults[name] = (row.cells["default_partition"], int(row.cells.get("default_bytes") or 0))
            if row.cells.get("range_key"):
                self._range_keys[name] = row.cells["range_key"]

        for review in reviews:
            if review.table in sizes:
                review.table_bytes = sizes[review.table]

    def _settle_index(self, review: StatementReview) -> StatementReview:
        """Review an index build again, now that the catalog says whether its table is partitioned."""
        stmt = self._index_statements.get(id(review))
        if stmt is None or review.table not in self._known_tables:
            return review
        settled = self._index(stmt, review.statement, partitions=self._partitions.get(review.table, []), known=True)
        settled.table_bytes = review.table_bytes
        return settled

    def _weigh(self, review: StatementReview) -> None:
        """Decide how much a statement matters, and say why in its detail."""
        if review.kind == "create_index_concurrently_partitioned":
            # It takes no lock worth weighing: it fails, and the migration with it.
            review.severity = Severity.HIGH
        elif review.lock is LockLevel.SHARE_UPDATE_EXCLUSIVE:
            # Blocks no reads and no writes, so its duration does not matter to
            # anyone but the next migration.
            review.severity = Severity.LOW
        elif review.duration is Duration.INSTANT:
            # The lock is real but held for a catalog update. Worth a lock
            # timeout so it cannot queue behind a long query, nothing more.
            review.severity = Severity.LOW
        elif review.lock is LockLevel.ACCESS_EXCLUSIVE:
            review.severity = Severity.HIGH
        else:
            review.severity = Severity.MEDIUM

        if review.table_bytes is not None and review.duration is not Duration.INSTANT:
            size = _humanize(review.table_bytes)
            review.detail += (
                f" {review.table} is {size} today, which is how much has to be {'rewritten' if review.rewrites_table else 'read'} under that lock."
            )
            if review.table_bytes < LARGE_TABLE_BYTES and review.severity is Severity.HIGH:
                review.severity = Severity.MEDIUM
                review.detail += " At that size it is a pause rather than an outage."

    # -- Classification -----------------------------------------------------

    def _review_statement(self, raw: Any, sql: str) -> StatementReview | None:
        stmt = raw.stmt if isinstance(raw, ast.RawStmt) else raw
        text = _statement_text(raw, sql)

        if isinstance(stmt, ast.CreateStmt):
            return self._create_table(stmt, text)
        if isinstance(stmt, (ast.InsertStmt, ast.CopyStmt)):
            name = _relation_name(stmt.relation)
            if name:
                self._written.add(name)
            return None
        if isinstance(stmt, ast.IndexStmt):
            return self._index(stmt, text)
        if isinstance(stmt, ast.DropStmt):
            return self._drop(stmt, text)
        if isinstance(stmt, ast.AlterTableStmt):
            return self._alter_table(stmt, text)
        if isinstance(stmt, ast.TruncateStmt):
            return _review(
                text,
                "truncate",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.INSTANT,
                rewrites=True,
                table=_first_relation(stmt.relations),
                summary="TRUNCATE takes an ACCESS EXCLUSIVE lock and discards every row.",
                detail="The lock itself is brief, but nothing can read the table while it is held, and the rows are gone.",
            )
        if isinstance(stmt, ast.ClusterStmt):
            return _review(
                text,
                "cluster",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.REWRITE,
                rewrites=True,
                table=_relation_name(stmt.relation),
                summary="CLUSTER rewrites the table under an ACCESS EXCLUSIVE lock.",
                detail="Nothing can read or write the table until it finishes.",
                safer=["-- pg_repack rewrites a table without holding the table locked throughout."],
            )
        if isinstance(stmt, ast.VacuumStmt):
            return self._vacuum(stmt, text)
        if isinstance(stmt, ast.RenameStmt):
            return _review(
                text,
                "rename",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.INSTANT,
                rewrites=False,
                table=_relation_name(stmt.relation),
                summary="A rename takes an ACCESS EXCLUSIVE lock, briefly.",
                detail=(
                    "The lock is a catalog update and lasts no time at all. The risk is not the rename, it is that "
                    "anything still referring to the old name breaks the moment it commits."
                ),
            )

        # Anything else — CREATE TABLE, INSERT, a function definition — has no
        # lock worth reporting on an existing table.
        return None

    def _index(
        self,
        stmt: ast.IndexStmt,
        text: str,
        partitions: list[tuple[str, bool]] | None = None,
        known: bool = False,
    ) -> StatementReview:
        """An index build.

        Args:
            partitions: The table's partitions, when the catalog was read and
                it is partitioned; empty when it was read and is not.
            known: Whether the catalog was read for this table.
        """
        table = _relation_name(stmt.relation)
        only = stmt.relation is not None and stmt.relation.inh is False
        partitioned = bool(partitions)
        if only and (partitioned or not known):
            review = _review(
                text,
                "create_index_on_only",
                LockLevel.SHARE,
                Duration.INSTANT,
                rewrites=False,
                table=table,
                summary="CREATE INDEX ON ONLY creates a partitioned table's index without building one.",
                detail=(
                    "It is a catalog update, and the index stays invalid, unused by the planner, until an index on "
                    "every partition is attached to it. Build each partition's with CREATE INDEX CONCURRENTLY, which "
                    "blocks no writes, and attach it with ALTER INDEX ... ATTACH PARTITION. On a table that is not "
                    "partitioned, ONLY changes nothing and the index is built as usual."
                ),
                safer=_partitioned_index_recipe(stmt, partitions or []) if partitioned else [],
            )
            self._index_statements[id(review)] = stmt
            return review
        if partitioned and stmt.concurrent:
            review = _review(
                text,
                "create_index_concurrently_partitioned",
                LockLevel.SHARE_UPDATE_EXCLUSIVE,
                Duration.INSTANT,
                rewrites=False,
                table=table,
                summary="PostgreSQL refuses CREATE INDEX CONCURRENTLY on a partitioned table, so this statement fails.",
                detail=(
                    f"{table} is partitioned. Create the index ON ONLY the parent, which builds nothing, then build each "
                    "partition's CONCURRENTLY and attach it."
                ),
                safer=_partitioned_index_recipe(stmt, partitions or []),
            )
            self._index_statements[id(review)] = stmt
            return review
        if partitioned:
            review = _review(
                text,
                "create_index_partitioned",
                LockLevel.SHARE,
                Duration.SCAN,
                rewrites=False,
                table=table,
                summary="CREATE INDEX on a partitioned table builds an index on every partition, blocking writes to each.",
                detail=(
                    f"{table} has {len(partitions or [])} partitions. The build takes each one's SHARE lock in turn and "
                    "holds them all until the statement ends, so writes to a partition already built wait for the "
                    "rest. CONCURRENTLY is refused on a partitioned table; the way round is ON ONLY on the parent and a "
                    "concurrent build on each partition."
                ),
                safer=_partitioned_index_recipe(stmt, partitions or []),
            )
            self._index_statements[id(review)] = stmt
            return review
        review = self._plain_index(stmt, text, table)
        if not known:
            review.detail += (
                " On a partitioned table CONCURRENTLY is refused and the build runs on every partition; reviewed "
                "against the database, the review says which this is and gives the per-partition steps."
            )
        self._index_statements[id(review)] = stmt
        return review

    def _plain_index(self, stmt: ast.IndexStmt, text: str, table: str | None) -> StatementReview:
        if stmt.concurrent:
            return _review(
                text,
                "create_index_concurrently",
                LockLevel.SHARE_UPDATE_EXCLUSIVE,
                Duration.SCAN,
                rewrites=False,
                table=table,
                summary="CREATE INDEX CONCURRENTLY builds the index without blocking reads or writes.",
                detail=(
                    "It takes longer and scans the table twice, and it cannot run inside a transaction — so it "
                    "cannot be part of a migration that is applied atomically. If it fails it leaves an invalid "
                    "index behind, which has to be dropped before retrying."
                ),
            )
        return _review(
            text,
            "create_index",
            LockLevel.SHARE,
            Duration.SCAN,
            rewrites=False,
            table=table,
            summary="CREATE INDEX blocks writes to the table until the index is built.",
            detail="Reads continue throughout. Writes wait for the whole build.",
            safer=[text.rstrip(";").replace("CREATE INDEX", "CREATE INDEX CONCURRENTLY", 1) + ";"],
        )

    def _drop(self, stmt: ast.DropStmt, text: str) -> StatementReview | None:
        if stmt.removeType == ObjectType.OBJECT_INDEX:
            if stmt.concurrent:
                return _review(
                    text,
                    "drop_index_concurrently",
                    LockLevel.SHARE_UPDATE_EXCLUSIVE,
                    Duration.INSTANT,
                    rewrites=False,
                    summary="DROP INDEX CONCURRENTLY does not block reads or writes.",
                    detail="It cannot run inside a transaction block.",
                )
            return _review(
                text,
                "drop_index",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.INSTANT,
                rewrites=False,
                summary="DROP INDEX takes an ACCESS EXCLUSIVE lock on the table, briefly.",
                detail="The drop itself is a catalog update; the exposure is waiting for the lock behind a long query.",
                safer=[text.rstrip(";").replace("DROP INDEX", "DROP INDEX CONCURRENTLY", 1) + ";"],
            )
        if stmt.removeType == ObjectType.OBJECT_TABLE:
            return _review(
                text,
                "drop_table",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.INSTANT,
                rewrites=False,
                summary="DROP TABLE takes an ACCESS EXCLUSIVE lock and removes the table.",
                detail="Brief, and irreversible once committed.",
            )
        return None

    def _vacuum(self, stmt: ast.VacuumStmt, text: str) -> StatementReview:
        full = any(getattr(option, "defname", None) == "full" for option in (stmt.options or ()))
        table = _first_relation([item.relation for item in (stmt.rels or ()) if item.relation is not None])
        if full:
            return _review(
                text,
                "vacuum_full",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.REWRITE,
                rewrites=True,
                table=table,
                summary="VACUUM FULL rewrites the table under an ACCESS EXCLUSIVE lock.",
                detail="Nothing can read or write the table until it finishes, and it needs the table's size again in free space.",
                safer=["-- pg_repack reclaims the same space without holding the table locked throughout."],
            )
        return _review(
            text,
            "vacuum",
            LockLevel.SHARE_UPDATE_EXCLUSIVE,
            Duration.SCAN,
            rewrites=False,
            table=table,
            summary="VACUUM does not block reads or writes.",
            detail="It cannot run inside a transaction block.",
        )

    def _alter_table(self, stmt: ast.AlterTableStmt, text: str) -> StatementReview | None:
        table = _relation_name(stmt.relation)
        # A statement with several subcommands holds one lock for all of them,
        # so it is as unsafe as its worst part.
        reviews = [self._alter_command(cmd, text, table) for cmd in (stmt.cmds or ())]
        reviews = [review for review in reviews if review is not None]
        if not reviews:
            return None
        return max(reviews, key=lambda review: (review.duration is not Duration.INSTANT, review.rewrites_table))

    def _alter_command(self, cmd: ast.AlterTableCmd, text: str, table: str | None) -> StatementReview | None:
        subtype = cmd.subtype

        if subtype == AlterTableType.AT_AddColumn:
            return self._add_column(cmd, text, table)

        if subtype == AlterTableType.AT_AlterColumnType:
            return self._alter_column_type(cmd, text, table)

        if subtype == AlterTableType.AT_SetNotNull:
            return _review(
                text,
                "set_not_null",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.SCAN,
                rewrites=False,
                table=table,
                summary="SET NOT NULL scans the whole table under an ACCESS EXCLUSIVE lock.",
                detail="Nothing can read or write the table while it checks every row.",
                safer=[
                    f"ALTER TABLE {table} ADD CONSTRAINT {_constraint_name(table, cmd.name)} CHECK ({cmd.name} IS NOT NULL) NOT VALID;",
                    f"ALTER TABLE {table} VALIDATE CONSTRAINT {_constraint_name(table, cmd.name)};",
                    f"ALTER TABLE {table} ALTER COLUMN {cmd.name} SET NOT NULL;  -- now instant: it trusts the constraint",
                    f"ALTER TABLE {table} DROP CONSTRAINT {_constraint_name(table, cmd.name)};",
                ],
            )

        if subtype == AlterTableType.AT_AddConstraint:
            self._note_check(cmd, table)
            return self._add_constraint(cmd, text, table)

        if subtype == AlterTableType.AT_AttachPartition:
            return self._attach_partition(cmd, text, table)

        if subtype == AlterTableType.AT_DetachPartition:
            return self._detach_partition(cmd, text, table)

        if subtype == AlterTableType.AT_ValidateConstraint:
            if table and (table, str(cmd.name)) in self._unvalidated_checks:
                self._validated_checks.add(table)
            return _review(
                text,
                "validate_constraint",
                LockLevel.SHARE_UPDATE_EXCLUSIVE,
                Duration.SCAN,
                rewrites=False,
                table=table,
                summary="VALIDATE CONSTRAINT checks every row without blocking reads or writes.",
                detail="This is the half of the NOT VALID pattern that does the work, and it is the safe half.",
            )

        if subtype in (AlterTableType.AT_SetUnLogged, AlterTableType.AT_SetLogged):
            return _review(
                text,
                "set_logged",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.REWRITE,
                rewrites=True,
                table=table,
                summary="Changing a table between LOGGED and UNLOGGED rewrites it under an ACCESS EXCLUSIVE lock.",
                detail="Nothing can read or write the table until it finishes.",
            )

        instant = _INSTANT_ALTERS.get(subtype)  # type: ignore[arg-type]
        if instant is not None:
            kind, label = instant
            return _review(
                text,
                kind,
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.INSTANT,
                rewrites=False,
                table=table,
                summary=f"{label} takes an ACCESS EXCLUSIVE lock, briefly.",
                detail=(
                    "It is a catalog update, so the lock is held for no time at all. The exposure is the wait to "
                    "acquire it: behind a long-running query it queues, and everything else queues behind it. "
                    "Set a short lock_timeout and retry."
                ),
            )

        if subtype in (AlterTableType.AT_SetStatistics, AlterTableType.AT_SetOptions, AlterTableType.AT_SetRelOptions):
            return _review(
                text,
                "set_options",
                LockLevel.SHARE_UPDATE_EXCLUSIVE,
                Duration.INSTANT,
                rewrites=False,
                table=table,
                summary="Adjusting table or column options does not block reads or writes.",
                detail="A catalog update under a lock that only excludes other DDL.",
            )

        return None

    def _add_column(self, cmd: ast.AlterTableCmd, text: str, table: str | None) -> StatementReview:
        column = cmd.def_ if isinstance(cmd.def_, ast.ColumnDef) else None
        default = _default_expression(column)
        generated = _is_generated(column)

        if generated:
            return _review(
                text,
                "add_generated_column",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.REWRITE,
                rewrites=True,
                table=table,
                summary="Adding a stored generated column rewrites the table under an ACCESS EXCLUSIVE lock.",
                detail="Every existing row has to be given a value, so the whole table is written out again.",
            )

        if default is not None and not isinstance(default, ast.A_Const):
            column_name = column.colname if column and column.colname else "the_column"
            functions = _function_names(default)
            named = ", ".join(f"{name}()" for name in sorted(functions)) if functions else "the expression"
            review = _review(
                text,
                "add_column_with_computed_default",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.REWRITE,
                rewrites=True,
                table=table,
                summary="Adding a column with a non-constant default may rewrite the whole table.",
                detail=(
                    f"Since PostgreSQL 11 a constant default is stored once in the catalog and costs nothing, but a "
                    f"*volatile* one has to be evaluated per row, which rewrites the table under an ACCESS EXCLUSIVE "
                    f"lock. This default calls {named}: if that is volatile — random() and clock_timestamp() are, "
                    f"now() is not — the table is rewritten."
                ),
                safer=[
                    f"ALTER TABLE {table} ADD COLUMN {column_name} {_type_name(column) or '<type>'};  -- no default: instant",
                    f"ALTER TABLE {table} ALTER COLUMN {column_name} SET DEFAULT {_render(default)};  -- instant, applies to new rows",
                    "-- then backfill the existing rows in batches, outside a single transaction",
                ],
            )
            review.default_functions = sorted(functions)
            return review

        return _review(
            text,
            "add_column",
            LockLevel.ACCESS_EXCLUSIVE,
            Duration.INSTANT,
            rewrites=False,
            table=table,
            summary="ADD COLUMN takes an ACCESS EXCLUSIVE lock, briefly.",
            detail=(
                "No rewrite: since PostgreSQL 11 even NOT NULL with a constant default is a catalog update, whatever "
                "you may have read. The exposure is the wait for the lock, not the work under it — set a short "
                "lock_timeout so it cannot queue behind a long query and block everything behind itself."
            ),
        )

    def _alter_column_type(self, cmd: ast.AlterTableCmd, text: str, table: str | None) -> StatementReview:
        column = cmd.def_ if isinstance(cmd.def_, ast.ColumnDef) else None
        target = _type_name(column)

        if target and any(target == new for _old, new in _BINARY_COERCIBLE):
            # Whether it actually avoids the rewrite depends on the old type,
            # which is not in the statement — so this is stated as a condition
            # rather than as a fact.
            detail = (
                "Whether this rewrites depends on the type it is coming from. Widening varchar(n), or going from "
                "varchar to text, changes nothing on disk and is a catalog update; anything else writes every row "
                "out again under an ACCESS EXCLUSIVE lock."
            )
        else:
            detail = "The whole table is written out again under the lock, and nothing can read or write it meanwhile."

        return _review(
            text,
            "alter_column_type",
            LockLevel.ACCESS_EXCLUSIVE,
            Duration.REWRITE,
            rewrites=True,
            table=table,
            summary="ALTER COLUMN TYPE rewrites the table under an ACCESS EXCLUSIVE lock.",
            detail=detail,
            safer=[
                "-- Add the new column, backfill it in batches, switch reads over, then drop the old one:",
                f"ALTER TABLE {table} ADD COLUMN {cmd.name}_new {target or '<type>'};",
                "-- backfill in batches, keep both in step with a trigger, then swap and drop.",
            ],
        )

    def _add_constraint(self, cmd: ast.AlterTableCmd, text: str, table: str | None) -> StatementReview | None:
        constraint = cmd.def_ if isinstance(cmd.def_, ast.Constraint) else None
        if constraint is None:
            return None
        contype = constraint.contype
        not_valid = bool(constraint.skip_validation)

        if contype == ConstrType.CONSTR_FOREIGN:
            if not_valid:
                return _review(
                    text,
                    "add_foreign_key_not_valid",
                    LockLevel.SHARE_ROW_EXCLUSIVE,
                    Duration.INSTANT,
                    rewrites=False,
                    table=table,
                    summary="Adding a foreign key NOT VALID does not check existing rows.",
                    detail="Reads continue; writes wait only for the catalog update. VALIDATE CONSTRAINT afterwards does the checking safely.",
                )
            return _review(
                text,
                "add_foreign_key",
                LockLevel.SHARE_ROW_EXCLUSIVE,
                Duration.SCAN,
                rewrites=False,
                table=table,
                summary="Adding a foreign key checks every existing row while blocking writes.",
                detail=(
                    "Reads continue — this takes SHARE ROW EXCLUSIVE, not ACCESS EXCLUSIVE, whatever most advice says "
                    "— but writes to both tables wait for the whole scan."
                ),
                safer=[
                    text.rstrip(";") + " NOT VALID;",
                    f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint.conname or '<name>'};  -- scans without blocking writes",
                ],
            )

        if contype == ConstrType.CONSTR_CHECK:
            if not_valid:
                return _review(
                    text,
                    "add_check_not_valid",
                    LockLevel.ACCESS_EXCLUSIVE,
                    Duration.INSTANT,
                    rewrites=False,
                    table=table,
                    summary="Adding a CHECK constraint NOT VALID does not read the table.",
                    detail="The lock is brief because no existing row is examined.",
                )
            return _review(
                text,
                "add_check",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.SCAN,
                rewrites=False,
                table=table,
                summary="Adding a CHECK constraint reads every row under an ACCESS EXCLUSIVE lock.",
                detail="Nothing can read or write the table while it checks.",
                safer=[
                    text.rstrip(";") + " NOT VALID;",
                    f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint.conname or '<name>'};  -- checks without blocking",
                ],
            )

        if contype in (ConstrType.CONSTR_PRIMARY, ConstrType.CONSTR_UNIQUE):
            if constraint.indexname:
                return _review(
                    text,
                    "add_key_using_index",
                    LockLevel.ACCESS_EXCLUSIVE,
                    Duration.INSTANT,
                    rewrites=False,
                    table=table,
                    summary="Adopting an existing index as a key is a catalog update.",
                    detail="The index is already built, so the lock is held for an instant. This is the safe way to add a key.",
                )
            label = "primary key" if contype == ConstrType.CONSTR_PRIMARY else "unique constraint"
            index_columns = ", ".join(key.sval for key in (constraint.keys or ()))
            return _review(
                text,
                "add_key",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.SCAN,
                rewrites=False,
                table=table,
                summary=f"Adding a {label} builds its index under an ACCESS EXCLUSIVE lock.",
                detail="Nothing can read or write the table for the whole index build.",
                safer=[
                    f"CREATE UNIQUE INDEX CONCURRENTLY {_index_name(table, index_columns)} ON {table} ({index_columns});",
                    f"ALTER TABLE {table} ADD CONSTRAINT {constraint.conname or _index_name(table, index_columns)} "
                    f"{'PRIMARY KEY' if contype == ConstrType.CONSTR_PRIMARY else 'UNIQUE'} "
                    f"USING INDEX {_index_name(table, index_columns)};  -- instant",
                ],
            )

        return None

    # -- Partitions ---------------------------------------------------------

    def _note_check(self, cmd: ast.AlterTableCmd, table: str | None) -> None:
        """Remember a CHECK constraint, which ATTACH PARTITION can use to skip its scan once it is validated."""
        constraint = cmd.def_ if isinstance(cmd.def_, ast.Constraint) else None
        if constraint is None or table is None or constraint.contype != ConstrType.CONSTR_CHECK:
            return
        if constraint.skip_validation:
            self._unvalidated_checks[(table, constraint.conname or "")] = True
        else:
            self._validated_checks.add(table)

    def _create_table(self, stmt: ast.CreateStmt, text: str) -> StatementReview | None:
        """CREATE TABLE, which matters here only as PARTITION OF: that locks the parent."""
        name = _relation_name(stmt.relation)
        if stmt.partbound is None or not stmt.inhRelations or name is None:
            if name:
                self._created.add(name)
            return None
        parent = _relation_name(stmt.inhRelations[0]) or ""
        self._created.add(name)
        if parent in self._created:
            # A parent this migration created has nobody using it to block.
            return _review(
                text,
                "create_partition_of",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.INSTANT,
                rewrites=False,
                table=parent,
                summary="CREATE TABLE ... PARTITION OF takes an ACCESS EXCLUSIVE lock on the parent table.",
                detail=f"{parent} is created earlier in this migration, so nothing else is using it to be blocked.",
            )
        review = _review(
            text,
            "create_partition_of",
            LockLevel.ACCESS_EXCLUSIVE,
            Duration.INSTANT,
            rewrites=False,
            table=parent,
            summary="CREATE TABLE ... PARTITION OF takes an ACCESS EXCLUSIVE lock on the parent table.",
            detail=(
                f"Nothing can read or write {parent} while the statement waits for the lock and holds it: briefly, unless "
                f"{parent} has a DEFAULT partition holding rows, which is scanned under it. ATTACH PARTITION adds the same "
                "partition under SHARE UPDATE EXCLUSIVE on the parent, which blocks no reads or writes."
            ),
            safer=self._partition_recipe(parent, name, stmt.partbound, create=True),
        )
        self._partition_statements[id(review)] = (parent, name, stmt.partbound, "create")
        return review

    def _attach_partition(self, cmd: ast.AlterTableCmd, text: str, table: str | None) -> StatementReview | None:
        """ATTACH PARTITION: cheap on the parent, a scan of the attached table and the DEFAULT unless constraints prove them."""
        command = cmd.def_ if isinstance(cmd.def_, ast.PartitionCmd) else None
        partition = _relation_name(command.name) if command is not None else None
        if command is None or partition is None or table is None:
            return None
        checked = partition in self._validated_checks
        empty = partition in self._created and partition not in self._written
        if checked or empty:
            why = (
                "the validated CHECK constraint added to it earlier in this migration lets PostgreSQL skip the scan that "
                'proves its rows fit the bound, provided it implies the bound (PostgreSQL logs "partition constraint ... '
                'is implied by existing constraints" at DEBUG1 when it does)'
                if checked
                else (
                    "it is created earlier in this migration with nothing written into it, so the scan that proves its rows "
                    "fit the bound reads nothing"
                )
            )
            review = _review(
                text,
                "attach_partition",
                LockLevel.SHARE_UPDATE_EXCLUSIVE,
                Duration.INSTANT,
                rewrites=False,
                table=table,
                summary="ATTACH PARTITION takes SHARE UPDATE EXCLUSIVE on the parent, which blocks no reads or writes.",
                detail=f"{partition} itself is locked ACCESS EXCLUSIVE, and {why}.",
            )
        else:
            review = _review(
                text,
                "attach_partition_scan",
                LockLevel.ACCESS_EXCLUSIVE,
                Duration.SCAN,
                rewrites=False,
                table=partition,
                summary=f"ATTACH PARTITION scans {partition} under an ACCESS EXCLUSIVE lock to prove every row fits the partition bound.",
                detail=(
                    f"The parent {table} takes only SHARE UPDATE EXCLUSIVE, but nothing can read or write {partition} "
                    "until the scan ends. A validated CHECK constraint on it that implies the bound lets PostgreSQL skip the scan."
                ),
                safer=self._partition_recipe(table, partition, command.bound, create=False),
            )
        self._partition_statements[id(review)] = (table, partition, command.bound, "attach")
        return review

    def _detach_partition(self, cmd: ast.AlterTableCmd, text: str, table: str | None) -> StatementReview | None:
        command = cmd.def_ if isinstance(cmd.def_, ast.PartitionCmd) else None
        if command is None:
            return None
        if command.concurrent:
            return _review(
                text,
                "detach_partition_concurrently",
                LockLevel.SHARE_UPDATE_EXCLUSIVE,
                Duration.INSTANT,
                rewrites=False,
                table=table,
                summary="DETACH PARTITION CONCURRENTLY does not block reads or writes on the parent.",
                detail=(
                    "It waits out running transactions twice and cannot run inside a transaction block (PostgreSQL 14 and "
                    "later). Interrupted, it is finished with ALTER TABLE ... DETACH PARTITION ... FINALIZE."
                ),
            )
        return _review(
            text,
            "detach_partition",
            LockLevel.ACCESS_EXCLUSIVE,
            Duration.INSTANT,
            rewrites=False,
            table=table,
            summary="DETACH PARTITION takes an ACCESS EXCLUSIVE lock on the parent table, briefly.",
            detail=(
                "A catalog update, but every query on the parent queues behind it while it waits for the lock. "
                "CONCURRENTLY takes SHARE UPDATE EXCLUSIVE instead, from PostgreSQL 14, outside a transaction block "
                "and only where the parent has no DEFAULT partition."
            ),
            safer=[text.rstrip(";") + " CONCURRENTLY;  -- PostgreSQL 14 and later, outside a transaction block"],
        )

    def _settle_partition(self, review: StatementReview) -> StatementReview:
        """Weigh a partition statement again, now that the catalog says whether the parent has a DEFAULT partition."""
        entry = self._partition_statements.get(id(review))
        if entry is None:
            return review
        parent, partition, bound, kind = entry
        if review.safer:
            review.safer = self._partition_recipe(parent, partition, bound, create=kind == "create")
        default = self._defaults.get(parent)
        if default is None or bound is None or bound.is_default:
            return review
        default_name, default_bytes = default
        if default_bytes <= 0 or _named(default_name, self._validated_checks):
            return review

        statement = "ATTACH PARTITION" if kind == "attach" else "CREATE TABLE ... PARTITION OF"
        held = default_name if kind == "attach" else f"{default_name} or {parent}"
        detail = (
            f"It proves none of the DEFAULT partition's rows belongs to the new partition, and nothing can read or write "
            f"{held} until it finishes; a query on {parent} not pruned away from the DEFAULT waits with it. A validated "
            f"CHECK constraint on {default_name} that excludes the new range lets PostgreSQL skip the scan."
        )
        if review.kind == "attach_partition_scan":
            detail += f" {partition} is scanned under its own ACCESS EXCLUSIVE lock as well."
        settled = _review(
            review.statement,
            f"{'attach_partition' if kind == 'attach' else 'create_partition_of'}_scans_default",
            LockLevel.ACCESS_EXCLUSIVE,
            Duration.SCAN,
            rewrites=False,
            table=default_name,
            summary=f"{statement} scans {parent}'s DEFAULT partition {default_name} under an ACCESS EXCLUSIVE lock.",
            detail=detail,
            safer=self._partition_recipe(parent, partition, bound, create=kind == "create"),
        )
        settled.table_bytes = default_bytes
        return settled

    def _partition_recipe(self, parent: str, partition: str, bound: Any, create: bool) -> list[str]:
        """The statements that add the partition without ACCESS EXCLUSIVE on the parent or a scan under it.

        Written out in full where the bound is a range over one column whose
        name the catalog gave; otherwise the same steps, with the bound's
        condition left for the reader to write.
        """
        key = self._range_keys.get(parent)
        default = self._defaults.get(parent, (None, 0))[0]
        literals = _range_literals(bound)
        if key and literals:
            steps = attach_partition_recipe(parent, partition, key, literals[0], literals[1], default=default)
            if not create:
                steps = [step for step in steps if not step.startswith(("CREATE TABLE", "-- Attach rather", "-- load"))]
            return steps

        name = _identifier(f"{partition.rpartition('.')[2].strip(chr(34))}_bound")
        steps = [f"CREATE TABLE {partition} (LIKE {parent} INCLUDING ALL EXCLUDING IDENTITY);"] if create else []
        steps += [
            f"ALTER TABLE {partition} ADD CONSTRAINT {name} CHECK (<the partition bound, as a condition on the key>) NOT VALID;",
            f"ALTER TABLE {partition} VALIDATE CONSTRAINT {name};  -- lets ATTACH skip scanning it",
            f"-- if {parent} has a DEFAULT partition: a CHECK on it excluding the bound, NOT VALID and then VALIDATE",
            f"ALTER TABLE {parent} ATTACH PARTITION {partition} {_deparse(bound) if bound is not None else '<bound>'};",
            f"ALTER TABLE {partition} DROP CONSTRAINT {name};",
        ]
        return steps


# -- Helpers ----------------------------------------------------------------


def _range_literals(bound: Any) -> tuple[str, str] | None:
    """The lower and upper bound of a one-column range bound, as SQL literals."""
    if bound is None or bound.is_default or bound.strategy != "r":
        return None
    lower, upper = bound.lowerdatums or (), bound.upperdatums or ()
    if len(lower) != 1 or len(upper) != 1 or not isinstance(lower[0], ast.A_Const) or not isinstance(upper[0], ast.A_Const):
        return None
    return _deparse(lower[0]), _deparse(upper[0])


def _named(name: str, names: set[str]) -> bool:
    """Whether ``name`` is among ``names``, which may be written with or without the schema."""
    bare = name.rpartition(".")[2]
    return name in names or any(other.rpartition(".")[2] == bare for other in names)


def _review(
    statement: str,
    kind: str,
    lock: LockLevel,
    duration: Duration,
    rewrites: bool,
    summary: str,
    detail: str,
    table: str | None = None,
    safer: list[str] | None = None,
) -> StatementReview:
    return StatementReview(
        statement=statement,
        kind=kind,
        lock=lock,
        duration=duration,
        rewrites_table=rewrites,
        # Replaced by _weigh once the table's size is known.
        severity=Severity.LOW,
        summary=summary,
        detail=detail,
        table=table,
        safer=list(safer or []),
    )


def _statement_text(raw: Any, sql: str) -> str:
    """The statement as written, sliced out of the migration by the parser."""
    if not isinstance(raw, ast.RawStmt):
        return sql.strip()
    start = raw.stmt_location or 0
    end = start + raw.stmt_len if raw.stmt_len else len(sql)
    return sql[start:end].strip().rstrip(";")


def _partitioned_index_recipe(stmt: ast.IndexStmt, partitions: list[tuple[str, bool]]) -> list[str]:
    """The statements that build an index on a partitioned table without blocking its writes.

    The parent's index ON ONLY, which builds nothing; then for each partition
    a concurrent build and an ALTER INDEX ... ATTACH PARTITION, after which the
    parent's index is valid.
    """
    relation = stmt.relation
    if relation is None:
        return []
    name = stmt.idxname or _identifier(f"{relation.relname}_{'_'.join(_index_column_names(stmt))}_idx")
    qualified = f"{relation.schemaname}.{name}" if relation.schemaname else name

    parent = copy.deepcopy(stmt)
    parent.relation = copy.deepcopy(relation)
    parent.relation.inh = False
    parent.concurrent = False
    parent.idxname = name
    steps = [f"{_deparse(parent)};"]

    for partition, nested in partitions[:RECIPE_PARTITIONS]:
        schema, _, relname = partition.rpartition(".")
        child_name = _identifier(f"{name}_{relname.strip(chr(34))}")
        if nested:
            steps.append(f"-- {partition} is partitioned itself: give it the same treatment, ON ONLY and then its partitions.")
            continue
        child = copy.deepcopy(stmt)
        child.relation = ast.RangeVar(schemaname=schema or None, relname=relname.strip('"'), inh=True, relpersistence="p")
        child.concurrent = True
        child.idxname = child_name
        steps.append(f"{_deparse(child)};")
        steps.append(f"ALTER INDEX {qualified} ATTACH PARTITION {f'{schema}.' if schema else ''}{child_name};")
    if len(partitions) > RECIPE_PARTITIONS:
        steps.append(f"-- and the same for each of the other {len(partitions) - RECIPE_PARTITIONS} partitions.")
    if not partitions:
        steps.append("-- then, for each partition: CREATE INDEX CONCURRENTLY on it, and ALTER INDEX ... ATTACH PARTITION.")
    return steps


def _deparse(node: ast.Node) -> str:
    return str(RawStream()(node))


def _index_column_names(stmt: ast.IndexStmt) -> list[str]:
    return [str(param.name) if param.name else "expr" for param in stmt.indexParams or () if isinstance(param, ast.IndexElem)]


def _identifier(name: str) -> str:
    """A name PostgreSQL will not truncate, cut where it would cut it."""
    return name.encode()[:MAX_IDENTIFIER_LENGTH].decode(errors="ignore")


def _relation_name(relation: Any) -> str | None:
    if relation is None:
        return None
    return f"{relation.schemaname}.{relation.relname}" if relation.schemaname else relation.relname


def _first_relation(relations: Any) -> str | None:
    for relation in relations or ():
        name = _relation_name(relation)
        if name:
            return name
    return None


def _default_expression(column: ast.ColumnDef | None) -> Any:
    for constraint in (column.constraints or ()) if column else ():
        if constraint.contype == ConstrType.CONSTR_DEFAULT:
            return constraint.raw_expr
    return None


def _is_generated(column: ast.ColumnDef | None) -> bool:
    if column is None:
        return False
    return any(constraint.contype == ConstrType.CONSTR_GENERATED for constraint in (column.constraints or ()))


def _function_names(node: Any) -> set[str]:
    """Every function called anywhere in an expression."""
    found: set[str] = set()
    if isinstance(node, ast.FuncCall):
        found.add(".".join(part.sval for part in node.funcname or ()))
    for attribute in getattr(node, "__slots__", ()) or ():
        value = getattr(node, attribute, None)
        if isinstance(value, ast.Node):
            found |= _function_names(value)
        elif isinstance(value, tuple):
            for item in value:
                if isinstance(item, ast.Node):
                    found |= _function_names(item)
    return found


def _render(node: Any) -> str:
    """The expression as SQL, for quoting back into a suggested statement."""
    try:
        from pglast.stream import RawStream

        return RawStream()(node)
    except Exception:
        return "..."


def _type_name(column: ast.ColumnDef | None) -> str | None:
    if column is None or column.typeName is None:
        return None
    names = [part.sval for part in column.typeName.names]
    return names[-1] if names else None


def _constraint_name(table: str | None, column: str | None) -> str:
    base = f"{(table or 'tbl').split('.')[-1]}_{column or 'col'}_not_null"
    return base[:63]


def _index_name(table: str | None, columns: str) -> str:
    base = f"{(table or 'tbl').split('.')[-1]}_{columns.replace(', ', '_')}_key"
    return base[:63]


def _humanize(size: int) -> str:
    remaining = float(size)
    for unit in ("bytes", "kB", "MB", "GB", "TB"):
        if remaining < 1024 or unit == "TB":
            return f"{remaining:.0f} {unit}" if unit == "bytes" else f"{remaining:.1f} {unit}"
        remaining /= 1024.0
    return f"{size} bytes"


__all__ = ["LARGE_TABLE_BYTES", "MigrationLinter", "StatementReview"]

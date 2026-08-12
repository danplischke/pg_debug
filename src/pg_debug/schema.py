"""Schema introspection: list schemas, list objects, describe objects."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any
from typing import Dict
from typing import List

from .errors import InvalidArgumentError
from .sql import SafeSqlDriver
from .sql import SqlDriver

logger = logging.getLogger(__name__)


class ObjectType(str, Enum):
    """Kinds of database object that can be listed and described."""

    TABLE = "table"
    VIEW = "view"
    SEQUENCE = "sequence"
    EXTENSION = "extension"

    @classmethod
    def parse(cls, value: str | ObjectType) -> ObjectType:
        """Coerce a string to an ObjectType, with a helpful error message."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as e:
            valid = ", ".join(t.value for t in cls)
            raise InvalidArgumentError(f"Unsupported object type: {value}. Valid values are: {valid}.") from e


class SchemaInspector:
    """Reads the catalog to describe schemas, relations, sequences and extensions."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def list_schemas(self) -> List[Dict[str, Any]]:
        """List all schemas in the database.

        Returns:
            One dict per schema with ``schema_name``, ``schema_owner`` and
            ``schema_type`` ("User Schema", "System Schema" or
            "System Information Schema").
        """
        rows = await self.sql_driver.execute_query(
            """
            SELECT
                schema_name,
                schema_owner,
                CASE
                    WHEN schema_name LIKE 'pg_%' THEN 'System Schema'
                    WHEN schema_name = 'information_schema' THEN 'System Information Schema'
                    ELSE 'User Schema'
                END as schema_type
            FROM information_schema.schemata
            ORDER BY schema_type, schema_name
            """
        )
        return [row.cells for row in rows] if rows else []

    async def list_objects(self, schema_name: str, object_type: str | ObjectType = ObjectType.TABLE) -> List[Dict[str, Any]]:
        """List objects of a given type in a schema.

        Args:
            schema_name: Schema to list objects from. Ignored for extensions,
                which are not schema-specific.
            object_type: One of table, view, sequence, extension.

        Returns:
            One dict per object. Keys depend on the object type.
        """
        object_type = ObjectType.parse(object_type)

        if object_type in (ObjectType.TABLE, ObjectType.VIEW):
            table_type = "BASE TABLE" if object_type is ObjectType.TABLE else "VIEW"
            rows = await SafeSqlDriver.execute_param_query(
                self.sql_driver,
                """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = {} AND table_type = {}
                ORDER BY table_name
                """,
                [schema_name, table_type],
            )
            return (
                [{"schema": row.cells["table_schema"], "name": row.cells["table_name"], "type": row.cells["table_type"]} for row in rows]
                if rows
                else []
            )

        if object_type is ObjectType.SEQUENCE:
            rows = await SafeSqlDriver.execute_param_query(
                self.sql_driver,
                """
                SELECT sequence_schema, sequence_name, data_type
                FROM information_schema.sequences
                WHERE sequence_schema = {}
                ORDER BY sequence_name
                """,
                [schema_name],
            )
            return (
                [{"schema": row.cells["sequence_schema"], "name": row.cells["sequence_name"], "data_type": row.cells["data_type"]} for row in rows]
                if rows
                else []
            )

        # Extensions are not schema-specific.
        rows = await self.sql_driver.execute_query(
            """
            SELECT extname, extversion, extrelocatable
            FROM pg_extension
            ORDER BY extname
            """
        )
        return (
            [{"name": row.cells["extname"], "version": row.cells["extversion"], "relocatable": row.cells["extrelocatable"]} for row in rows]
            if rows
            else []
        )

    async def get_object_details(
        self,
        schema_name: str,
        object_name: str,
        object_type: str | ObjectType = ObjectType.TABLE,
    ) -> Dict[str, Any]:
        """Describe a single database object.

        For tables and views the result has ``basic``, ``columns``,
        ``constraints`` and ``indexes`` keys. For sequences and extensions it
        is a flat dict of catalog attributes, empty if the object is unknown.
        """
        object_type = ObjectType.parse(object_type)

        if object_type in (ObjectType.TABLE, ObjectType.VIEW):
            return await self._get_relation_details(schema_name, object_name, object_type)

        if object_type is ObjectType.SEQUENCE:
            rows = await SafeSqlDriver.execute_param_query(
                self.sql_driver,
                """
                SELECT sequence_schema, sequence_name, data_type, start_value, increment
                FROM information_schema.sequences
                WHERE sequence_schema = {} AND sequence_name = {}
                """,
                [schema_name, object_name],
            )
            if rows and rows[0]:
                row = rows[0]
                return {
                    "schema": row.cells["sequence_schema"],
                    "name": row.cells["sequence_name"],
                    "data_type": row.cells["data_type"],
                    "start_value": row.cells["start_value"],
                    "increment": row.cells["increment"],
                }
            return {}

        rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            """
            SELECT extname, extversion, extrelocatable
            FROM pg_extension
            WHERE extname = {}
            """,
            [object_name],
        )
        if rows and rows[0]:
            row = rows[0]
            return {"name": row.cells["extname"], "version": row.cells["extversion"], "relocatable": row.cells["extrelocatable"]}
        return {}

    async def _get_relation_details(self, schema_name: str, object_name: str, object_type: ObjectType) -> Dict[str, Any]:
        col_rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = {} AND table_name = {}
            ORDER BY ordinal_position
            """,
            [schema_name, object_name],
        )
        columns = (
            [
                {
                    "column": r.cells["column_name"],
                    "data_type": r.cells["data_type"],
                    "is_nullable": r.cells["is_nullable"],
                    "default": r.cells["column_default"],
                }
                for r in col_rows
            ]
            if col_rows
            else []
        )

        con_rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            """
            SELECT tc.constraint_name, tc.constraint_type, kcu.column_name
            FROM information_schema.table_constraints AS tc
            LEFT JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = {} AND tc.table_name = {}
            """,
            [schema_name, object_name],
        )

        constraints: Dict[str, Dict[str, Any]] = {}
        if con_rows:
            for row in con_rows:
                cname = row.cells["constraint_name"]
                ctype = row.cells["constraint_type"]
                col = row.cells["column_name"]

                if cname not in constraints:
                    constraints[cname] = {"type": ctype, "columns": []}
                if col:
                    constraints[cname]["columns"].append(col)

        idx_rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = {} AND tablename = {}
            """,
            [schema_name, object_name],
        )
        indexes = [{"name": r.cells["indexname"], "definition": r.cells["indexdef"]} for r in idx_rows] if idx_rows else []

        return {
            "basic": {"schema": schema_name, "name": object_name, "type": object_type.value},
            "columns": columns,
            "constraints": [{"name": name, **data} for name, data in constraints.items()],
            "indexes": indexes,
        }


__all__ = ["ObjectType", "SchemaInspector"]

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SqlDriver
from pg_debug.errors import InvalidArgumentError
from pg_debug.schema import ObjectType
from pg_debug.schema import SchemaInspector


def make_driver(*result_sets):
    """Mock driver returning the given row dicts, one result set per call."""
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(side_effect=[[SqlDriver.RowResult(cells=row) for row in rows] for rows in result_sets])
    return driver


def test_object_type_parse():
    assert ObjectType.parse("table") is ObjectType.TABLE
    assert ObjectType.parse(" VIEW ") is ObjectType.VIEW
    assert ObjectType.parse(ObjectType.SEQUENCE) is ObjectType.SEQUENCE

    with pytest.raises(InvalidArgumentError, match="Unsupported object type"):
        ObjectType.parse("trigger")


@pytest.mark.asyncio
async def test_list_schemas():
    driver = make_driver([{"schema_name": "public", "schema_owner": "postgres", "schema_type": "User Schema"}])

    result = await SchemaInspector(driver).list_schemas()

    assert result == [{"schema_name": "public", "schema_owner": "postgres", "schema_type": "User Schema"}]
    assert "information_schema.schemata" in driver.execute_query.call_args[0][0]


@pytest.mark.asyncio
async def test_list_schemas_when_empty():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=None)

    assert await SchemaInspector(driver).list_schemas() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "object_type,expected_table_type",
    [("table", "BASE TABLE"), ("view", "VIEW")],
)
async def test_list_relations(object_type, expected_table_type):
    driver = make_driver([{"table_schema": "public", "table_name": "orders", "table_type": expected_table_type}])

    result = await SchemaInspector(driver).list_objects("public", object_type)

    assert result == [{"schema": "public", "name": "orders", "type": expected_table_type}]
    query = driver.execute_query.call_args[0][0]
    assert "information_schema.tables" in query
    assert expected_table_type in query


@pytest.mark.asyncio
async def test_list_sequences():
    driver = make_driver([{"sequence_schema": "public", "sequence_name": "orders_id_seq", "data_type": "bigint"}])

    result = await SchemaInspector(driver).list_objects("public", "sequence")

    assert result == [{"schema": "public", "name": "orders_id_seq", "data_type": "bigint"}]


@pytest.mark.asyncio
async def test_list_extensions_ignores_schema():
    driver = make_driver([{"extname": "hypopg", "extversion": "1.4.1", "extrelocatable": True}])

    result = await SchemaInspector(driver).list_objects("anything", "extension")

    assert result == [{"name": "hypopg", "version": "1.4.1", "relocatable": True}]
    assert "pg_extension" in driver.execute_query.call_args[0][0]


@pytest.mark.asyncio
async def test_list_objects_rejects_unknown_type():
    with pytest.raises(InvalidArgumentError):
        await SchemaInspector(MagicMock(spec=SqlDriver)).list_objects("public", "trigger")


@pytest.mark.asyncio
async def test_get_table_details_groups_constraint_columns():
    driver = make_driver(
        [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "column_default": None}],
        [
            {"constraint_name": "orders_pkey", "constraint_type": "PRIMARY KEY", "column_name": "id"},
            {"constraint_name": "orders_pkey", "constraint_type": "PRIMARY KEY", "column_name": "tenant_id"},
            {"constraint_name": "orders_check", "constraint_type": "CHECK", "column_name": None},
        ],
        [{"indexname": "orders_pkey", "indexdef": "CREATE UNIQUE INDEX orders_pkey ON public.orders USING btree (id)"}],
    )

    result = await SchemaInspector(driver).get_object_details("public", "orders")

    assert result["basic"] == {"schema": "public", "name": "orders", "type": "table"}
    assert result["columns"] == [{"column": "id", "data_type": "integer", "is_nullable": "NO", "default": None}]
    assert result["constraints"] == [
        {"name": "orders_pkey", "type": "PRIMARY KEY", "columns": ["id", "tenant_id"]},
        {"name": "orders_check", "type": "CHECK", "columns": []},
    ]
    assert result["indexes"][0]["name"] == "orders_pkey"


@pytest.mark.asyncio
async def test_get_sequence_details():
    driver = make_driver(
        [
            {
                "sequence_schema": "public",
                "sequence_name": "orders_id_seq",
                "data_type": "bigint",
                "start_value": 1,
                "increment": 1,
            }
        ]
    )

    result = await SchemaInspector(driver).get_object_details("public", "orders_id_seq", "sequence")

    assert result["name"] == "orders_id_seq"
    assert result["start_value"] == 1


@pytest.mark.asyncio
async def test_get_object_details_for_missing_object():
    driver = make_driver([])

    assert await SchemaInspector(driver).get_object_details("public", "nope", "extension") == {}

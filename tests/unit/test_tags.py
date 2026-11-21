"""sqlcommenter tags, as the specification serializes them."""

import pytest

from pg_debug.tags import describe_tags
from pg_debug.tags import parse_sql_comment


def test_the_specification_s_serialization_is_decoded():
    sql = (
        "SELECT * FROM FOO /*action='%2Fparam*d',controller='index',framework='spring',"
        "traceparent='00-5bd66ef5095369c7b0d1f8f4bd33716a-c532cb4098ac3dd2-01',"
        "tracestate='congo%3Dt61rcWkgMzE%2Crojo%3D00f067aa0ba902b7'*/"
    )
    assert parse_sql_comment(sql) == {
        "action": "/param*d",
        "controller": "index",
        "framework": "spring",
        "traceparent": "00-5bd66ef5095369c7b0d1f8f4bd33716a-c532cb4098ac3dd2-01",
        "tracestate": "congo=t61rcWkgMzE,rojo=00f067aa0ba902b7",
    }


def test_an_escaped_quote_and_an_encoded_key_are_read():
    assert parse_sql_comment("SELECT 1 /*route%20parameter='FOO%20\\'BAR'*/;") == {"route parameter": "FOO 'BAR"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT 1 /* pg_debug */",
        "SELECT /*+ SeqScan(t) */ * FROM t",
        "SELECT 1 /*action='a', just words*/",
        "SELECT 1 /**/",
        "",
        None,
    ],
)
def test_a_comment_that_is_not_tags_reads_as_none(sql):
    assert parse_sql_comment(sql) == {}


def test_only_the_trailing_comment_counts():
    assert parse_sql_comment("/*app='x'*/ SELECT 1 /*route='%2Fa'*/") == {"route": "/a"}


def test_the_keys_that_name_a_caller_come_first():
    assert describe_tags({"traceparent": "00-1", "zeta": "z", "controller": "orders", "route": "/o"}) == (
        "route=/o controller=orders traceparent=00-1 zeta=z"
    )

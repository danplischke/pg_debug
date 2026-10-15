"""How the tuning commands show what they checked and refused, and candidates that were never costed."""

from pg_debug.cli.render import render_index_recommendations

RECOMMENDATION = {
    "index_apply_order": 1,
    "index_definition": "CREATE INDEX ON t USING btree (a, b)",
    "index_estimated_size": "8.0 MB",
    "benefit_of_this_index_only": {"improvement_multiple": "10.0"},
    "benefit_after_previous_indexes": {"improvement_multiple": "10.0"},
    "queries_using_index": 2,
    "workload_queries": 3,
}


def test_each_index_says_how_many_queries_use_it_and_the_checks_follow():
    rendered = render_index_recommendations(
        {
            "summary": {"total_recommendations": 1},
            "recommendations": [RECOMMENDATION],
            "consolidations": [
                {
                    "merged": True,
                    "indexes": ["CREATE INDEX ON t (a)", "CREATE INDEX ON t (a, b)"],
                    "into": "CREATE INDEX ON t (a, b)",
                    "worst_ratio": 1.02,
                    "size_before_bytes": 3_000_000,
                    "size_after_bytes": 2_000_000,
                },
                {"merged": False, "indexes": ["CREATE INDEX ON t (c, d)", "CREATE INDEX ON t (c, e)"], "reason": "over the bound of 1.10"},
            ],
            "regression_check": {
                "max_regression_factor": 0.1,
                "queries_checked": 3,
                "worst_query": {"ratio": 0.5},
                "refused": [
                    {
                        "index": "CREATE INDEX ON t (d)",
                        "queries": [{"query": "select 1", "baseline_cost": 100.0, "cost_with_set": 150.0, "ratio": 1.5}],
                    }
                ],
            },
            "refused_candidates": [{"indexes": ["CREATE INDEX ON t (x)"], "execution_cost": 90.0, "baseline_cost": 100.0, "reason": "did not beat"}],
            "workload": {
                "source": "query_store",
                "queries": 3,
                "counting_window": {"counting_since": "2026-10-14T10:00:00+00:00", "counting_seconds": 7200, "statements_evicted": 4},
                "note": "HypoPG's costs are deterministic.",
            },
        }
    )

    assert "used by:               2 of 3 workload queries" in rendered
    assert "Merged CREATE INDEX ON t (a) and CREATE INDEX ON t (a, b)" in rendered
    assert "3.0 MB -> 2.0 MB" in rendered
    assert "Not merged: CREATE INDEX ON t (c, d)" in rendered
    assert "may cost 1.10x its baseline; the most any costs now is 0.50x" in rendered
    assert "refused CREATE INDEX ON t (d):" in rendered
    assert "1.50x (100.0 -> 150.0): select 1" in rendered
    assert "Refused candidates (1):" in rendered
    assert "Workload: 3 queries from pg_stat_statements, counted since 2026-10-14 10:00 UTC (2 hours), with 4 evictions since." in rendered
    assert "deterministic" in rendered


def test_nothing_recommended_still_says_what_was_refused():
    rendered = render_index_recommendations(
        {
            "recommendations": "No index recommendations found.",
            "regression_check": {"max_regression_factor": 0.1, "queries_checked": 1, "worst_query": None, "refused": []},
            "workload": {"source": "query_store", "queries": 1, "counting_window": None, "note": "n"},
        }
    )
    assert rendered.startswith("No index recommendations found.")
    assert "over a window pg_stat_statements did not record" in rendered


def test_uncosted_candidates_are_marked_as_never_costed():
    rendered = render_index_recommendations(
        {
            "costed": False,
            "note": "These candidates were not costed by the planner.",
            "hypopg": "Cloud SQL does not offer hypopg.",
            "candidates": [
                {
                    "severity": "high",
                    "definition": "CREATE INDEX CONCURRENTLY ON public.orders (customer_id)",
                    "table_rows": 1_000_000,
                    "expected_rows": 20.0,
                    "steps": [{"column": "customer_id", "operator": "=", "n_distinct": 50000.0, "null_frac": 0.0, "rows_after": 20.0}],
                    "notes": ["lower(note) = 'x': inside an expression"],
                    "verify": "EXPLAIN it.",
                }
            ],
            "refused": [{"query": "select * from tiny", "table": "public.tiny", "reason": "100 rows"}],
            "google_db_advisor": {"recommendations": [{"index": "CREATE INDEX ON x(y)"}], "note": "for its own workload"},
        }
    )
    assert rendered.startswith("Cloud SQL does not offer hypopg.")
    assert "NOT COSTED BY THE PLANNER" in rendered
    assert "1. [high] (candidate) CREATE INDEX CONCURRENTLY ON public.orders (customer_id)" in rendered
    assert "expected rows: 1,000,000 -> 20.0" in rendered
    assert "left out: lower(note)" in rendered
    assert "public.tiny: 100 rows" in rendered
    assert "index: CREATE INDEX ON x(y)" in rendered

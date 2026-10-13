"""Runs the rules and puts their findings in a useful order."""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterable

from ..db import SafeSqlDriver
from ..db import SqlDriver
from . import columns
from . import keys
from . import layout
from .result import DEFAULT_MIN_ROWS
from .result import Confidence
from .result import ModelFinding
from .result import ReviewOptions
from .result import RuleId

logger = logging.getLogger(__name__)

Rule = Callable[[SqlDriver, ReviewOptions], Awaitable[list[ModelFinding]]]

#: Every rule, by the id that selects it. Adding one here is all it takes to
#: put it in the review, the CLI's --rule list and the documented vocabulary.
RULES: dict[RuleId, Rule] = {
    RuleId.UNINDEXED_FOREIGN_KEY: keys.unindexed_foreign_keys,
    RuleId.FOREIGN_KEY_TYPE_MISMATCH: keys.foreign_key_type_mismatches,
    RuleId.MISSING_PRIMARY_KEY: keys.missing_primary_keys,
    RuleId.COLUMN_ORDER_PADDING: layout.column_order_padding,
    RuleId.NAIVE_TIMESTAMP: columns.naive_timestamps,
    RuleId.BLANK_PADDED_CHAR: columns.blank_padded_chars,
    RuleId.NULLABLE_NEVER_NULL: columns.nullable_never_null,
    RuleId.LOW_CARDINALITY_TEXT: columns.low_cardinality_text,
}


class ModelReviewer:
    """Reviews a data model against every rule and reports what it finds."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def review(
        self,
        rules: str | RuleId | Iterable[str | RuleId] = RuleId.ALL,
        schema: str | None = None,
        min_rows: int = DEFAULT_MIN_ROWS,
        proven_only: bool = False,
    ) -> list[ModelFinding]:
        """Review the data model.

        Args:
            rules: Which rules to run: a :class:`RuleId`, a comma-separated
                string, or an iterable. ``all`` by default.
            schema: Restrict the review to one schema. Every user schema by
                default; system schemas are never reviewed.
            min_rows: Rules that reason from sampled statistics ignore tables
                smaller than this, where a sample is most of the table and
                proves nothing.
            proven_only: Drop the findings inferred from statistics, keeping
                only what the catalog proves.

        Returns:
            The findings, most severe first and, within a severity, ordered by
            each rule's own measure of impact.

        Raises:
            InvalidArgumentError: If an unknown rule is requested.
        """
        selected = RuleId.parse(rules)
        options = ReviewOptions(schema=schema, min_rows=min_rows)
        findings: list[ModelFinding] = []

        # One pinned connection for the whole review, so every rule reads the
        # same session. The rules run one after another rather than at once for
        # the same reason: a pinned connection carries one statement at a time.
        async with self.sql_driver.session() as driver:
            for rule_id, rule in RULES.items():
                if rule_id not in selected:
                    continue
                logger.debug("Running model rule %s", rule_id.value)
                findings.extend(await rule(driver, options))

        if proven_only:
            findings = [f for f in findings if f.confidence is Confidence.PROVEN]

        findings.sort(key=lambda finding: finding.sort_key)
        return findings


__all__ = ["RULES", "ModelReviewer", "Rule"]

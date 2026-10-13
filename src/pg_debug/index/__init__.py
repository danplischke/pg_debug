"""Index tuning: recommend indexes for a query workload."""

from typing import TYPE_CHECKING
from typing import Any

from .base import MAX_NUM_INDEX_TUNING_QUERIES
from .base import IndexRecommendation
from .base import IndexTuningBase
from .base import IndexTuningResult
from .dta import DatabaseTuningAdvisor
from .presentation import TextPresentation

if TYPE_CHECKING:
    from .llm import LLMOptimizerTool


def __getattr__(name: str) -> Any:
    """Import the LLM optimizer when it is first asked for.

    It needs the ``llm`` extra. Imported with the rest of the package, it would
    load pydantic on every ``import pg_debug``, and fail where the extra is not
    installed; asked for without it, it raises MissingDependencyError.
    """
    if name == "LLMOptimizerTool":
        from .llm import LLMOptimizerTool

        return LLMOptimizerTool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "MAX_NUM_INDEX_TUNING_QUERIES",
    "DatabaseTuningAdvisor",
    "IndexRecommendation",
    "IndexTuningBase",
    "IndexTuningResult",
    "LLMOptimizerTool",
    "TextPresentation",
]

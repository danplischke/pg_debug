"""Which model the LLM tuning method asks, apart from the method itself.

The method needs the ``llm`` extra; the CLI and the client need its default
without it, so the setting lives here, where nothing imports pydantic.
"""

import os

#: The model the LLM optimizer asks unless told otherwise.
DEFAULT_LLM_MODEL = "gpt-4o"

#: The message for a use of the LLM method without its extra installed.
LLM_EXTRA_REQUIRED = "The 'llm' index tuning method requires extra dependencies. Install them with: pip install 'pg-debug[llm]'"


def llm_model() -> str:
    """The model the LLM optimizer asks for candidate indexes.

    Override it with PG_DEBUG_LLM_MODEL — the default is a capable general
    model, but the prompt is not tied to it.
    """
    return os.environ.get("PG_DEBUG_LLM_MODEL", DEFAULT_LLM_MODEL)

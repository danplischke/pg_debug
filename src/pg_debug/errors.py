"""Exception types raised by the pg_debug library.

The library signals failures with exceptions instead of returning formatted
error strings, so callers can handle them programmatically. The CLI turns
them back into readable messages.
"""


class PgDebugError(Exception):
    """Base class for all errors raised by pg_debug."""


class ConnectionFailedError(PgDebugError):
    """Raised when a connection to PostgreSQL could not be established."""


class NotConnectedError(PgDebugError):
    """Raised when an operation is attempted before connecting."""


class InvalidArgumentError(PgDebugError, ValueError):
    """Raised when an argument is outside the range the library accepts."""


class ExtensionRequiredError(PgDebugError):
    """Raised when a required PostgreSQL extension is not installed.

    Attributes:
        extension: Name of the missing extension.
        detail: Human-readable explanation, including how to install it.
    """

    def __init__(self, extension: str, detail: str = ""):
        self.extension = extension
        self.detail = detail or f"The '{extension}' extension is required but is not installed."
        super().__init__(self.detail)


class ExplainPlanError(PgDebugError):
    """Raised when PostgreSQL could not produce an execution plan."""


class QueryError(PgDebugError):
    """Raised when a query is rejected or fails to execute."""


class IndexTuningError(PgDebugError):
    """Raised when index tuning could not finish.

    For example: the tables have never been analyzed, so there are no
    statistics to plan with, or none of the workload's queries could be read.
    """


class MissingDependencyError(PgDebugError, ImportError):
    """Raised when an optional dependency is needed but not installed."""


__all__ = [
    "ConnectionFailedError",
    "ExplainPlanError",
    "ExtensionRequiredError",
    "IndexTuningError",
    "InvalidArgumentError",
    "MissingDependencyError",
    "NotConnectedError",
    "PgDebugError",
    "QueryError",
]

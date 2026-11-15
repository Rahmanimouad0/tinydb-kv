"""TinyDB - a lightweight persistent key-value database."""

__version__ = "1.0.0"

from .engine import CompactionResult, Stats, TinyDB  # noqa: E402
from .errors import (  # noqa: E402
    CorruptDatabaseError,
    DatabaseClosedError,
    DatabaseLockedError,
    InvalidDatabaseFileError,
    TinyDBError,
)
from .storage import RecoveryReport  # noqa: E402

__all__ = [
    "TinyDB",
    "Stats",
    "CompactionResult",
    "RecoveryReport",
    "TinyDBError",
    "CorruptDatabaseError",
    "InvalidDatabaseFileError",
    "DatabaseLockedError",
    "DatabaseClosedError",
]

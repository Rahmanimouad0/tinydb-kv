"""Exceptions raised by TinyDB."""

from __future__ import annotations


class TinyDBError(Exception):
    """Base class for every error TinyDB raises on purpose."""


class CorruptDatabaseError(TinyDBError):
    """A record on disk failed validation (bad checksum, impossible length, ...)."""

    def __init__(self, path: str, offset: int, reason: str) -> None:
        self.path = path
        self.offset = offset
        self.reason = reason
        super().__init__(f"{path}: corrupt record at byte {offset}: {reason}")


class InvalidDatabaseFileError(TinyDBError):
    """The file is not a TinyDB database, or uses an unsupported format version."""


class DatabaseLockedError(TinyDBError):
    """Another process (or another TinyDB instance) already has the database open."""


class DatabaseClosedError(TinyDBError):
    """An operation was attempted on a closed database."""

"""Database engine: an in-memory hash index on top of the storage engine.

The index maps every live key to where its latest record sits in the file::

    "name" -> (offset=8,  size=27)
    "age"  -> (offset=35, size=22)

    SET     append a PUT record, point the index at it              O(1)
    GET     dict lookup + one positioned read + checksum check      O(1)
    DELETE  append a DELETE record (tombstone), drop the key        O(1)
    open    replay the log from start to end to rebuild the index   O(records)

Only keys and locations live in memory; values stay on disk. Memory grows
with the number of keys, not with the size of the values.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

from . import record
from .errors import CorruptDatabaseError, DatabaseClosedError
from .storage import FILE_HEADER_SIZE, RecoveryReport, Storage


@dataclass(frozen=True)
class Stats:
    keys: int
    file_size: int
    live_bytes: int  # records the index points at
    dead_bytes: int  # overwritten values and tombstones, reclaimable by compact()

    @property
    def fragmentation(self) -> float:
        total = self.live_bytes + self.dead_bytes
        return self.dead_bytes / total if total else 0.0


@dataclass(frozen=True)
class CompactionResult:
    size_before: int
    size_after: int

    @property
    def reclaimed(self) -> int:
        return self.size_before - self.size_after


class TinyDB:
    """A persistent key-value store backed by a single append-only file.

    >>> with TinyDB("data.db") as db:
    ...     db.set("name", "Mouad")
    ...     db.get("name")
    'Mouad'

    fsync=True  forces every write to the disk before returning, so it
                survives power loss (not just a process crash). Much slower.
    repair=True opens a corrupt file by dropping the unreadable records. The
                original is copied to ``<path>.corrupt`` first.
    """

    def __init__(self, path: str | os.PathLike[str], *, fsync: bool = False, repair: bool = False):
        self.path = os.fspath(path)
        self._storage = Storage(self.path, fsync=fsync)
        self._index: dict[str, tuple[int, int]] = {}
        self._dead_bytes = 0
        self._closed = False

        self._storage.open()
        try:
            self.recovery = self._load(repair)
        except BaseException:
            self._closed = True
            self._storage.close()
            raise

    def _load(self, repair: bool) -> RecoveryReport:
        """Rebuild the index by replaying every record in the log."""
        report = RecoveryReport()
        index = self._index
        dead = 0
        for offset, size, kind, key in self._storage.scan(report, skip_corrupt=repair):
            if kind == record.PUT:
                previous = index.get(key)
                index[key] = (offset, size)
            else:
                previous = index.pop(key, None)
                dead += size  # once applied, a tombstone is garbage too
            if previous is not None:
                dead += previous[1]
        self._dead_bytes = dead

        if report.corruptions:
            report.backup_path = self._storage.backup()
        self._storage.attach(report.end_offset)
        if report.corruptions:
            self.compact()  # rewrite without the bad records so the next open is clean
        return report

    # -- public API ----------------------------------------------------------

    def set(self, key: str, value: str) -> None:
        self._check_open()
        data = record.encode(record.PUT, key, value)
        offset = self._storage.append(data)
        previous = self._index.get(key)
        if previous is not None:
            self._dead_bytes += previous[1]
        self._index[key] = (offset, len(data))

    def get(self, key: str, default: str | None = None) -> str | None:
        self._check_open()
        _check_key(key)
        location = self._index.get(key)
        if location is None:
            return default
        _, value = self._fetch(key, location)
        return value

    def delete(self, key: str) -> bool:
        """Remove ``key``. Returns False (and writes nothing) if it did not exist."""
        self._check_open()
        _check_key(key)
        previous = self._index.get(key)
        if previous is None:
            return False
        data = record.encode(record.DELETE, key)
        self._storage.append(data)
        del self._index[key]
        self._dead_bytes += previous[1] + len(data)
        return True

    def keys(self, prefix: str = "") -> list[str]:
        """Live keys in sorted order. A hash index has no order, so this is O(n log n)."""
        self._check_open()
        return sorted(key for key in self._index if key.startswith(prefix))

    def stats(self) -> Stats:
        self._check_open()
        size = self._storage.size
        live = size - FILE_HEADER_SIZE - self._dead_bytes
        return Stats(len(self._index), size, live, self._dead_bytes)

    def compact(self) -> CompactionResult:
        """Rewrite the file so it holds exactly one record per live key.

        Overwritten values and tombstones are dropped. The new file is built
        beside the old one and renamed over it, so a crash at any point leaves
        either the old file or the new one, never a mix of both.
        """
        self._check_open()
        size_before = self._storage.size
        new_index: dict[str, tuple[int, int]] = {}

        def live_records() -> Iterator[bytes]:
            offset = FILE_HEADER_SIZE
            # Copy in file order: reads are sequential and the log keeps its order.
            for key, location in sorted(self._index.items(), key=lambda item: item[1][0]):
                data, _ = self._fetch(key, location)
                new_index[key] = (offset, len(data))
                offset += len(data)
                yield data

        self._storage.rewrite(live_records())
        self._index = new_index
        self._dead_bytes = 0
        return CompactionResult(size_before, self._storage.size)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._storage.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        self._check_open()
        return len(self._index)

    def __contains__(self, key: object) -> bool:
        self._check_open()
        return key in self._index

    def __enter__(self) -> TinyDB:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else f"{len(self._index)} keys"
        return f"TinyDB({self.path!r}, {state})"

    # -- internals -----------------------------------------------------------

    def _fetch(self, key: str, location: tuple[int, int]) -> tuple[bytes, str]:
        """Read the record at ``location``, verify it, return ``(raw bytes, value)``."""
        offset, size = location
        data = self._storage.read(offset, size)
        try:
            _, stored_key, value = record.decode(data)
        except record.RecordError as exc:
            raise CorruptDatabaseError(self.path, offset, str(exc)) from None
        if stored_key != key:
            raise CorruptDatabaseError(self.path, offset, f"expected key {key!r}, found {stored_key!r}")
        return data, value

    def _check_open(self) -> None:
        if self._closed:
            raise DatabaseClosedError(f"{self.path} is closed")


def _check_key(key: object) -> None:
    if not isinstance(key, str):
        raise TypeError(f"keys must be str, not {type(key).__name__}")

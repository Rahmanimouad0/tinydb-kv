from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tinydb import TinyDB, record
from tinydb.storage import FILE_HEADER_SIZE


class DatabaseTestCase(unittest.TestCase):
    """Gives each test a fresh directory and closes every database it opens."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "test.db"

    def open(self, **kwargs) -> TinyDB:
        db = TinyDB(self.path, **kwargs)
        self.addCleanup(db.close)  # cleanups run last-in first-out: before the rmtree
        return db

    def write(self, items: list[tuple[str, str]]) -> list[tuple[int, int]]:
        """SET each item in a fresh database; return each record's (offset, size)."""
        self.path.unlink(missing_ok=True)
        with TinyDB(self.path) as db:
            for key, value in items:
                db.set(key, value)
        locations, offset = [], FILE_HEADER_SIZE
        for key, value in items:
            size = len(record.encode(record.PUT, key, value))
            locations.append((offset, size))
            offset += size
        return locations

    def file_bytes(self) -> bytes:
        return self.path.read_bytes()

    def flip_byte(self, offset: int) -> None:
        data = bytearray(self.path.read_bytes())
        data[offset] ^= 0xFF
        self.path.write_bytes(bytes(data))

import unittest

from tinydb import DatabaseClosedError, DatabaseLockedError, TinyDB, record
from tinydb.storage import FILE_HEADER, FILE_HEADER_SIZE, TEMP_SUFFIX

from .support import DatabaseTestCase


class BasicOperationTests(DatabaseTestCase):
    def test_set_and_get(self):
        db = self.open()
        db.set("name", "Mouad")
        db.set("age", "22")
        self.assertEqual(db.get("name"), "Mouad")
        self.assertEqual(db.get("age"), "22")

    def test_get_missing_key(self):
        db = self.open()
        self.assertIsNone(db.get("nope"))
        self.assertEqual(db.get("nope", "fallback"), "fallback")

    def test_overwrite_keeps_latest_value(self):
        db = self.open()
        db.set("k", "one")
        db.set("k", "two")
        self.assertEqual(db.get("k"), "two")
        self.assertEqual(len(db), 1)

    def test_delete(self):
        db = self.open()
        db.set("k", "v")
        self.assertTrue(db.delete("k"))
        self.assertIsNone(db.get("k"))
        self.assertNotIn("k", db)

    def test_delete_missing_key_writes_nothing(self):
        db = self.open()
        size = db.stats().file_size
        self.assertFalse(db.delete("nope"))
        self.assertEqual(db.stats().file_size, size)

    def test_set_after_delete(self):
        db = self.open()
        db.set("k", "old")
        db.delete("k")
        db.set("k", "new")
        self.assertEqual(db.get("k"), "new")

    def test_len_contains_and_keys(self):
        db = self.open()
        for key in ["user:2", "user:1", "order:1"]:
            db.set(key, "x")
        self.assertEqual(len(db), 3)
        self.assertIn("user:1", db)
        self.assertEqual(db.keys(), ["order:1", "user:1", "user:2"])
        self.assertEqual(db.keys("user:"), ["user:1", "user:2"])
        self.assertEqual(db.keys("zzz"), [])

    def test_unusual_values(self):
        db = self.open()
        values = {
            "empty": "",
            "spaces": "hello world",
            "newlines": "line 1\nline 2\r\n",
            "unicode": "مرحبا 🌍 日本語",
            "nul": "a\x00b",
            "big": "x" * 1_000_000,
        }
        for key, value in values.items():
            db.set(key, value)
        for key, value in values.items():
            self.assertEqual(db.get(key), value, key)

    def test_invalid_keys_and_values(self):
        db = self.open()
        with self.assertRaises(ValueError):
            db.set("", "v")
        with self.assertRaises(ValueError):
            db.set("k" * (record.MAX_KEY_SIZE + 1), "v")
        with self.assertRaises(TypeError):
            db.set(1, "v")
        with self.assertRaises(TypeError):
            db.set("k", 1)
        with self.assertRaises(TypeError):
            db.get(1)
        with self.assertRaises(TypeError):
            db.delete(None)
        self.assertEqual(len(db), 0)
        self.assertEqual(db.stats().file_size, FILE_HEADER_SIZE)  # nothing was written


class PersistenceTests(DatabaseTestCase):
    def test_new_file_holds_only_the_header(self):
        self.open().close()
        self.assertEqual(self.file_bytes(), FILE_HEADER)

    def test_data_survives_reopen(self):
        with TinyDB(self.path) as db:
            db.set("name", "Mouad")
            db.set("age", "22")
            db.set("city", "Rabat")
            db.set("age", "23")
            db.delete("city")
        db = self.open()
        self.assertEqual(db.get("name"), "Mouad")
        self.assertEqual(db.get("age"), "23")
        self.assertIsNone(db.get("city"))
        self.assertEqual(len(db), 2)
        self.assertTrue(db.recovery.clean)

    def test_many_keys_survive_reopen(self):
        expected = {f"key:{i}": f"value:{i * i}" for i in range(2000)}
        with TinyDB(self.path) as db:
            for key, value in expected.items():
                db.set(key, value)
            for i in range(0, 2000, 3):
                db.delete(f"key:{i}")
                del expected[f"key:{i}"]
        db = self.open()
        self.assertEqual(len(db), len(expected))
        for key, value in expected.items():
            self.assertEqual(db.get(key), value)

    def test_file_is_exactly_the_records_written(self):
        with TinyDB(self.path) as db:
            db.set("a", "1")
            db.delete("a")
        expected = FILE_HEADER + record.encode(record.PUT, "a", "1") + record.encode(record.DELETE, "a")
        self.assertEqual(self.file_bytes(), expected)

    def test_fsync_mode(self):
        with TinyDB(self.path, fsync=True) as db:
            db.set("k", "v")
        self.assertEqual(self.open().get("k"), "v")


class StatsAndCompactionTests(DatabaseTestCase):
    def test_stats_track_dead_bytes(self):
        db = self.open()
        db.set("k", "one")
        self.assertEqual(db.stats().dead_bytes, 0)
        db.set("k", "two")
        first = len(record.encode(record.PUT, "k", "one"))
        self.assertEqual(db.stats().dead_bytes, first)
        db.delete("k")
        second = len(record.encode(record.PUT, "k", "two"))
        tombstone = len(record.encode(record.DELETE, "k"))
        stats = db.stats()
        self.assertEqual(stats.dead_bytes, first + second + tombstone)
        self.assertEqual(stats.live_bytes, 0)
        self.assertEqual(stats.fragmentation, 1.0)

    def test_dead_bytes_are_rebuilt_on_open(self):
        with TinyDB(self.path) as db:
            db.set("k", "one")
            db.set("k", "two")
            db.set("other", "x")
            db.delete("other")
            before = db.stats()
        self.assertEqual(self.open().stats(), before)

    def test_compact_drops_garbage_and_keeps_data(self):
        db = self.open()
        for version in range(5):
            for i in range(100):
                db.set(f"k{i}", f"v{i}-{version}")
        for i in range(50):
            db.delete(f"k{i}")
        size_before = db.stats().file_size

        result = db.compact()

        self.assertEqual(result.size_before, size_before)
        self.assertLess(result.size_after, size_before / 5)
        self.assertEqual(db.stats().dead_bytes, 0)
        self.assertEqual(db.stats().file_size, result.size_after)
        self.assertEqual(len(db), 50)
        for i in range(50, 100):
            self.assertEqual(db.get(f"k{i}"), f"v{i}-4")
        # the compacted file is still writable and survives a reopen
        db.set("new", "value")
        db.close()
        db = self.open()
        self.assertEqual(len(db), 51)
        self.assertEqual(db.get("k99"), "v99-4")
        self.assertEqual(db.get("new"), "value")
        self.assertFalse((self.dir / ("test.db" + TEMP_SUFFIX)).exists())

    def test_compacting_an_empty_database(self):
        db = self.open()
        db.set("k", "v")
        db.delete("k")
        db.compact()
        self.assertEqual(self.file_bytes(), FILE_HEADER)

    def test_leftover_compaction_file_is_removed(self):
        with TinyDB(self.path) as db:
            db.set("k", "v")
        leftover = self.dir / ("test.db" + TEMP_SUFFIX)
        leftover.write_bytes(b"half-written compaction output")
        db = self.open()
        self.assertFalse(leftover.exists())
        self.assertEqual(db.get("k"), "v")


class LifecycleTests(DatabaseTestCase):
    def test_context_manager_closes(self):
        with TinyDB(self.path) as db:
            db.set("k", "v")
        self.assertTrue(db.closed)

    def test_operations_on_closed_database_fail(self):
        db = self.open()
        db.close()
        db.close()  # closing twice is harmless
        for operation in (lambda: db.get("k"), lambda: db.set("k", "v"), lambda: db.delete("k"),
                          lambda: len(db), lambda: db.keys(), lambda: db.compact()):
            with self.assertRaises(DatabaseClosedError):
                operation()

    def test_second_instance_is_locked_out(self):
        self.open()
        with self.assertRaises(DatabaseLockedError):
            TinyDB(self.path)

    def test_lock_is_released_on_close(self):
        TinyDB(self.path).close()
        self.open().set("k", "v")

    def test_failed_write_is_rolled_back(self):
        db = self.open()
        db.set("a", "1")
        real_writer = db._storage._writer

        class DiskFullWriter:
            """Writes half a record, then fails like a full disk."""

            def write(self, data):
                real_writer.write(bytes(data[: len(data) // 2]))
                raise OSError(28, "No space left on device")

            def fileno(self):
                return real_writer.fileno()

        db._storage._writer = DiskFullWriter()
        with self.assertRaises(OSError):
            db.set("b", "2")
        db._storage._writer = real_writer

        self.assertIsNone(db.get("b"))
        db.set("c", "3")  # must land right after "a", not after half of "b"
        db.close()
        db = self.open()
        self.assertTrue(db.recovery.clean)
        self.assertEqual((db.get("a"), db.get("b"), db.get("c")), ("1", None, "3"))


if __name__ == "__main__":
    unittest.main()

"""What happens when the file on disk is damaged.

Two kinds of damage are handled differently:

* torn write  - the last record is cut short (the process died mid-append).
                Expected after a crash; repaired automatically on open.
* corruption  - a complete record fails its checksum (bad disk, bad copy,
                someone edited the file). Never silently "fixed": opening
                fails unless repair=True is passed explicitly.
"""

import unittest

from tinydb import CorruptDatabaseError, InvalidDatabaseFileError, TinyDB, record
from tinydb.storage import BACKUP_SUFFIX, FILE_HEADER, MAGIC

from .support import DatabaseTestCase

ITEMS = [("a", "first"), ("b", "second"), ("c", "third")]


class TornWriteTests(DatabaseTestCase):
    def test_record_cut_short_is_dropped(self):
        (_, _), (_, _), (c_offset, c_size) = self.write(ITEMS)
        self.path.write_bytes(self.file_bytes()[:-5])

        db = self.open()

        self.assertEqual((db.get("a"), db.get("b"), db.get("c")), ("first", "second", None))
        self.assertEqual(db.recovery.torn_bytes, c_size - 5)
        self.assertEqual(self.path.stat().st_size, c_offset)  # the partial record is gone

    def test_header_cut_short_is_dropped(self):
        self.write(ITEMS)
        with open(self.path, "ab") as f:
            f.write(record.encode(record.PUT, "d", "fourth")[:10])
        db = self.open()
        self.assertEqual(db.recovery.torn_bytes, 10)
        self.assertEqual(len(db), 3)

    def test_database_is_writable_after_recovery(self):
        self.write(ITEMS)
        self.path.write_bytes(self.file_bytes()[:-1])
        with TinyDB(self.path) as db:
            db.set("c", "third, again")
            db.set("d", "fourth")
        db = self.open()
        self.assertTrue(db.recovery.clean)
        self.assertEqual(db.get("c"), "third, again")
        self.assertEqual(db.get("d"), "fourth")

    def test_empty_file_becomes_a_new_database(self):
        self.path.write_bytes(b"")
        self.assertEqual(len(self.open()), 0)
        self.assertEqual(self.file_bytes(), FILE_HEADER)

    def test_file_header_cut_short_becomes_a_new_database(self):
        self.path.write_bytes(FILE_HEADER[:3])
        self.assertEqual(len(self.open()), 0)
        self.assertEqual(self.file_bytes(), FILE_HEADER)


class CorruptionTests(DatabaseTestCase):
    def test_bad_value_refuses_to_open_and_leaves_the_file_alone(self):
        _, (b_offset, b_size), _ = self.write(ITEMS)
        self.flip_byte(b_offset + b_size - 1)  # last byte of b's value
        before = self.file_bytes()

        with self.assertRaises(CorruptDatabaseError) as ctx:
            TinyDB(self.path)

        self.assertEqual(ctx.exception.offset, b_offset)
        self.assertIn("body checksum", ctx.exception.reason)
        self.assertEqual(self.file_bytes(), before)

    def test_bad_header_refuses_to_open(self):
        _, (b_offset, _), _ = self.write(ITEMS)
        self.flip_byte(b_offset + 6)  # inside key_len
        with self.assertRaises(CorruptDatabaseError) as ctx:
            TinyDB(self.path)
        self.assertEqual(ctx.exception.offset, b_offset)
        self.assertIn("header checksum", ctx.exception.reason)

    def test_garbage_after_the_last_record_refuses_to_open(self):
        self.write(ITEMS)
        with open(self.path, "ab") as f:
            f.write(b"\x00" * 64)  # a complete (fake) header that fails its checksum
        with self.assertRaises(CorruptDatabaseError):
            TinyDB(self.path)

    def test_corruption_after_open_is_caught_on_read(self):
        _, (b_offset, b_size), _ = self.write(ITEMS)
        db = self.open()
        self.flip_byte(b_offset + b_size - 1)
        self.assertEqual(db.get("a"), "first")
        with self.assertRaises(CorruptDatabaseError):
            db.get("b")

    def test_not_a_database(self):
        self.path.write_bytes(b"just some text file\n")
        for repair in (False, True):
            with self.subTest(repair=repair), self.assertRaises(InvalidDatabaseFileError):
                TinyDB(self.path, repair=repair)
        self.assertEqual(self.file_bytes(), b"just some text file\n")

    def test_unsupported_format_version(self):
        self.path.write_bytes(MAGIC + b"\x63\x00")
        with self.assertRaisesRegex(InvalidDatabaseFileError, "version 99"):
            TinyDB(self.path)

    def test_failed_open_releases_the_lock(self):
        _, (b_offset, _), _ = self.write(ITEMS)
        self.flip_byte(b_offset + 20)
        with self.assertRaises(CorruptDatabaseError):
            TinyDB(self.path)
        self.open(repair=True)  # would raise DatabaseLockedError if the lock leaked


class RepairTests(DatabaseTestCase):
    def test_bad_value_drops_only_that_record(self):
        _, (b_offset, b_size), _ = self.write(ITEMS)
        self.flip_byte(b_offset + b_size - 1)
        original = self.file_bytes()

        db = self.open(repair=True)

        self.assertEqual((db.get("a"), db.get("b"), db.get("c")), ("first", None, "third"))
        [problem] = db.recovery.corruptions
        self.assertEqual((problem.offset, problem.bytes_lost), (b_offset, b_size))
        backup = self.dir / ("test.db" + BACKUP_SUFFIX)
        self.assertEqual(db.recovery.backup_path, str(backup))
        self.assertEqual(backup.read_bytes(), original)

    def test_bad_header_drops_everything_after_it(self):
        _, (b_offset, b_size), (_, c_size) = self.write(ITEMS)
        self.flip_byte(b_offset + 6)

        db = self.open(repair=True)

        self.assertEqual((db.get("a"), db.get("b"), db.get("c")), ("first", None, None))
        [problem] = db.recovery.corruptions
        self.assertEqual(problem.bytes_lost, b_size + c_size)

    def test_repaired_file_opens_cleanly_afterwards(self):
        _, (b_offset, _), _ = self.write(ITEMS)
        self.flip_byte(b_offset + 20)
        with TinyDB(self.path, repair=True) as db:
            db.set("d", "fourth")
        db = self.open()
        self.assertTrue(db.recovery.clean)
        self.assertEqual(db.keys(), ["a", "c", "d"])

    def test_repair_never_overwrites_an_earlier_backup(self):
        for expected_suffix in (BACKUP_SUFFIX, BACKUP_SUFFIX + ".1"):
            _, (b_offset, _), _ = self.write(ITEMS)
            self.flip_byte(b_offset + 20)
            with TinyDB(self.path, repair=True) as db:
                self.assertTrue(db.recovery.backup_path.endswith(expected_suffix))

    def test_repair_on_a_healthy_file_changes_nothing(self):
        self.write(ITEMS)
        before = self.file_bytes()
        db = self.open(repair=True)
        self.assertTrue(db.recovery.clean)
        self.assertIsNone(db.recovery.backup_path)
        db.close()
        self.assertEqual(self.file_bytes(), before)


if __name__ == "__main__":
    unittest.main()

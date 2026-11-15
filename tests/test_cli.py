import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tinydb import TinyDB
from tinydb.cli import handle_line, main, repl

from .support import DatabaseTestCase


class ShellTests(DatabaseTestCase):
    def run_session(self, lines: list[str]) -> list[str]:
        db = self.open()
        out = io.StringIO()
        repl(db, stdin=io.StringIO("".join(line + "\n" for line in lines)), stdout=out)
        return out.getvalue().splitlines()

    def test_example_session(self):
        output = self.run_session(["SET name Mouad", "SET age 22", "GET name", "DELETE age"])
        self.assertEqual(output, ["OK", "OK", "Mouad", "OK"])

    def test_missing_keys(self):
        output = self.run_session(["GET ghost", "DELETE ghost", "EXISTS ghost"])
        self.assertEqual(output, ["(nil)", "(not found)", "false"])

    def test_empty_value_is_shown_as_quotes(self):
        self.assertEqual(self.run_session(['SET k ""', "GET k"]), ["OK", '""'])

    def test_errors_do_not_end_the_session(self):
        output = self.run_session(["FLY", "SET a", "SET a 1", "GET a"])
        self.assertTrue(output[0].startswith("ERR unknown command"))
        self.assertTrue(output[1].startswith("ERR wrong number of arguments"))
        self.assertEqual(output[2:], ["OK", "1"])

    def test_exit_stops_reading(self):
        self.assertEqual(self.run_session(["SET a 1", "EXIT", "GET a"]), ["OK"])

    def test_keys_count_and_stats(self):
        output = self.run_session(["SET b 2", "SET a 1", "KEYS", "COUNT", "STATS"])
        self.assertEqual(output[2:5], ["1) a", "2) b", "2"])
        self.assertTrue(output[5].startswith("keys"))

    def test_keys_on_empty_database(self):
        self.assertEqual(self.run_session(["KEYS"]), ["(empty)"])

    def test_compact(self):
        output = self.run_session(["SET a 1", "SET a 2", "COMPACT", "GET a"])
        self.assertTrue(output[2].startswith("OK ("), output[2])
        self.assertEqual(output[3], "2")

    def test_reply_flags(self):
        db = self.open()
        self.assertTrue(handle_line(db, "SET a 1").ok)
        self.assertFalse(handle_line(db, "NOPE").ok)
        self.assertTrue(handle_line(db, "EXIT").quit)
        self.assertIsNone(handle_line(db, "   ").text)


class MainTests(DatabaseTestCase):
    def run_main(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(self.path), *args])
        return code, out.getvalue(), err.getvalue()

    def test_commands_from_arguments(self):
        code, out, _ = self.run_main("-c", "SET name Mouad", "-c", "GET name")
        self.assertEqual((code, out), (0, "OK\nMouad\n"))
        code, out, _ = self.run_main("-c", "GET name")  # persisted
        self.assertEqual((code, out), (0, "Mouad\n"))

    def test_failing_command_sets_exit_status(self):
        code, out, _ = self.run_main("-c", "SET only-a-key")
        self.assertEqual(code, 1)
        self.assertIn("ERR", out)

    def test_corrupt_database_suggests_repair(self):
        with TinyDB(self.path) as db:
            db.set("a", "1")
            db.set("b", "2")
        data = bytearray(self.path.read_bytes())
        data[-1] ^= 0xFF
        self.path.write_bytes(bytes(data))

        code, _, err = self.run_main("-c", "GET a")
        self.assertEqual(code, 1)
        self.assertIn("corrupt record", err)
        self.assertIn("--repair", err)

        code, out, err = self.run_main("--repair", "-c", "GET a", "-c", "GET b")
        self.assertEqual((code, out), (0, "1\n(nil)\n"))
        self.assertIn("repaired", err)

    def test_not_a_database(self):
        self.path.write_text("hello")
        code, _, err = self.run_main("-c", "GET a")
        self.assertEqual(code, 1)
        self.assertIn("not a TinyDB database", err)


if __name__ == "__main__":
    unittest.main()

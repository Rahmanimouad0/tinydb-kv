import unittest

from tinydb.parser import Command, ParseError, parse


class ParserTests(unittest.TestCase):
    def test_basic_commands(self):
        self.assertEqual(parse("SET name Mouad"), Command("SET", ("name", "Mouad")))
        self.assertEqual(parse("GET name"), Command("GET", ("name",)))
        self.assertEqual(parse("DELETE age"), Command("DELETE", ("age",)))
        self.assertEqual(parse("KEYS"), Command("KEYS", ()))
        self.assertEqual(parse("KEYS user:"), Command("KEYS", ("user:",)))

    def test_command_names_are_case_insensitive_but_arguments_are_not(self):
        self.assertEqual(parse("set Name Mouad"), Command("SET", ("Name", "Mouad")))
        self.assertEqual(parse("Get Name"), Command("GET", ("Name",)))

    def test_aliases(self):
        self.assertEqual(parse("DEL k"), Command("DELETE", ("k",)))
        self.assertEqual(parse("quit"), Command("EXIT", ()))

    def test_blank_lines(self):
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("   \t \n"))

    def test_quoting(self):
        self.assertEqual(parse('SET msg "hello world"').args, ("msg", "hello world"))
        self.assertEqual(parse("SET msg 'say \"hi\"'").args, ("msg", 'say "hi"'))
        self.assertEqual(parse('SET "my key" v').args, ("my key", "v"))
        self.assertEqual(parse('SET empty ""').args, ("empty", ""))

    def test_backslashes_and_hashes_are_plain_characters(self):
        self.assertEqual(parse(r"SET path C:\Users\mouad").args, ("path", r"C:\Users\mouad"))
        self.assertEqual(parse("SET tag #1").args, ("tag", "#1"))

    def test_extra_whitespace_is_ignored(self):
        self.assertEqual(parse("   SET    a    b   \n"), Command("SET", ("a", "b")))

    def test_unknown_command(self):
        with self.assertRaisesRegex(ParseError, "unknown command 'FLY'"):
            parse("FLY away")

    def test_wrong_number_of_arguments(self):
        for line in ["SET", "SET k", "GET", "GET a b", "DELETE", "COUNT extra", "KEYS a b"]:
            with self.subTest(line=line), self.assertRaisesRegex(ParseError, "wrong number of arguments"):
                parse(line)

    def test_unquoted_spaces_in_value_get_a_hint(self):
        with self.assertRaisesRegex(ParseError, "quote values"):
            parse("SET greeting hello world")

    def test_unbalanced_quote(self):
        with self.assertRaisesRegex(ParseError, "no closing quotation"):
            parse('SET k "oops')


if __name__ == "__main__":
    unittest.main()

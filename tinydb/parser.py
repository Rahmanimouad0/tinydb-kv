"""Command parser: turns one line of input into a validated Command."""

from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class Spec:
    usage: str
    summary: str
    min_args: int
    max_args: int


COMMANDS: dict[str, Spec] = {
    "SET": Spec("SET <key> <value>", "store a value (quote values with spaces)", 2, 2),
    "GET": Spec("GET <key>", "print a value, or (nil) if the key is missing", 1, 1),
    "DELETE": Spec("DELETE <key>", "remove a key (alias: DEL)", 1, 1),
    "EXISTS": Spec("EXISTS <key>", "print true or false", 1, 1),
    "KEYS": Spec("KEYS [prefix]", "list keys, optionally only those starting with prefix", 0, 1),
    "COUNT": Spec("COUNT", "number of keys", 0, 0),
    "STATS": Spec("STATS", "file size, live and dead bytes", 0, 0),
    "COMPACT": Spec("COMPACT", "rewrite the file without overwritten or deleted records", 0, 0),
    "HELP": Spec("HELP", "show this help", 0, 0),
    "EXIT": Spec("EXIT", "quit (alias: QUIT, or Ctrl-D)", 0, 0),
}
ALIASES = {"DEL": "DELETE", "QUIT": "EXIT"}


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...] = ()


def tokenize(line: str) -> list[str]:
    """Split on whitespace; "double" or 'single' quotes group words together.

    Backslashes are ordinary characters, so Windows paths survive unchanged.
    """
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    try:
        return list(lexer)
    except ValueError as exc:  # "No closing quotation"
        raise ParseError(str(exc).lower()) from None


def parse(line: str) -> Command | None:
    """Parse one line. Returns None for a blank line, raises ParseError if invalid."""
    tokens = tokenize(line)
    if not tokens:
        return None
    word, args = tokens[0], tuple(tokens[1:])
    name = ALIASES.get(word.upper(), word.upper())
    spec = COMMANDS.get(name)
    if spec is None:
        raise ParseError(f"unknown command '{word}' (type HELP for a list)")
    if not spec.min_args <= len(args) <= spec.max_args:
        hint = "; quote values that contain spaces" if name == "SET" and len(args) > 2 else ""
        raise ParseError(f"wrong number of arguments for {name} (usage: {spec.usage}{hint})")
    return Command(name, args)

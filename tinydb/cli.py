"""Interactive shell: read a line, parse it, run it against the database."""

from __future__ import annotations

import argparse
import sys
from typing import NamedTuple, TextIO

from . import __version__
from .engine import Stats, TinyDB
from .errors import CorruptDatabaseError, TinyDBError
from .parser import COMMANDS, Command, parse
from .storage import RecoveryReport

PROMPT = "tinydb> "
HELP = "Commands:\n" + "\n".join(f"  {s.usage:<20} {s.summary}" for s in COMMANDS.values())


class Reply(NamedTuple):
    text: str | None
    ok: bool = True
    quit: bool = False


def execute(db: TinyDB, command: Command) -> Reply:
    match command.name, command.args:
        case "SET", (key, value):
            db.set(key, value)
            return Reply("OK")
        case "GET", (key,):
            value = db.get(key)
            return Reply("(nil)" if value is None else value or '""')
        case "DELETE", (key,):
            return Reply("OK" if db.delete(key) else "(not found)")
        case "EXISTS", (key,):
            return Reply("true" if key in db else "false")
        case "KEYS", args:
            keys = db.keys(*args)
            return Reply("\n".join(f"{i}) {key}" for i, key in enumerate(keys, 1)) or "(empty)")
        case "COUNT", _:
            return Reply(str(len(db)))
        case "STATS", _:
            return Reply(format_stats(db.stats()))
        case "COMPACT", _:
            result = db.compact()
            return Reply(
                f"OK ({human_bytes(result.size_before)} -> {human_bytes(result.size_after)}, "
                f"reclaimed {human_bytes(result.reclaimed)})"
            )
        case "HELP", _:
            return Reply(HELP)
        case "EXIT", _:
            return Reply(None, quit=True)
    raise AssertionError(f"parser accepted a command the shell cannot run: {command}")


def handle_line(db: TinyDB, line: str) -> Reply:
    try:
        command = parse(line)
        return Reply(None) if command is None else execute(db, command)
    except (TinyDBError, ValueError) as exc:
        return Reply(f"ERR {exc}", ok=False)
    except OSError as exc:
        return Reply(f"ERR i/o error: {exc}", ok=False)


def repl(db: TinyDB, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Show a prompt when attached to a terminal; otherwise read commands silently
    (so ``tinydb data.db < script.txt`` works)."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    interactive = stdin.isatty() and stdout.isatty()
    if interactive:
        try:
            import readline  # noqa: F401  (line editing and history, where available)
        except ImportError:
            pass
        print(f"TinyDB {__version__} - {db.path} ({len(db)} keys). Type HELP for commands.", file=stdout)

    while True:
        try:
            line = input(PROMPT) if interactive else stdin.readline()
        except EOFError:
            break
        except KeyboardInterrupt:  # Ctrl-C clears the line, like a normal shell
            print(file=stdout)
            continue
        if not interactive and not line:
            break
        reply = handle_line(db, line)
        if reply.text is not None:
            print(reply.text, file=stdout)
        if reply.quit:
            break


def format_stats(stats: Stats) -> str:
    rows = [
        ("keys", f"{stats.keys:,}"),
        ("file size", human_bytes(stats.file_size)),
        ("live data", human_bytes(stats.live_bytes)),
        ("dead data", f"{human_bytes(stats.dead_bytes)} ({stats.fragmentation:.1%} of records)"),
    ]
    return "\n".join(f"{name:<10} {value}" for name, value in rows)


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{n} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def print_recovery(report: RecoveryReport, err: TextIO) -> None:
    if report.torn_bytes:
        print(
            f"recovered: dropped {report.torn_bytes} bytes of an incomplete write "
            "at the end of the file",
            file=err,
        )
    for c in report.corruptions:
        print(f"repaired: dropped {c.bytes_lost} bytes at offset {c.offset} ({c.reason})", file=err)
    if report.backup_path:
        print(f"repaired: the original file was saved as {report.backup_path}", file=err)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tinydb", description="TinyDB - a lightweight persistent key-value database."
    )
    parser.add_argument("path", nargs="?", default="tinydb.db",
                        help="database file, created if missing (default: %(default)s)")
    parser.add_argument("-c", "--command", action="append", metavar="CMD",
                        help="run CMD and exit; repeat for several commands")
    parser.add_argument("--fsync", action="store_true",
                        help="fsync after every write: survives power loss, much slower")
    parser.add_argument("--repair", action="store_true",
                        help="open a corrupt database by dropping unreadable records "
                             "(the original is backed up first)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):  # never crash printing a value the console can't show
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    try:
        db = TinyDB(args.path, fsync=args.fsync, repair=args.repair)
    except CorruptDatabaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"hint: run `tinydb --repair {args.path}` to keep every readable record "
              "(the original file is backed up first)", file=sys.stderr)
        return 1
    except (TinyDBError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    with db:
        print_recovery(db.recovery, sys.stderr)
        if not args.command:
            repl(db)
            return 0
        status = 0
        for line in args.command:
            reply = handle_line(db, line)
            if reply.text is not None:
                print(reply.text)
            if not reply.ok:
                status = 1
            if reply.quit:
                break
        return status

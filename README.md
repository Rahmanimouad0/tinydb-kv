# TinyDB — A Lightweight Persistent Key-Value Database

A small embedded key-value database built from scratch in pure Python, with no dependencies.
It has an append-only log on disk, a hash index in memory, checksummed binary records,
crash recovery, compaction and a Redis-style shell.

```
tinydb> SET name Mouad
OK
tinydb> SET age 22
OK
tinydb> GET name
Mouad
tinydb> DELETE age
OK
tinydb> GET age
(nil)
```

**Demonstrates:** data structures (hash index, append-only log) · algorithms (log replay,
compaction) · databases (durability, recovery, tombstones) · file I/O (binary formats,
fsync, atomic rename) · software engineering (layered design, 78 unit tests, benchmarks)

## Features

- `SET`, `GET`, `DELETE`, plus `EXISTS`, `KEYS [prefix]`, `COUNT`, `STATS`, `COMPACT`
- **Persistent storage:** all data lives in one append-only file
- **Binary serialization:** length-prefixed records with separate CRC-32 checksums for the header and the body
- **O(1) indexing:** a hash map from each key to its record's location on disk
- **Database loading:** the index is rebuilt by replaying the log at startup
- **Corruption handling:** torn writes are repaired automatically. Real corruption is detected
  and reported with its byte offset, and is never silently "fixed". `--repair` recovers
  everything readable and backs up the original first.
- **Compaction:** rewrites the file without dead records, swapped in with an atomic rename
- **Single-writer lock**, so two processes can't corrupt the same file
- **78 unit tests** and a **benchmark suite** (1k / 10k / 100k keys)

## Quick start

Requires Python 3.11+. Nothing to install.

```bash
python -m tinydb data.db
```

```bash
python -m tinydb data.db -c "SET greeting 'hello world'" -c "GET greeting"
```

```bash
python -m unittest -v
```

```bash
python benchmarks/bench.py
```

Values containing spaces must be quoted: `SET msg "hello world"`. Backslashes are
literal, so `SET path C:\Users\me` works as expected. Commands are case-insensitive;
keys and values are not. When input is piped (`python -m tinydb data.db < script.txt`),
the prompt is left out so the output stays clean.

### As a library

```python
from tinydb import TinyDB

with TinyDB("data.db") as db:
    db.set("name", "Mouad")
    db.get("name")            # 'Mouad'
    db.get("missing")         # None
    db.delete("name")         # True (False if the key did not exist)
    db.keys("user:")          # sorted keys starting with "user:"
    db.stats()                # Stats(keys=..., file_size=..., live_bytes=..., dead_bytes=...)
    db.compact()              # CompactionResult(size_before=..., size_after=...)
```

`TinyDB(path, fsync=True)` makes every write durable against power loss.
`TinyDB(path, repair=True)` opens a corrupt file (see [Corruption handling](#corruption-and-error-handling)).

## Architecture

```
CLI                   tinydb/cli.py       REPL, -c commands, output formatting
 │
 ▼
Command Parser        tinydb/parser.py    tokenizing, quoting, argument checks
 │
 ▼
Database Engine       tinydb/engine.py    SET / GET / DELETE, compaction, stats
 │
 ├── In-memory index   dict: key -> (offset, size) of the latest record
 │
 └── Storage Engine   tinydb/storage.py   append, read, scan, recover, rewrite
        │             tinydb/record.py    binary record format + checksums
        ▼             tinydb/lock.py      one process per database
      Disk            data.db
```

Each layer knows only about the layer below it. The parser never touches the database,
the engine never touches file handles, and only `record.py` knows the byte layout.

### On-disk format

The file starts with an 8-byte header (`TINYDB` + a 16-bit format version), followed by
records, one per `SET` or `DELETE`:

| Offset | Size | Field | Notes |
|---:|---:|---|---|
| 0 | 4 | `body_crc` | CRC-32 of key + value |
| 4 | 1 | `kind` | 1 = PUT, 2 = DELETE (tombstone) |
| 5 | 4 | `key_len` | bytes, 1 – 4,096 |
| 9 | 4 | `value_len` | bytes, 0 – 16 MiB (always 0 for DELETE) |
| 13 | 4 | `header_crc` | CRC-32 of bytes 0–12 |
| 17 | k | key | UTF-8 |
| 17+k | v | value | UTF-8 |

For example, `SET name Mouad` is stored as 17 + 4 + 5 = **26 bytes**.

### Write path: `SET` and `DELETE`

1. Encode the record (`record.encode`).
2. Append it to the end of the file with a single `write()` call.
3. Point the index at the new record.

Nothing is ever modified in place. Overwriting a key appends a new record and leaves the
old one as dead bytes. `DELETE` appends a *tombstone* so the deletion survives a restart.
Sequential appends are the cheapest write a disk can do.

### Read path: `GET`

1. Look up the key in the dict to get `(offset, size)`. A missing key never touches the disk.
2. Seek to the offset and read exactly `size` bytes.
3. Verify both checksums and that the stored key matches, then return the value.

Checksums are verified on every read, so corruption that happens *after* startup is still
caught.

### Loading: rebuilding the index

At startup the log is replayed from start to end. A PUT sets `index[key] = location` and a
DELETE removes the key. Because later records overwrite earlier ones, the last write wins.
This costs O(number of records), which is why compaction matters.

### Compaction

The log only grows, so `COMPACT` rewrites it. Live records are copied, in file order, to
`data.db.compact`. That file is fsync'd and then **atomically renamed** over `data.db`.
A crash at any point leaves either the complete old file or the complete new one, and a
leftover temp file is deleted on the next open. `STATS` shows how many bytes compaction
would reclaim.

## Corruption and error handling

| Situation | How it is detected | What happens |
|---|---|---|
| Crash in the middle of a write (record cut short at end of file) | record length runs past EOF | **Repaired automatically** on open: the partial record is truncated and reported |
| Bit flip in a value | body CRC | Open **refuses** and names the byte offset. `--repair` drops only that record |
| Bit flip in a header | header CRC | Open refuses. `--repair` keeps every record before it |
| Corruption after the database was opened | CRCs re-checked on every `GET` | `GET` raises `CorruptDatabaseError` |
| Not a TinyDB file, or a newer format version | magic bytes / version | Refuses, and **never modifies the file**, even with `--repair` |
| Write fails halfway (disk full, Ctrl-C) | exception from `write()` | The partial record is truncated away, so the log stays consistent |
| Crash during compaction | temp file + atomic rename | Old file intact; temp file removed on next open |
| Two processes open the same database | OS lock on `data.db.lock` | The second gets `DatabaseLockedError` |

**Why the header has its own checksum.** A crash mid-append leaves a record that is
*shorter* than its header claims. That is expected and safe to truncate. A bit flip in a
length field, however, can make a record in the middle of the file *look* like it runs
past EOF, and truncating there would silently delete every record after it. Because the
header is checksummed separately, its lengths are trusted only after they pass the check.
"Runs past EOF" therefore really means "torn write", and anything else is treated as
corruption.

`--repair` (or `repair=True`) copies the original to `data.db.corrupt` before changing
anything. It then keeps every readable record and compacts the result, so the next normal
open is clean.

**Durability.** By default each write goes to the operating system before `SET` returns,
so it survives the process crashing but not a power cut. `--fsync` / `fsync=True` forces it
to the physical disk. The benchmarks below measure what that costs.

## Tests

```bash
python -m unittest -v
```

| File | Covers |
|---|---|
| `tests/test_record.py` | exact byte layout, round trips, **every single-byte flip is detected**, invalid lengths and kinds, UTF-8 |
| `tests/test_engine.py` | SET/GET/DELETE semantics, persistence across reopen, stats, compaction, locking, rollback of a failed write |
| `tests/test_corruption.py` | torn writes, flipped bits in headers and values, garbage, foreign files, repair and backups |
| `tests/test_parser.py` | quoting, aliases, case handling, argument errors |
| `tests/test_cli.py` | the example session, error replies, `-c` mode, exit codes, the repair hint |

## Benchmarks

```bash
python benchmarks/bench.py            # writes benchmarks/RESULTS.md
```

Measured on an AMD Ryzen 7 255 laptop, Windows 11, Python 3.11.9. Keys are
`user:00000000`… with 100-byte values. Lookups and deletes run in random order, and each
figure is the median of 5 runs. Full output is in [benchmarks/RESULTS.md](benchmarks/RESULTS.md).

| Keys | Insert | Lookup | Delete | Startup | Memory | Memory / key | File size |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 5.3 ms · 5.3 µs/op | 4.3 ms · 4.3 µs/op | 4.5 ms · 4.5 µs/op | 2.2 ms | 173.7 KiB | 178 B | 127.0 KiB |
| 10,000 | 48.3 ms · 4.8 µs/op | 40.2 ms · 4.0 µs/op | 46.2 ms · 4.6 µs/op | 17.8 ms | 1.6 MiB | 171 B | 1.2 MiB |
| 100,000 | 1.04 s · 10.4 µs/op | 1.23 s · 12.3 µs/op | 1.05 s · 10.5 µs/op | 405.4 ms | 18.0 MiB | 188 B | 12.4 MiB |

| Experiment | Result |
|---|---|
| fsync on every write (1,000 inserts) | 9.1 µs → 402 µs per insert: **44× slower**, but survives power loss |
| Compaction (10,000 keys × 10 overwrites) | 12.4 MiB → 1.2 MiB, startup 387 ms → 34 ms: **11× faster** |
| Memory with 10 B vs 10,000 B values (10,000 keys) | 1.6 MiB vs 1.9 MiB, while the file grows 250× |

### What the numbers show

- **Operations are O(1).** Insert, lookup and delete cost about 4–5 µs each at 1k and 10k
  keys. That time is almost all one system call (`write`, or `seek` + `read`). The dict
  lookup itself takes 0.1–0.3 µs.
- **The 100k row is noisy on this laptop.** Across three full runs, inserts at 100k keys
  measured 8.7, 4.9 and 10.4 µs/op, while inserts at 1k and 10k stayed within about 10%. A
  standalone profile at 100k measured 4.7–4.9 µs/insert. Two effects are real and
  repeatable. First, random lookups are slower than key-order lookups (81k vs 115k ops/s in
  the run above), because random reads jump around a 12 MB file while key-order reads walk
  it front to back. Second, a larger dict has worse CPU-cache locality.
- **Startup is O(records).** Replaying the log costs about 2 µs per record (1.8–2.2 µs at
  1k and 10k; a standalone 100k measurement gave 1.95 µs). On Windows the *first* open after
  a file is written costs an extra ~10 ms (the OS scans the freshly written file), so the
  table reports the steady-state reopen. `RESULTS.md` lists both.
- **Memory depends on the number of keys, not on the data size.** Values stay on disk, so
  each key costs about 170–200 bytes of Python objects: the key string (~62 B), an
  `(offset, size)` tuple (56 B), the offset int (28 B) and a dict slot. The jump from 171 B
  to 203 B with 1,000-byte values is Python's small-int cache. Record sizes above 256 need
  their own int object.
- **fsync is the price of durability.** Flushing to the OS is cheap. Waiting for the
  physical disk is not.
- **Compaction pays for itself.** After 10 overwrites per key, 90% of the file is dead.
  Removing it cuts both disk use and startup time by about 10×.

## Design trade-offs and limitations

- **Append-only log + hash index** (the design of Bitcask, Riak's storage engine): fast
  writes and simple crash recovery, at the cost of garbage that compaction must clean up.
- **Hash index:** O(1) point lookups but no range queries. `KEYS` scans and sorts in O(n log n).
- **All keys must fit in RAM.** Values don't.
- **Not thread-safe.** Use one instance per thread, or wrap calls in a lock.
- **Compaction is manual** (`COMPACT`). It could be triggered automatically when `STATS`
  shows high fragmentation.

Ideas for future work: a *hint file* (a snapshot of the index) so startup doesn't replay
the whole log; group commit to amortize fsync; `mmap`/`pread` for faster reads; batched
writes; range queries with a sorted index or an SSTable/LSM layout.

## Project structure

```
tinydb/
  cli.py          REPL and command execution
  parser.py       command parsing and validation
  engine.py       TinyDB class: index, SET/GET/DELETE, compaction
  storage.py      file header, append/read, log scan, recovery, atomic rewrite
  record.py       binary record encoding/decoding with CRC-32
  lock.py         cross-platform exclusive file lock
  errors.py       exception types
tests/            78 unit tests (python -m unittest)
benchmarks/
  bench.py        benchmark suite
  RESULTS.md      latest results
```

> **Name clash:** the import name `tinydb` is also used by an unrelated PyPI package
> (TinyDB, a JSON document store). This project runs from its own folder with no install
> step, so the two only clash if both are installed in the same environment.

## License

MIT. See [LICENSE](LICENSE).

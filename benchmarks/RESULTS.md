# TinyDB benchmark results

- **Machine:** AMD Ryzen 7 255 w/ Radeon 780M Graphics, Windows 11
- **Python:** 3.11.9 (CPython)
- **Workload:** keys `user:00000000`…, 100-byte values; lookups and deletes in random order; median of 5 runs; `fsync` off unless stated
- **Date:** 2026-09-25

## Core operations

| Keys | Insert | Lookup | Delete | Startup | Memory | Memory / key | File size |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 5.3 ms · 5.3 µs/op | 4.3 ms · 4.3 µs/op | 4.5 ms · 4.5 µs/op | 2.2 ms | 173.7 KiB | 178 B | 127.0 KiB |
| 10,000 | 48.3 ms · 4.8 µs/op | 40.2 ms · 4.0 µs/op | 46.2 ms · 4.6 µs/op | 17.8 ms | 1.6 MiB | 171 B | 1.2 MiB |
| 100,000 | 1.04 s · 10.4 µs/op | 1.23 s · 12.3 µs/op | 1.05 s · 10.5 µs/op | 405.4 ms | 18.0 MiB | 188 B | 12.4 MiB |

| Keys | Insert ops/s | Lookup ops/s (random) | Lookup ops/s (key order) | Delete ops/s | Startup per record | First open after writing |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 186,954 | 229,933 | 282,326 | 221,396 | 2.19 µs | 12.4 ms |
| 10,000 | 207,092 | 248,514 | 270,590 | 216,386 | 1.78 µs | 28.2 ms |
| 100,000 | 95,702 | 81,077 | 115,427 | 94,859 | 4.05 µs | 422.8 ms |

## Durability: cost of fsync (1,000 inserts)

| Mode | Total | Per insert | Survives |
|---|---:|---:|---|
| `fsync=False` (default) | 9.1 ms | 9.1 µs | process crash |
| `fsync=True` | 402.4 ms | 402.4 µs | process crash + power loss |

fsync makes each write **44× slower**.

## Compaction: 10,000 keys × 10 overwrites

| | Records in log | File size | Startup |
|---|---:|---:|---:|
| Before compaction | 100,000 | 12.4 MiB | 387.4 ms |
| After compaction | 10,000 | 1.2 MiB | 34.2 ms |

Compaction took 103.7 ms and made startup **11.3× faster**.

## Memory vs value size (10,000 keys)

| Value size | Memory | Memory / key | File size |
|---:|---:|---:|---:|
| 10 B | 1.6 MiB | 171 B | 390.6 KiB |
| 100 B | 1.6 MiB | 171 B | 1.2 MiB |
| 1,000 B | 1.9 MiB | 203 B | 9.8 MiB |
| 10,000 B | 1.9 MiB | 203 B | 95.7 MiB |

Values stay on disk, so memory tracks the number of keys, not the data size.

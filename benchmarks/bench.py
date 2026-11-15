"""TinyDB benchmarks.

    python benchmarks/bench.py                          # 1k, 10k, 100k keys
    python benchmarks/bench.py --sizes 1000 50000       # custom sizes
    python benchmarks/bench.py --repeat 5 --value-size 256

Each size runs --repeat times on a fresh database and the median is reported.
Timing and memory are measured in separate passes, because tracemalloc slows
down every allocation it tracks. Results are printed and saved as Markdown.
"""

from __future__ import annotations

import argparse
import gc
import platform
import random
import statistics
import sys
import tempfile
import time
import tracemalloc
from dataclasses import dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tinydb import TinyDB  # noqa: E402

REOPENS = 5  # startup is the median of this many opens


@dataclass
class Run:
    n: int
    insert: float  # seconds for n SETs
    lookup: float  # seconds for n GETs, random order
    lookup_seq: float  # seconds for n GETs, in the order the keys were written
    delete: float  # seconds for n DELETEs, random order
    first_open: float  # seconds for the first open after the file was written
    startup: float  # seconds to reopen a database holding n keys (median)
    memory: int  # bytes held by the open database (index, keys, file handles)
    file_size: int  # bytes on disk after the inserts


def make_dataset(n: int, value_size: int, seed: int = 42):
    rng = random.Random(seed)
    keys = [f"user:{i:08d}" for i in range(n)]
    values = [rng.randbytes((value_size + 1) // 2).hex()[:value_size] for _ in range(n)]
    shuffled = keys[:]
    rng.shuffle(shuffled)
    return keys, values, shuffled


def fresh_path(workdir: Path, name: str) -> Path:
    for leftover in workdir.glob(name + "*"):
        leftover.unlink()
    return workdir / name


def timed_open(path: Path) -> float:
    start = time.perf_counter()
    TinyDB(path).close()
    return time.perf_counter() - start


def open_measuring_memory(path: Path) -> tuple[TinyDB, int]:
    gc.collect()
    tracemalloc.start()
    try:
        db = TinyDB(path)
        memory, _peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return db, memory


def run_once(n: int, dataset, workdir: Path) -> Run:
    keys, values, shuffled = dataset
    path = fresh_path(workdir, f"bench_{n}.db")

    db = TinyDB(path)
    start = time.perf_counter()
    for key, value in zip(keys, values):
        db.set(key, value)
    insert = time.perf_counter() - start

    start = time.perf_counter()
    for key in shuffled:
        db.get(key)
    lookup = time.perf_counter() - start

    start = time.perf_counter()
    for key in keys:
        db.get(key)
    lookup_seq = time.perf_counter() - start

    for i in range(0, n, max(1, n // 1000)):  # spot-check correctness, untimed
        assert db.get(keys[i]) == values[i]
    file_size = db.stats().file_size
    db.close()

    first_open = timed_open(path)
    startup = statistics.median(timed_open(path) for _ in range(REOPENS))

    db, memory = open_measuring_memory(path)
    assert len(db) == n

    start = time.perf_counter()
    for key in shuffled:
        db.delete(key)
    delete = time.perf_counter() - start
    assert len(db) == 0
    db.close()

    return Run(n, insert, lookup, lookup_seq, delete, first_open, startup, memory, file_size)


def median_run(runs: list[Run]) -> Run:
    # median_low picks a real sample, so integer fields stay integers with an even --repeat
    return Run(**{f.name: statistics.median_low(getattr(r, f.name) for r in runs) for f in fields(Run)})


# -- extra experiments ---------------------------------------------------------


def bench_durability(workdir: Path, n: int, value_size: int) -> tuple[float, float]:
    """Cost of fsync: the same n inserts with and without it."""
    keys, values, _ = make_dataset(n, value_size, seed=7)
    results = []
    for fsync in (False, True):
        with TinyDB(fresh_path(workdir, f"durability_{fsync}.db"), fsync=fsync) as db:
            start = time.perf_counter()
            for key, value in zip(keys, values):
                db.set(key, value)
            results.append(time.perf_counter() - start)
    return results[0], results[1]


def bench_compaction(workdir: Path, n: int, versions: int, value_size: int) -> dict[str, float]:
    """n keys each overwritten `versions` times: what compaction buys at startup."""
    keys, values, _ = make_dataset(n, value_size, seed=11)
    path = fresh_path(workdir, "compaction.db")
    with TinyDB(path) as db:
        for _ in range(versions):
            for key, value in zip(keys, values):
                db.set(key, value)

    timed_open(path)  # the first open after a write pays a one-off OS cost; see RESULTS.md
    result = {"records_before": n * versions, "size_before": path.stat().st_size,
              "startup_before": statistics.median(timed_open(path) for _ in range(REOPENS))}
    with TinyDB(path) as db:
        start = time.perf_counter()
        db.compact()
        result["compact_time"] = time.perf_counter() - start
    result["size_after"] = path.stat().st_size
    timed_open(path)
    result["startup_after"] = statistics.median(timed_open(path) for _ in range(REOPENS))
    return result


def bench_memory_vs_value_size(workdir: Path, n: int, value_sizes: list[int]) -> list[tuple[int, int, int]]:
    """Memory should depend on the number of keys, not on how big the values are."""
    rows = []
    for value_size in value_sizes:
        keys, values, _ = make_dataset(n, value_size, seed=3)
        path = fresh_path(workdir, f"values_{value_size}.db")
        with TinyDB(path) as db:
            for key, value in zip(keys, values):
                db.set(key, value)
        db, memory = open_measuring_memory(path)
        db.close()
        rows.append((value_size, memory, path.stat().st_size))
    return rows


# -- formatting ----------------------------------------------------------------


def fmt_time(seconds: float) -> str:
    if seconds < 1e-3:
        return f"{seconds * 1e6:.0f} µs"
    if seconds < 1:
        return f"{seconds * 1e3:.1f} ms"
    return f"{seconds:.2f} s"


def fmt_op(seconds: float, n: int) -> str:
    return f"{fmt_time(seconds)} · {seconds / n * 1e6:.1f} µs/op"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError


def cpu_name() -> str:
    try:
        if sys.platform == "win32":
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        if sys.platform == "darwin":
            import subprocess

            return subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown CPU"


def os_name() -> str:
    system, release = platform.system(), platform.release()
    if system == "Windows" and release == "10":  # Python < 3.12 reports Windows 11 as "10"
        try:
            if int(platform.version().split(".")[2]) >= 22000:
                release = "11"
        except (IndexError, ValueError):
            pass
    return f"{system} {release}"


def render(args, runs: list[Run], durability, compaction, memory_rows) -> str:
    out = []
    out.append("# TinyDB benchmark results\n")
    out.append(f"- **Machine:** {cpu_name()}, {os_name()}")
    out.append(f"- **Python:** {platform.python_version()} ({platform.python_implementation()})")
    out.append(f"- **Workload:** keys `user:00000000`…, {args.value_size}-byte values; "
               f"lookups and deletes in random order; median of {args.repeat} runs; "
               "`fsync` off unless stated")
    out.append(f"- **Date:** {time.strftime('%Y-%m-%d')}\n")

    out.append("## Core operations\n")
    out.append("| Keys | Insert | Lookup | Delete | Startup | Memory | Memory / key | File size |")
    out.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in runs:
        out.append(
            f"| {r.n:,} | {fmt_op(r.insert, r.n)} | {fmt_op(r.lookup, r.n)} | {fmt_op(r.delete, r.n)} "
            f"| {fmt_time(r.startup)} | {fmt_bytes(r.memory)} | {r.memory / r.n:.0f} B "
            f"| {fmt_bytes(r.file_size)} |"
        )
    out.append("")
    out.append("| Keys | Insert ops/s | Lookup ops/s (random) | Lookup ops/s (key order) | Delete ops/s "
               "| Startup per record | First open after writing |")
    out.append("|---:|---:|---:|---:|---:|---:|---:|")
    for r in runs:
        out.append(f"| {r.n:,} | {r.n / r.insert:,.0f} | {r.n / r.lookup:,.0f} | {r.n / r.lookup_seq:,.0f} "
                   f"| {r.n / r.delete:,.0f} | {r.startup / r.n * 1e6:.2f} µs | {fmt_time(r.first_open)} |")

    no_sync, sync = durability
    out.append(f"\n## Durability: cost of fsync ({args.durability_keys:,} inserts)\n")
    out.append("| Mode | Total | Per insert | Survives |")
    out.append("|---|---:|---:|---|")
    out.append(f"| `fsync=False` (default) | {fmt_time(no_sync)} | {no_sync / args.durability_keys * 1e6:.1f} µs "
               "| process crash |")
    out.append(f"| `fsync=True` | {fmt_time(sync)} | {sync / args.durability_keys * 1e6:.1f} µs "
               "| process crash + power loss |")
    out.append(f"\nfsync makes each write **{sync / no_sync:.0f}× slower**.")

    c = compaction
    out.append(f"\n## Compaction: {args.compaction_keys:,} keys × {args.compaction_versions} overwrites\n")
    out.append("| | Records in log | File size | Startup |")
    out.append("|---|---:|---:|---:|")
    out.append(f"| Before compaction | {c['records_before']:,} | {fmt_bytes(c['size_before'])} "
               f"| {fmt_time(c['startup_before'])} |")
    out.append(f"| After compaction | {args.compaction_keys:,} | {fmt_bytes(c['size_after'])} "
               f"| {fmt_time(c['startup_after'])} |")
    out.append(f"\nCompaction took {fmt_time(c['compact_time'])} and made startup "
               f"**{c['startup_before'] / c['startup_after']:.1f}× faster**.")

    out.append(f"\n## Memory vs value size ({args.memory_keys:,} keys)\n")
    out.append("| Value size | Memory | Memory / key | File size |")
    out.append("|---:|---:|---:|---:|")
    for value_size, memory, file_size in memory_rows:
        out.append(f"| {value_size:,} B | {fmt_bytes(memory)} | {memory / args.memory_keys:.0f} B "
                   f"| {fmt_bytes(file_size)} |")
    out.append("\nValues stay on disk, so memory tracks the number of keys, not the data size.")
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1_000, 10_000, 100_000])
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--value-size", type=int, default=100)
    parser.add_argument("--durability-keys", type=int, default=1_000)
    parser.add_argument("--compaction-keys", type=int, default=10_000)
    parser.add_argument("--compaction-versions", type=int, default=10)
    parser.add_argument("--memory-keys", type=int, default=10_000)
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks" / "RESULTS.md")
    args = parser.parse_args()
    sys.stdout.reconfigure(errors="replace")  # the report uses µ and ×

    with tempfile.TemporaryDirectory(prefix="tinydb-bench-") as tmp:
        workdir = Path(tmp)
        runs = []
        for n in args.sizes:
            dataset = make_dataset(n, args.value_size)
            samples = []
            for i in range(args.repeat):
                print(f"  {n:>9,} keys  run {i + 1}/{args.repeat}", file=sys.stderr)
                samples.append(run_once(n, dataset, workdir))
            runs.append(median_run(samples))
        print("  durability", file=sys.stderr)
        durability = bench_durability(workdir, args.durability_keys, args.value_size)
        print("  compaction", file=sys.stderr)
        compaction = bench_compaction(workdir, args.compaction_keys, args.compaction_versions, args.value_size)
        print("  memory vs value size", file=sys.stderr)
        memory_rows = bench_memory_vs_value_size(workdir, args.memory_keys, [10, 100, 1_000, 10_000])

    report = render(args, runs, durability, compaction, memory_rows)
    print(report)
    args.output.write_text(report, encoding="utf-8")
    print(f"saved to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()

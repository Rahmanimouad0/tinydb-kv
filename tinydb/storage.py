"""Storage engine: the database file on disk.

File layout::

    +------------------+----------+----------+-----+----------+
    | "TINYDB" + u16 v | record 0 | record 1 | ... | record n |
    +------------------+----------+----------+-----+----------+
          8 bytes         format described in record.py

Responsibilities:

* create and validate the file header (magic bytes + format version)
* append encoded records, optionally fsync'd, rolling back partial writes
* read a record back from a known ``(offset, size)``
* scan the whole log at startup, telling torn writes apart from corruption
* atomically replace the file when the engine compacts it
* hold an exclusive lock so two processes never write the same file
"""

from __future__ import annotations

import os
import shutil
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from . import record
from .errors import CorruptDatabaseError, InvalidDatabaseFileError, TinyDBError
from .lock import FileLock

MAGIC = b"TINYDB"
FORMAT_VERSION = 1
FILE_HEADER = MAGIC + struct.pack("<H", FORMAT_VERSION)
FILE_HEADER_SIZE = len(FILE_HEADER)

LOCK_SUFFIX = ".lock"
TEMP_SUFFIX = ".compact"  # compaction output, renamed over the database when complete
BACKUP_SUFFIX = ".corrupt"  # copy of a corrupt file, taken before it is repaired

_SCAN_BUFFER = 1 << 20


@dataclass(frozen=True)
class Corruption:
    offset: int
    reason: str
    bytes_lost: int


@dataclass
class RecoveryReport:
    """What happened while the log was replayed at startup."""

    records: int = 0  # valid records replayed
    end_offset: int = FILE_HEADER_SIZE  # first byte after the last record kept
    torn_bytes: int = 0  # incomplete record at the end of the file, dropped
    corruptions: list[Corruption] = field(default_factory=list)  # only with repair=True
    backup_path: str | None = None  # copy of the file taken before repairing it

    @property
    def clean(self) -> bool:
        return not self.torn_bytes and not self.corruptions


class Storage:
    def __init__(self, path: str, *, fsync: bool = False) -> None:
        self.path = path
        self.fsync = fsync
        self._lock = FileLock(path + LOCK_SUFFIX)
        self._writer = None
        self._reader = None
        self._size = 0
        self._failed = False

    @property
    def size(self) -> int:
        """Current length of the file in bytes."""
        return self._size

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Lock the database and make sure the file exists with a valid header.

        Call scan() next to replay the log, then attach() to start reading and
        writing records.
        """
        self._lock.acquire()
        try:
            _remove_if_exists(self.path + TEMP_SUFFIX)  # left behind by a crash mid-compaction
            self._init_header()
        except BaseException:
            self._lock.release()
            raise

    def attach(self, end_offset: int) -> None:
        """Drop everything past ``end_offset`` (a torn write) and open the data handles."""
        if os.path.getsize(self.path) > end_offset:
            with open(self.path, "r+b") as f:
                f.truncate(end_offset)
                os.fsync(f.fileno())
        # Unbuffered: every append is one write() call, every read one read() call.
        self._writer = open(self.path, "ab", buffering=0)
        self._reader = open(self.path, "rb", buffering=0)
        self._size = end_offset

    def close(self) -> None:
        self._close_handles()
        self._lock.release()

    def _close_handles(self) -> None:
        for handle in (self._writer, self._reader):
            if handle is not None:
                handle.close()
        self._writer = self._reader = None

    def _init_header(self) -> None:
        try:
            with open(self.path, "rb") as f:
                head = f.read(FILE_HEADER_SIZE)
        except FileNotFoundError:
            head = b""

        if len(head) < FILE_HEADER_SIZE and FILE_HEADER.startswith(head):
            # A new database, or a crash while its header was being written.
            with open(self.path, "wb") as f:
                f.write(FILE_HEADER)
                f.flush()
                os.fsync(f.fileno())
            _fsync_dir(self.path)
        elif len(head) < FILE_HEADER_SIZE or not head.startswith(MAGIC):
            raise InvalidDatabaseFileError(f"{self.path} is not a TinyDB database file")
        else:
            (version,) = struct.unpack_from("<H", head, len(MAGIC))
            if version != FORMAT_VERSION:
                raise InvalidDatabaseFileError(
                    f"{self.path} uses format version {version}; "
                    f"this build reads version {FORMAT_VERSION}"
                )

    # -- startup -----------------------------------------------------------

    def scan(
        self, report: RecoveryReport, *, skip_corrupt: bool = False
    ) -> Iterator[tuple[int, int, int, str]]:
        """Yield ``(offset, size, kind, key)`` for every valid record, in log order.

        A record cut short by the end of the file is a torn write (the process
        died mid-append): the scan stops and ``report.torn_bytes`` records how
        much to drop. Any other invalid record raises CorruptDatabaseError,
        unless ``skip_corrupt`` is set. Then it is noted in the report and:

        * bad body, good header -> the header gives its length, so skip it
        * bad header            -> the next record cannot be found, so stop
        """
        with open(self.path, "rb", buffering=_SCAN_BUFFER) as f:
            file_size = os.fstat(f.fileno()).st_size
            offset = FILE_HEADER_SIZE
            f.seek(offset)
            while offset < file_size:
                remaining = file_size - offset
                if remaining < record.HEADER_SIZE:
                    report.torn_bytes = remaining
                    return
                try:
                    header = record.decode_header(f.read(record.HEADER_SIZE))
                except record.RecordError as exc:
                    self._on_corruption(report, offset, str(exc), remaining, skip_corrupt)
                    return
                if header.record_size > remaining:
                    report.torn_bytes = remaining
                    return
                try:
                    key, _ = record.decode_body(header, f.read(header.body_size))
                except record.RecordError as exc:
                    self._on_corruption(report, offset, str(exc), header.record_size, skip_corrupt)
                else:
                    report.records += 1
                    yield offset, header.record_size, header.kind, key
                offset += header.record_size
                report.end_offset = offset

    def _on_corruption(
        self, report: RecoveryReport, offset: int, reason: str, bytes_lost: int, skip: bool
    ) -> None:
        if not skip:
            raise CorruptDatabaseError(self.path, offset, reason)
        report.corruptions.append(Corruption(offset, reason, bytes_lost))

    def backup(self) -> str:
        """Copy the file aside (before a repair rewrites it) and return the copy's path."""
        dest = self.path + BACKUP_SUFFIX
        n = 1
        while os.path.exists(dest):
            dest = f"{self.path}{BACKUP_SUFFIX}.{n}"
            n += 1
        shutil.copyfile(self.path, dest)
        return dest

    # -- records -----------------------------------------------------------

    def append(self, data: bytes) -> int:
        """Append one encoded record and return the offset it was written at."""
        self._check_usable()
        offset = self._size
        try:
            view = memoryview(data)
            while view:
                view = view[self._writer.write(view) :]
            if self.fsync:
                os.fsync(self._writer.fileno())
        except BaseException:
            self._rollback(offset)
            raise
        self._size = offset + len(data)
        return offset

    def _rollback(self, offset: int) -> None:
        # A half-written record must not stay in the middle of the log: the next
        # record would be appended after it and the file would read as corrupt.
        try:
            os.ftruncate(self._writer.fileno(), offset)
        except OSError:
            self._failed = True

    def read(self, offset: int, size: int) -> bytes:
        self._check_usable()
        self._reader.seek(offset)
        data = self._reader.read(size)
        if len(data) != size:
            raise CorruptDatabaseError(
                self.path, offset, f"expected {size} bytes, file ends after {len(data)}"
            )
        return data

    def _check_usable(self) -> None:
        if self._failed:
            raise TinyDBError(
                f"{self.path}: a failed write could not be rolled back; reopen the database"
            )

    # -- compaction --------------------------------------------------------

    def rewrite(self, records: Iterable[bytes]) -> None:
        """Atomically replace the file's contents with ``records``.

        The new file is written and fsync'd under a temporary name, then renamed
        over the old one. A crash at any moment leaves either the complete old
        file or the complete new one.
        """
        self._check_usable()
        tmp = self.path + TEMP_SUFFIX
        try:
            with open(tmp, "wb") as f:
                f.write(FILE_HEADER)
                for data in records:
                    f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            _remove_if_exists(tmp)
            raise

        self._close_handles()  # Windows cannot replace a file that is open
        try:
            os.replace(tmp, self.path)
        except BaseException:
            _remove_if_exists(tmp)
            self.attach(self._size)
            raise
        _fsync_dir(self.path)
        self.attach(os.path.getsize(self.path))


def _remove_if_exists(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _fsync_dir(path: str) -> None:
    """Make a create/rename durable (POSIX; Windows has no directory fsync)."""
    if os.name == "nt":
        return
    fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

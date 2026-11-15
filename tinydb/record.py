"""Serialization: how a single change is laid out on disk.

The database file is a log of records. Every SET or DELETE appends one::

    offset  size  field
    ------  ----  -------------------------------------------------
     0      4     body_crc     CRC-32 of key + value
     4      1     kind         1 = PUT, 2 = DELETE (tombstone)
     5      4     key_len      key length in bytes
     9      4     value_len    value length in bytes (0 for DELETE)
    13      4     header_crc   CRC-32 of bytes 0..12
    17      k     key          UTF-8
    17+k    v     value        UTF-8

Integers are little-endian. The header carries its own checksum, so the
lengths can be trusted *before* the body is read. That is what lets the
loader tell a torn write (a record cut short at the end of the file) apart
from real corruption (bytes that changed after they were written).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

PUT = 1
DELETE = 2

MAX_KEY_SIZE = 4 * 1024  # bytes, UTF-8 encoded
MAX_VALUE_SIZE = 16 * 1024 * 1024  # bytes, UTF-8 encoded

_FIELDS = struct.Struct("<IBII")  # body_crc, kind, key_len, value_len
_CRC = struct.Struct("<I")
HEADER_SIZE = _FIELDS.size + _CRC.size  # 17 bytes


class RecordError(ValueError):
    """A record failed validation. The storage engine turns this into
    CorruptDatabaseError, adding the file name and offset."""


@dataclass(frozen=True, slots=True)
class Header:
    body_crc: int
    kind: int
    key_len: int
    value_len: int

    @property
    def body_size(self) -> int:
        return self.key_len + self.value_len

    @property
    def record_size(self) -> int:
        return HEADER_SIZE + self.key_len + self.value_len


def encode(kind: int, key: str, value: str = "") -> bytes:
    """Serialize one record. Raises TypeError / ValueError for invalid input."""
    if not isinstance(key, str) or not isinstance(value, str):
        raise TypeError("keys and values must be str")
    if kind not in (PUT, DELETE):
        raise ValueError(f"unknown record kind {kind}")
    key_bytes = key.encode("utf-8")
    value_bytes = value.encode("utf-8")
    if not key_bytes:
        raise ValueError("key must not be empty")
    if len(key_bytes) > MAX_KEY_SIZE:
        raise ValueError(f"key is {len(key_bytes)} bytes; the limit is {MAX_KEY_SIZE}")
    if len(value_bytes) > MAX_VALUE_SIZE:
        raise ValueError(f"value is {len(value_bytes)} bytes; the limit is {MAX_VALUE_SIZE}")
    if kind == DELETE and value_bytes:
        raise ValueError("a DELETE record cannot carry a value")

    body_crc = zlib.crc32(value_bytes, zlib.crc32(key_bytes))
    fields = _FIELDS.pack(body_crc, kind, len(key_bytes), len(value_bytes))
    return b"".join((fields, _CRC.pack(zlib.crc32(fields)), key_bytes, value_bytes))


def decode_header(buf: bytes) -> Header:
    """Parse and validate the fixed-size header at the start of a record."""
    if len(buf) != HEADER_SIZE:
        raise RecordError(f"header is {len(buf)} bytes, expected {HEADER_SIZE}")
    fields = buf[: _FIELDS.size]
    (header_crc,) = _CRC.unpack_from(buf, _FIELDS.size)
    if zlib.crc32(fields) != header_crc:
        raise RecordError("header checksum mismatch")

    body_crc, kind, key_len, value_len = _FIELDS.unpack(fields)
    if kind not in (PUT, DELETE):
        raise RecordError(f"unknown record kind {kind}")
    if not 0 < key_len <= MAX_KEY_SIZE:
        raise RecordError(f"invalid key length {key_len}")
    if value_len > MAX_VALUE_SIZE or (kind == DELETE and value_len):
        raise RecordError(f"invalid value length {value_len}")
    return Header(body_crc, kind, key_len, value_len)


def decode_body(header: Header, body: bytes) -> tuple[str, str]:
    """Verify the body checksum and return ``(key, value)``."""
    if len(body) != header.body_size:
        raise RecordError(f"body is {len(body)} bytes, expected {header.body_size}")
    if zlib.crc32(body) != header.body_crc:
        raise RecordError("body checksum mismatch")
    try:
        key = body[: header.key_len].decode("utf-8")
        value = body[header.key_len :].decode("utf-8")
    except UnicodeDecodeError:
        raise RecordError("record is not valid UTF-8") from None
    return key, value


def decode(data: bytes) -> tuple[Header, str, str]:
    """Decode one complete record: ``(header, key, value)``."""
    header = decode_header(data[:HEADER_SIZE])
    if len(data) != header.record_size:
        raise RecordError(f"record is {len(data)} bytes, header says {header.record_size}")
    key, value = decode_body(header, data[HEADER_SIZE:])
    return header, key, value

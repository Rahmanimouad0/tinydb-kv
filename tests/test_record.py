import struct
import unittest
import zlib

from tinydb import record
from tinydb.record import DELETE, HEADER_SIZE, MAX_KEY_SIZE, MAX_VALUE_SIZE, PUT, RecordError


def raw_record(kind: int, key: bytes, value: bytes, *, body_crc: int | None = None) -> bytes:
    """Build a record by hand, straight from the format described in record.py."""
    if body_crc is None:
        body_crc = zlib.crc32(key + value)
    fields = struct.pack("<IBII", body_crc, kind, len(key), len(value))
    return fields + struct.pack("<I", zlib.crc32(fields)) + key + value


class EncodeTests(unittest.TestCase):
    def test_matches_documented_layout(self):
        self.assertEqual(record.encode(PUT, "name", "Mouad"), raw_record(PUT, b"name", b"Mouad"))
        self.assertEqual(record.encode(DELETE, "name"), raw_record(DELETE, b"name", b""))

    def test_header_is_17_bytes(self):
        self.assertEqual(HEADER_SIZE, 17)
        self.assertEqual(len(record.encode(PUT, "k", "v")), 17 + 1 + 1)

    def test_sizes_are_counted_in_utf8_bytes(self):
        data = record.encode(PUT, "clé", "日本")
        self.assertEqual(len(data), 17 + 4 + 6)

    def test_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            record.encode(PUT, "", "v")
        with self.assertRaises(ValueError):
            record.encode(PUT, "k" * (MAX_KEY_SIZE + 1), "v")
        with self.assertRaises(ValueError):
            record.encode(PUT, "k", "v" * (MAX_VALUE_SIZE + 1))
        with self.assertRaises(ValueError):
            record.encode(DELETE, "k", "not allowed")
        with self.assertRaises(ValueError):
            record.encode(7, "k", "v")
        with self.assertRaises(TypeError):
            record.encode(PUT, b"k", "v")
        with self.assertRaises(TypeError):
            record.encode(PUT, "k", 42)

    def test_accepts_limits_exactly(self):
        data = record.encode(PUT, "k" * MAX_KEY_SIZE, "")
        self.assertEqual(record.decode(data)[1], "k" * MAX_KEY_SIZE)


class DecodeTests(unittest.TestCase):
    def test_round_trip(self):
        for kind, key, value in [(PUT, "a", "1"), (PUT, "ключ", ""), (DELETE, "gone", "")]:
            header, k, v = record.decode(record.encode(kind, key, value))
            self.assertEqual((header.kind, k, v), (kind, key, value))

    def test_every_single_byte_flip_is_detected(self):
        data = record.encode(PUT, "name", "Mouad")
        for i in range(len(data)):
            corrupted = bytearray(data)
            corrupted[i] ^= 0x01
            with self.subTest(byte=i), self.assertRaises(RecordError):
                record.decode(bytes(corrupted))

    def test_header_checksum_mismatch(self):
        data = bytearray(record.encode(PUT, "k", "v"))
        data[13] ^= 0xFF  # inside header_crc itself
        with self.assertRaisesRegex(RecordError, "header checksum"):
            record.decode_header(bytes(data[:HEADER_SIZE]))

    def test_body_checksum_mismatch(self):
        data = raw_record(PUT, b"k", b"v", body_crc=0)
        with self.assertRaisesRegex(RecordError, "body checksum"):
            record.decode(data)

    def test_rejects_impossible_headers_even_with_a_valid_checksum(self):
        cases = {
            "unknown record kind": raw_record(9, b"k", b"v"),
            "invalid key length": raw_record(PUT, b"", b"v"),
            "invalid value length": raw_record(DELETE, b"k", b"v"),
        }
        for reason, data in cases.items():
            with self.subTest(reason), self.assertRaisesRegex(RecordError, reason):
                record.decode(data)

    def test_rejects_oversized_lengths_before_reading_the_body(self):
        fields = struct.pack("<IBII", 0, PUT, MAX_KEY_SIZE + 1, 0)
        header = fields + struct.pack("<I", zlib.crc32(fields))
        with self.assertRaisesRegex(RecordError, "invalid key length"):
            record.decode_header(header)

    def test_rejects_invalid_utf8(self):
        with self.assertRaisesRegex(RecordError, "UTF-8"):
            record.decode(raw_record(PUT, b"\xff\xfe", b"v"))

    def test_rejects_wrong_length(self):
        data = record.encode(PUT, "k", "value")
        with self.assertRaises(RecordError):
            record.decode(data[:-1])
        with self.assertRaises(RecordError):
            record.decode(data + b"x")


if __name__ == "__main__":
    unittest.main()

import io
import stat
import struct
import unittest
import warnings
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from original_text import extract_zip_text


def archive(entries, compression=ZIP_STORED):
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with ZipFile(buffer, "w", compression=compression) as zipped:
            for name, content in entries:
                zipped.writestr(name, content)
    return buffer.getvalue()


class OriginalTextTests(unittest.TestCase):
    def test_reads_unicode_text_and_keeps_source_code_as_text(self):
        source = "throw new Error('must not execute');"
        payload = archive([("info.json", '{"name":"征集管理"}'),
                           ("nested/extend.js", source), ("empty.css", "")])
        self.assertEqual(extract_zip_text(payload), [
            {"path": "info.json", "content": '{"name":"征集管理"}'},
            {"path": "nested/extend.js", "content": source},
            {"path": "empty.css", "content": ""},
        ])

    def test_skips_directories_and_unsupported_binary_members(self):
        payload = archive([("folder/", b""), ("folder/image.png", b"\x89PNG\xff"),
                           ("folder/note.TXT", "text"), ("nested.zip", b"not interpreted")])
        self.assertEqual(extract_zip_text(payload), [{"path": "folder/note.TXT", "content": "text"}])

    def test_accepts_empty_archive_and_character_limit_boundary(self):
        self.assertEqual(extract_zip_text(archive([])), [])
        self.assertEqual(len(extract_zip_text(archive([("text.txt", "中" * 100000)]))[0]["content"]), 100000)

    def test_rejects_malformed_zip_and_non_bytes_input(self):
        for payload in (b"not a zip", b"PK\x03\x04broken", "text", None):
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(ValueError):
                    extract_zip_text(payload)

    def test_rejects_unsafe_and_duplicate_paths_even_for_binary_files(self):
        for name in ("../escape.txt", "a/../escape.bin", "/absolute.txt", "C:/drive.txt",
                     "C:relative.txt", "a\\..\\escape.txt", "a/./text.txt", "a//text.txt"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    extract_zip_text(archive([(name, "unsafe")]))
        with self.assertRaises(ValueError):
            extract_zip_text(archive([("same.txt", "one"), ("same.txt", "two")]))
        with self.assertRaises(ValueError):
            extract_zip_text(archive([("same/", ""), ("same", "collision")]))

    def test_rejects_symlink(self):
        link = ZipInfo("link.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):
            extract_zip_text(archive([(link, "target.txt")]))

    def test_rejects_encrypted_flag(self):
        payload = bytearray(archive([("secret.txt", "secret")]))
        central = payload.index(b"PK\x01\x02")
        struct.pack_into("<H", payload, 6, struct.unpack_from("<H", payload, 6)[0] | 1)
        struct.pack_into("<H", payload, central + 8, struct.unpack_from("<H", payload, central + 8)[0] | 1)
        with self.assertRaises(ValueError):
            extract_zip_text(bytes(payload))

    def test_rejects_non_utf8_or_binary_text(self):
        for content in (b"\xff\xfe", b"text\x00binary"):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    extract_zip_text(archive([("bad.txt", content)]))

    def test_rejects_corrupt_crc_in_supported_and_skipped_members(self):
        for name in ("bad.txt", "image.png"):
            payload = bytearray(archive([(name, b"unique payload")]))
            payload[payload.index(b"unique payload")] ^= 1
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    extract_zip_text(bytes(payload))

    def test_rejects_input_member_count_and_member_size_limits(self):
        with self.assertRaises(ValueError):
            extract_zip_text(b"x" * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            extract_zip_text(archive([(f"{n}.txt", "") for n in range(101)]))
        with self.assertRaises(ValueError):
            extract_zip_text(archive([("large.bin", b"x" * (1024 * 1024 + 1))], ZIP_DEFLATED))

    def test_rejects_total_uncompressed_ratio_and_text_limits(self):
        with self.assertRaises(ValueError):
            extract_zip_text(archive([(f"{n}.bin", b"x" * 900000) for n in range(5)], ZIP_DEFLATED))
        with self.assertRaises(ValueError):
            extract_zip_text(archive([("bomb.txt", "a" * 50000)], ZIP_DEFLATED))
        with self.assertRaises(ValueError):
            extract_zip_text(archive([("one.txt", "a" * 60000), ("two.txt", "b" * 60000)]))


if __name__ == "__main__":
    unittest.main()

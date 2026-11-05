import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import config
from edrms_client import EdrmsAuthError, EdrmsClient, EdrmsIntegrationError
from models import ArchiveItem


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self, n=-1): return self.body if n < 0 else self.body[:n]


class TestReadOriginal(unittest.TestCase):
    def setUp(self):
        self.client = EdrmsClient(base_url="http://example.invalid")
        self.item = ArchiveItem("title", 2020, [71], "file-1", None, "未确认", {})
        self.metadata = {"success": True, "totalCount": 1, "data": [{"arch_id": "file-1", "fileid": 71, "fileverid": 70, "source_file_name": "a.zip", "source_elec_size": 4}]}

    @patch.object(config, "RUN_MODE", "LIVE")
    def test_missing_auth_rejected_before_network(self):
        with self.assertRaises(EdrmsAuthError):
            self.client.read_original_text("title", "file-1", 71, None)

    @patch.object(config, "RUN_MODE", "LIVE")
    def test_rejects_file_not_in_caller_search_result(self):
        with patch.object(self.client, "search_archives", return_value=([self.item], "ok")):
            with self.assertRaises(EdrmsIntegrationError):
                self.client.read_original_text("title", "other", 71, "Bearer token")

    @patch.object(config, "RUN_MODE", "LIVE")
    def test_rejects_original_not_in_caller_file(self):
        with patch.object(self.client, "search_archives", return_value=([self.item], "ok")):
            with self.assertRaises(EdrmsIntegrationError):
                self.client.read_original_text("title", "file-1", 999, "Bearer token")

    @patch.object(config, "RUN_MODE", "LIVE")
    def test_download_uses_version_id_and_returns_text(self):
        lookup = {"code": 0, "rawList": [{"rawData": json.dumps({"id": "file-1", "archive_type_id": "cat"})}]}
        side = [Response(json.dumps(lookup).encode()), Response(json.dumps(self.metadata).encode())]
        with patch.object(self.client, "search_archives", return_value=([self.item], "ok")), \
             patch("urllib.request.urlopen", side_effect=side), \
             patch("urllib.request.build_opener") as opener, \
             patch("original_text.extract_zip_text", return_value=[{"path": "a.txt", "content": "hello"}]):
            opener.return_value.open.return_value = Response(b"PK\x03\x04")
            result = self.client.read_original_text("title", "file-1", 71, "Bearer token")
        req = opener.return_value.open.call_args.args[0]
        self.assertEqual(parse_qs(urlsplit(req.full_url).query)["fileId"], ["70"])
        self.assertEqual(result["files"], [{"path": "a.txt", "content": "hello"}])
        self.assertEqual(result["attachmentId"], 71)

    @patch.object(config, "RUN_MODE", "LIVE")
    def test_incorrect_size_rejected(self):
        lookup = {"code": 0, "rawList": [{"rawData": json.dumps({"id": "file-1", "archive_type_id": "cat"})}]}
        with patch.object(self.client, "search_archives", return_value=([self.item], "ok")), \
             patch("urllib.request.urlopen", side_effect=[Response(json.dumps(lookup).encode()), Response(json.dumps(self.metadata).encode())]), \
             patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = Response(b"wrong")
            with self.assertRaises(EdrmsIntegrationError):
                self.client.read_original_text("title", "file-1", 71, "Bearer token")

    @patch.object(config, "RUN_MODE", "LIVE")
    def test_transient_download_error_is_retried_but_not_returned(self):
        lookup = {"code": 0, "rawList": [{"rawData": json.dumps({"id": "file-1", "archive_type_id": "cat"})}]}
        with patch.object(self.client, "search_archives", return_value=([self.item], "ok")), \
             patch("urllib.request.urlopen", side_effect=[Response(json.dumps(lookup).encode()), Response(json.dumps(self.metadata).encode())]), \
             patch("urllib.request.build_opener") as opener, \
             patch("original_text.extract_zip_text", return_value=[{"path":"a.txt", "content":"hello"}]):
            opener.return_value.open.side_effect = [Response(b'{"success":false,"code":"System:InternalError"}'), Response(b"PK\x03\x04")]
            result = self.client.read_original_text("title", "file-1", 71, "Bearer token")
        self.assertEqual(result["files"][0]["content"], "hello")
        self.assertEqual(opener.return_value.open.call_count, 2)


if __name__ == "__main__": unittest.main()

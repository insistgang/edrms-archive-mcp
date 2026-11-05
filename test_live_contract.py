import io
import json
import unittest
from unittest.mock import patch

import config
from edrms_client import EdrmsClient


class LiveContractTests(unittest.TestCase):
    def setUp(self):
        self.client = EdrmsClient(base_url="http://example.invalid")

    @patch.object(config, "RUN_MODE", "LIVE")
    def test_live_failure_does_not_fallback_to_mock(self):
        with patch.object(self.client, "_call_live_edrms_search", return_value=(None, "upstream failure")):
            with self.assertRaises(RuntimeError):
                self.client.search_archives("test", "Bearer session")

    @patch.object(config, "RUN_MODE", "LIVE")
    def test_live_requires_identity(self):
        with self.assertRaises(RuntimeError):
            self.client.search_archives("test", None)

    @patch.object(config, "RUN_MODE", "AUTO")
    def test_auto_configured_upstream_does_not_fallback_on_failure(self):
        with patch.object(self.client, "_call_live_edrms_search", return_value=(None, "upstream failure")):
            with self.assertRaises(RuntimeError):
                self.client.search_archives("test", "Bearer session")

    def test_upstream_error_is_not_empty_success(self):
        payload = {"code": 42, "msg": "failure", "rawList": []}
        with patch("urllib.request.urlopen", return_value=Response(payload)):
            items, error = self.client._call_live_edrms_search("test", "Bearer session", None, 1, 10)
        self.assertIsNone(items)
        self.assertTrue(error)

    def test_real_dossier_without_required_fields_is_not_fabricated(self):
        raw = {"title": "案卷1", "id": "case-1", "_index": "edrmsdossier_2025", "filling_date": "2025-11-21"}
        payload = {"code": 0, "total": 1, "rawList": [{"rawData": json.dumps(raw)}]}
        with patch("urllib.request.urlopen", return_value=Response(payload)):
            items, error = self.client._call_live_edrms_search("案卷1", "Bearer session", None, 1, 10)
        self.assertIsNone(items)
        self.assertTrue(error)

    def test_real_empty_response_is_valid(self):
        with patch("urllib.request.urlopen", return_value=Response({"code": 0, "total": 0, "rawList": []})):
            items, error = self.client._call_live_edrms_search("通知", "Bearer session", None, 1, 10)
        self.assertEqual(items, [])
        self.assertIsNone(error)

    def test_live_file_metadata_and_originals_are_joined(self):
        raw = {"id": "file-1", "title": "实机文件", "archive_type_id": "category-1", "dossier_id": "0"}
        detail = {"id": "file-1", "title": "实机文件", "year": 2020, "dossier_id": "0", "document_number": None, "filling_dept": "档案部"}
        original = {"id": "original-1", "arch_id": "file-1", "fileid": 71}
        responses = [Response({"code": 0, "total": 1, "rawList": [{"rawData": json.dumps(raw)}]}),
                     Response({"success": True, "data": [detail]}),
                     Response({"success": True, "data": [original], "totalCount": 1})]
        with patch("urllib.request.urlopen", side_effect=responses) as calls:
            items, error = self.client._call_live_edrms_search("实机文件", "Bearer session", None, 1, 10)
        self.assertIsNone(error)
        self.assertEqual(items[0].to_dict(), {"name": "实机文件", "year": 2020, "attachmentsId": [71], "fileId": "file-1", "caseId": None, "archiveType": "未确认", "archiveTypeId": "category-1", "meta": {"docNo": None, "formUnit": None, "originalsStatus": "ok"}})
        self.assertEqual(json.loads(calls.call_args_list[0].args[0].data)["metaDataType"], 1)
        self.assertEqual(json.loads(calls.call_args_list[2].args[0].data)["nodeId"], "category-1")

    def test_originals_problems_are_reported_without_failing_the_search(self):
        raw = {"id": "file-1", "title": "文件", "archive_type_id": "cat", "dossier_id": "0"}
        detail = {"id": "file-1", "title": "文件", "year": 2020, "dossier_id": "0"}
        for originals, status in (({"success": True, "data": [{"arch_id": "other", "fileid": 5}], "totalCount": 1}, "invalid_association"),
                                  ({"success": True, "data": [{"arch_id": "file-1", "fileid": 5}], "totalCount": 2}, "incomplete"),
                                  ({"success": False, "data": None}, "unavailable")):
            with self.subTest(originals=originals):
                with patch("urllib.request.urlopen", side_effect=[Response({"code": 0, "rawList": [{"rawData": json.dumps(raw)}]}), Response({"success": True, "data": [detail]}), Response(originals)]):
                    items, error = self.client._call_live_edrms_search("文件", "Bearer session", None, 1, 10)
                self.assertIsNone(error)
                self.assertEqual(items[0].attachmentsId, [])
                self.assertEqual(items[0].meta["originalsStatus"], status)

    def test_file_without_known_parent_state_is_rejected_not_hidden(self):
        raw = {"id": "file-1", "title": "文件", "archive_type_id": "cat"}
        detail = {"id": "file-1", "title": "文件", "year": 2020}
        with patch("urllib.request.urlopen", side_effect=[Response({"code": 0, "rawList": [{"rawData": json.dumps(raw)}]}), Response({"success": True, "data": [detail]})]):
            items, error = self.client._call_live_edrms_search("文件", "Bearer session", None, 1, 10)
        self.assertIsNone(items)
        self.assertTrue(error)


class Response:
    def __init__(self, data):
        self.buffer = io.BytesIO(json.dumps(data).encode())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.buffer.close()

    def read(self):
        return self.buffer.read()


if __name__ == "__main__":
    unittest.main()

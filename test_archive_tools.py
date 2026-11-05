import io
import json
import unittest
from unittest.mock import patch

import config
from edrms_client import EdrmsAuthError, EdrmsClient, EdrmsIntegrationError


class JsonResponse:
    def __init__(self, payload):
        self.buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.buffer.close()

    def read(self):
        return self.buffer.read()


class MockArchiveToolTests(unittest.TestCase):
    def setUp(self):
        self.mode = patch.object(config, "RUN_MODE", "MOCK")
        self.mode.start()
        self.client = EdrmsClient(base_url="")
        self.normal = "Bearer token_normal_user"
        self.risk = "Bearer token_risk_director"

    def tearDown(self):
        self.mode.stop()

    def test_search_detail_original_list_and_read_closed_loop(self):
        items, _ = self.client.search_archives("风险防控", self.normal)
        item = items[0]
        detail = self.client.get_archive_detail(
            item.archiveTypeId, "file", item.fileId, self.normal
        )
        self.assertEqual(detail["entity"]["fileId"], item.fileId)
        self.assertEqual(detail["source"]["system"], "MOCK")

        originals = self.client.list_originals(
            item.archiveTypeId, item.fileId, self.normal
        )
        self.assertEqual(originals["page"]["total"], 2)
        attachment_id = originals["items"][0]["attachmentId"]
        result = self.client.read_original(
            item.archiveTypeId, item.fileId, attachment_id, self.normal
        )
        self.assertEqual(result["sourceType"], "MOCK")
        self.assertEqual(result["parser"], "mock_pre_extracted_text")
        self.assertEqual(result["fragments"][0]["source"]["fileId"], item.fileId)
        self.assertEqual(result["fragments"][0]["source"]["attachmentId"], attachment_id)
        self.assertIn("未执行真实文件下载", result["warnings"][0])

    def test_restricted_file_is_denied_at_every_mock_step(self):
        file_id = "F2026-WS-002"
        category = "文书档案（立卷）"
        items, _ = self.client.search_archives("全面风险管理", self.normal)
        self.assertEqual(items, [])
        for call in (
            lambda: self.client.get_archive_detail(category, "file", file_id, self.normal),
            lambda: self.client.list_originals(category, file_id, self.normal),
            lambda: self.client.read_original(category, file_id, 10103, self.normal),
        ):
            with self.subTest(call=call), self.assertRaises(EdrmsIntegrationError):
                call()
        detail = self.client.get_archive_detail(category, "file", file_id, self.risk)
        self.assertEqual(detail["entity"]["fileId"], file_id)

    def test_case_detail_only_returns_files_visible_to_current_user(self):
        normal = self.client.get_archive_detail(
            "文书档案（立卷）", "case", "C2026-WS-AJ01", self.normal, True
        )
        risk = self.client.get_archive_detail(
            "文书档案（立卷）", "case", "C2026-WS-AJ01", self.risk, True
        )
        self.assertEqual([item["fileId"] for item in normal["files"]], ["F2026-WS-001"])
        self.assertEqual(normal["filePage"]["total"], 1)
        self.assertEqual(risk["filePage"]["total"], 2)

    def test_original_pagination_and_output_bounds(self):
        category = "文书档案（立卷）"
        file_id = "F2026-WS-001"
        page = self.client.list_originals(category, file_id, self.normal, 2, 1)
        self.assertEqual(page["page"]["total"], 2)
        self.assertEqual(page["page"]["returnedCount"], 1)
        self.assertEqual(page["items"][0]["attachmentId"], 10102)
        result = self.client.read_original(
            category, file_id, 10101, self.normal, max_chars=12, max_chunks=1
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["fragments"][0]["text"]), 12)

    def test_new_tools_require_identity_even_in_mock(self):
        for call in (
            lambda: self.client.get_archive_detail("文书档案（立卷）", "file", "F2026-WS-001", None),
            lambda: self.client.list_originals("文书档案（立卷）", "F2026-WS-001", None),
            lambda: self.client.read_original("文书档案（立卷）", "F2026-WS-001", 10101, None),
        ):
            with self.subTest(call=call), self.assertRaises(EdrmsAuthError):
                call()


class LiveArchiveToolContractTests(unittest.TestCase):
    def setUp(self):
        self.client = EdrmsClient(base_url="http://example.invalid")
        self.auth = "Bearer user-token"

    def test_live_file_detail_uses_caller_token_and_keeps_unconfirmed_fields_empty(self):
        detail = {"success": True, "data": [{
            "id": "file-1", "title": "实机文件", "year": 2020,
            "dossier_id": "0", "document_number": None, "filling_dept": "不直接映射"
        }]}
        with patch.object(config, "RUN_MODE", "LIVE"), \
             patch("urllib.request.urlopen", return_value=JsonResponse(detail)) as request:
            result = self.client.get_archive_detail("cat", "file", "file-1", self.auth)
        self.assertEqual(result["entity"]["archiveTypeId"], "cat")
        self.assertIsNone(result["entity"]["caseId"])
        self.assertIsNone(result["entity"]["meta"]["formUnit"])
        self.assertEqual(request.call_args.args[0].headers["Token"], "user-token")

    def test_live_original_list_does_not_expose_download_version_id(self):
        detail = {"success": True, "data": [{
            "id": "file-1", "title": "实机文件", "year": 2020, "dossier_id": "0"
        }]}
        originals = {"success": True, "totalCount": 1, "data": [{
            "arch_id": "file-1", "fileid": 71, "fileverid": 70,
            "source_file_name": "a.zip", "source_elec_size": 4
        }]}
        with patch.object(config, "RUN_MODE", "LIVE"), \
             patch("urllib.request.urlopen", side_effect=[JsonResponse(detail), JsonResponse(originals)]):
            result = self.client.list_originals("cat", "file-1", self.auth)
        self.assertEqual(result["items"][0]["attachmentId"], 71)
        self.assertEqual(result["items"][0]["readStatus"], "not_checked")
        self.assertNotIn("versionId", result["items"][0])

    def test_live_case_detail_returns_returned_file_data_with_scope(self):
        detail = {"success": True, "data": [{
            "recordData": {"id": "case-1", "title": "案卷", "year": 2020},
            "fileData": [{"id": "file-1", "title": "卷内文件", "year": 2020}]
        }]}
        with patch.object(config, "RUN_MODE", "LIVE"), \
             patch("urllib.request.urlopen", return_value=JsonResponse(detail)):
            result = self.client.get_archive_detail("cat", "case", "case-1", self.auth, True)
        self.assertEqual(result["files"][0]["caseId"], "case-1")
        self.assertEqual(result["filePage"]["countingScope"], "returned_fileData")

    def test_live_read_still_requires_query_before_any_network_call(self):
        with patch.object(config, "RUN_MODE", "LIVE"), \
             patch("urllib.request.urlopen") as request:
            with self.assertRaises(EdrmsIntegrationError):
                self.client.read_original("cat", "file-1", 71, self.auth)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()

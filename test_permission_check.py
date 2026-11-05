import unittest
from unittest.mock import patch

import config
from edrms_client import EdrmsAuthError, EdrmsClient, EdrmsIntegrationError
from permission_check import Probe, compare, identifiers_of, run


class FakeClient:
    """按令牌区分可见范围的假客户端。"""

    base_url = "http://example.invalid"

    def __init__(self):
        self.visible = {
            "A": {"files": ["file-1"], "attachments": [71]},
            "B": {"files": ["file-2"], "attachments": [72]},
        }

    def search_archives(self, query, auth_header, archive_type_id, page_index, page_size):
        label = "A" if auth_header == "token-a" else "B"
        files = self.visible[label]["files"]
        from models import ArchiveItem
        return [ArchiveItem(name="档案 " + file_id, year=2026, attachmentsId=[71], fileId=file_id,
                            caseId=None, archiveType="一文一件", meta={}, archiveTypeId="cat")
                for file_id in files], "ok"

    def get_archive_detail(self, archive_type_id, entity_type, entity_id, auth_header, *args):
        label = "A" if auth_header == "token-a" else "B"
        if entity_id not in self.visible[label]["files"]:
            raise EdrmsIntegrationError("档案不存在或当前用户无权访问。")
        return {"entityType": "file", "entity": {"fileId": entity_id}}

    def list_originals(self, archive_type_id, file_id, auth_header, page_index, page_size):
        label = "A" if auth_header == "token-a" else "B"
        if file_id not in self.visible[label]["files"]:
            raise EdrmsIntegrationError("档案不存在或当前用户无权访问。")
        return {"fileId": file_id, "items": [{"attachmentId": self.visible[label]["attachments"][0]}],
                "page": {"total": 1}}

    def read_original(self, archive_type_id, file_id, attachment_id, auth_header, query, max_chars, max_chunks):
        label = "A" if auth_header == "token-a" else "B"
        if attachment_id != self.visible[label]["attachments"][0]:
            raise EdrmsIntegrationError("原文不存在、当前用户无权访问或没有可用 Mock 文本。")
        return {"fileId": file_id, "attachmentId": attachment_id,
                "fragments": [{"text": "片段", "source": {"fileId": file_id, "attachmentId": attachment_id}}]}


class PermissionCheckTests(unittest.TestCase):
    def test_identifiers_are_collected_from_nested_results(self):
        value = {"data": [{"fileId": "f1", "name": "甲"}], "items": [{"attachmentId": 71}]}
        found = identifiers_of(value, "fileId", "attachmentId", "name")
        self.assertIn("fileId=f1", found)
        self.assertIn("attachmentId=71", found)
        self.assertIn("name=甲", found)

    def test_probe_does_not_treat_ambiguous_invisibility_as_permission_proof(self):
        client = FakeClient()
        probe = Probe(client, "token-a", "A")
        result = probe.run("detail:file", lambda: client.get_archive_detail("cat", "file", "file-2", "token-a"))
        self.assertIsNone(result["allowed"])
        self.assertIn("不能据此证明", result["reason"])
        self.assertNotIn("value", result)

    def test_probe_reports_missing_identity_as_inconclusive(self):
        probe = Probe(FakeClient(), "", "A")
        result = probe.run("search:x", lambda: (_ for _ in ()).throw(EdrmsAuthError("需要令牌")))
        self.assertIsNone(result["allowed"])

    def test_upstream_outage_is_never_allowed(self):
        result = Probe(FakeClient(), "token-a", "A").run(
            "detail:file", lambda: (_ for _ in ()).throw(EdrmsIntegrationError("超时")))
        self.assertIsNone(result["allowed"])

    def test_tuple_and_entity_identifiers_are_visible(self):
        client = FakeClient()
        self.assertIn("fileId=file-1", identifiers_of(
            client.search_archives("x", "token-a", None, 1, 20), "fileId"))
        self.assertEqual(identifiers_of({"entity": {"fileId": "f"}}, "fileId"), ["fileId=f"])

    def test_duplicate_tokens_are_rejected(self):
        with self.assertRaises(ValueError):
            run(FakeClient(), {"A": "same", "B": "same"}, ["x"], "", None, "")

    def test_read_without_query_is_rejected(self):
        with self.assertRaises(ValueError):
            run(FakeClient(), {"A": "a", "B": "b"}, [], "file-1", 71, "cat")

    def test_compare_flags_different_visible_records_for_review(self):
        results = [
            {"step": "search:x", "user": "A", "allowed": True,
             "value": {"data": [{"fileId": "file-1"}]}},
            {"step": "search:x", "user": "B", "allowed": True,
             "value": {"data": [{"fileId": "file-2"}]}}
        ]
        outcome = compare(results, "search:x")
        self.assertEqual(outcome["verdict"], "REVIEW")

    def test_compare_passes_when_one_user_is_denied(self):
        results = [
            {"step": "detail:file", "user": "A", "allowed": True, "value": {"entity": {"fileId": "file-1"}}},
            {"step": "detail:file", "user": "B", "allowed": False, "reason": "无权"}
        ]
        self.assertEqual(compare(results, "detail:file")["verdict"], "REVIEW")
        expected = {"detail:file": {
            "A": {"allowed": True, "identifiers": ["fileId=file-1"]},
            "B": {"allowed": False}
        }}
        self.assertEqual(compare(results, "detail:file", expected)["verdict"], "PASS")
        expected["detail:file"]["A"]["identifiers"] = ["fileId=other"]
        self.assertEqual(compare(results, "detail:file", expected)["verdict"], "FAIL")

    def test_compare_passes_when_both_are_denied(self):
        results = [
            {"step": "detail:file", "user": "A", "allowed": False, "reason": "无权"},
            {"step": "detail:file", "user": "B", "allowed": False, "reason": "无权"}
        ]
        self.assertEqual(compare(results, "detail:file")["verdict"], "REVIEW")
        expected = {"detail:file": {"A": {"allowed": False}, "B": {"allowed": False}}}
        self.assertEqual(compare(results, "detail:file", expected)["verdict"], "PASS")

    def test_run_produces_steps_for_all_users(self):
        with patch.object(config, "RUN_MODE", "LIVE"):
            report = run(FakeClient(), {"A": "token-a", "B": "token-b"},
                         ["案卷"], "file-1", 71, "cat")
        steps = {step["step"] for step in report["steps"]}
        self.assertIn("search:案卷", steps)
        self.assertIn("detail:file", steps)
        self.assertIn("originals:file", steps)
        self.assertIn("read:original", steps)
        self.assertEqual(len(report["detail"]), 8)
        self.assertTrue(all(step["verdict"] in ("PASS", "REVIEW") for step in report["steps"]))


if __name__ == "__main__":
    unittest.main()

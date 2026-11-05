import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app_standalone
import audit


class AuditTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_hides_token(self):
        token = "Bearer super-secret-token"
        first = audit.fingerprint(token)
        second = audit.fingerprint(token)
        self.assertEqual(first, second)
        self.assertNotIn("super-secret-token", first)
        self.assertEqual(len(first), 12)

    def test_fingerprint_of_missing_token_is_none(self):
        self.assertIsNone(audit.fingerprint(None))
        self.assertIsNone(audit.fingerprint(""))

    def test_no_file_is_written_when_path_is_not_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EDRMS_AUDIT_LOG", None)
            audit.log_event("probe", tool="archive_search")
        self.assertFalse(os.path.exists("audit-should-not-exist.log"))

    def test_event_scrubs_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "audit.jsonl")
            with patch.dict(os.environ, {"EDRMS_AUDIT_LOG": path}):
                audit.log_event("tool_call", tool="archive_search", token="secret-token",
                                content="档案正文", result_count=3, user=audit.fingerprint("secret-token"))
            with open(path, encoding="utf-8") as handle:
                line = handle.readline()
        record = json.loads(line)
        self.assertNotIn("token", record)
        self.assertNotIn("content", record)
        self.assertNotIn("secret-token", line)
        self.assertEqual(record["result_count"], 3)
        self.assertEqual(record["tool"], "archive_search")

    def test_tool_call_records_user_fingerprint_and_count(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "audit.jsonl")
            with patch.dict(os.environ, {"EDRMS_AUDIT_LOG": path}):
                with patch.object(app_standalone.service, "execute_rag") as execute:
                    from models import ArchiveQueryResponse
                    execute.return_value = ArchiveQueryResponse(data=[], msg="ok")
                    app_standalone.MCPRequestHandler._call_archive_tool(
                        "archive_search", {"query": "档案"}, "Bearer user-token")
            with open(path, encoding="utf-8") as handle:
                record = json.loads(handle.readline())
        self.assertEqual(record["event"], "tool_call")
        self.assertEqual(record["tool"], "archive_search")
        self.assertEqual(record["outcome"], "ok")
        self.assertEqual(record["result_count"], 0)
        self.assertEqual(record["user"], audit.fingerprint("Bearer user-token"))
        self.assertNotIn("user-token", json.dumps(record, ensure_ascii=False))

    def test_tool_error_is_audited_without_token(self):
        from edrms_client import EdrmsAuthError
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "audit.jsonl")
            with patch.dict(os.environ, {"EDRMS_AUDIT_LOG": path}):
                with patch.object(app_standalone.service, "execute_rag", side_effect=EdrmsAuthError("需要令牌")):
                    with self.assertRaises(EdrmsAuthError):
                        app_standalone.MCPRequestHandler._call_archive_tool(
                            "archive_search", {"query": "档案"}, "Bearer user-token")
            with open(path, encoding="utf-8") as handle:
                record = json.loads(handle.readline())
        self.assertEqual(record["outcome"], "error")
        self.assertEqual(record["error"], "EdrmsAuthError")


if __name__ == "__main__":
    unittest.main()

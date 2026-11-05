"""Review 2：补齐此前未覆盖的错误分支与边界场景。"""

import io
import json
import threading
import unittest
import zipfile
from http.server import HTTPServer
from unittest.mock import patch

import app_standalone
import config
import live_check
import model_agent
from edrms_client import EdrmsAuthError, EdrmsClient, EdrmsIntegrationError
from model_agent import ModelAgent
from original_parser import extract_archive_text


class Response:
    def __init__(self, payload):
        self.buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.buffer.close()

    def read(self, n=-1):
        return self.buffer.read() if n < 0 else self.buffer.read(n)


class HttpErrorTests(unittest.TestCase):
    """HTTP 兼容端点的鉴权与上游异常分支。"""

    @classmethod
    def setUpClass(cls):
        config.RUN_MODE = "MOCK"

        class Quiet(app_standalone.MCPRequestHandler):
            def log_message(self, *args):
                pass

        cls.server = HTTPServer(("127.0.0.1", 0), Quiet)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path, payload, headers=None):
        import urllib.error
        import urllib.request
        request = urllib.request.Request(
            self.url + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_detail_endpoint_requires_token(self):
        status, data = self._post("/mcp/archive/detail",
                                  {"archiveTypeId": "cat", "entityType": "file", "entityId": "f"})
        self.assertEqual(status, 401)
        self.assertEqual(data["data"], [])

    def test_original_list_endpoint_requires_token(self):
        status, data = self._post("/mcp/archive/originals", {"archiveTypeId": "cat", "fileId": "f"})
        self.assertEqual(status, 401)
        self.assertEqual(data["data"], [])

    def test_original_read_endpoint_requires_token(self):
        status, data = self._post("/mcp/archive/original/read",
                                  {"archiveTypeId": "cat", "fileId": "f", "attachmentId": 1})
        self.assertEqual(status, 401)
        self.assertEqual(data["data"], [])

    def test_endpoints_reject_invalid_arguments(self):
        for path, payload in (
            ("/mcp/archive/detail", {"archiveTypeId": "cat", "entityType": "wrong", "entityId": "f"}),
            ("/mcp/archive/originals", {"fileId": "f"}),
            ("/mcp/archive/original/read", {"archiveTypeId": "cat", "fileId": "f", "attachmentId": 0})
        ):
            with self.subTest(path=path):
                status, _ = self._post(path, payload, {"Authorization": "Bearer token_normal_user"})
                self.assertEqual(status, 400)

    def test_every_tool_rejects_missing_arguments(self):
        cases = [
            ("archive_search", {}),
            ("archive_get_detail", {"archiveTypeId": "cat"}),
            ("archive_list_originals", {"archiveTypeId": "cat"}),
            ("archive_read_original", {"archiveTypeId": "cat", "fileId": "f"})
        ]
        for name, arguments in cases:
            with self.subTest(tool=name):
                status, data = self._post("/mcp", {
                    "jsonrpc": "2.0", "id": 9, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}
                })
                self.assertIn(status, (200, 400))
                self.assertEqual(data.get("error", {}).get("code"), -32602)

    def test_unknown_tool_is_rejected(self):
        status, data = self._post("/mcp", {
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "archive_delete_everything", "arguments": {}}
        })
        self.assertEqual(data.get("error", {}).get("code"), -32602)


class OriginalsEdgeCaseTests(unittest.TestCase):
    def test_file_without_originals_returns_empty_list(self):
        client = EdrmsClient(base_url="http://example.invalid")
        detail = {"success": True, "data": [{"id": "f", "title": "文件", "year": 2020, "dossier_id": "0"}]}
        originals = {"success": True, "data": None, "totalCount": 0}
        with patch.object(config, "RUN_MODE", "LIVE"), \
             patch("urllib.request.urlopen", side_effect=[Response(detail), Response(originals)]):
            result = client.list_originals("cat", "f", "Bearer token")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["page"]["total"], 0)

    def test_unsupported_original_format_is_reported_clearly(self):
        client = EdrmsClient(base_url="http://example.invalid")
        with patch.object(config, "RUN_MODE", "LIVE"), \
             patch.object(client, "_download_original_bytes",
                          return_value={"payload": b"\x89PNG\r\n\x1a\nfake", "fileName": "a.png",
                                        "integrity": "size_verified_only", "version": 1}):
            with self.assertRaises(EdrmsIntegrationError) as context:
                client.read_original("cat", "f", 1, "Bearer token", "关键词", 1000, 5)
        self.assertIn("无法解析", str(context.exception))

    def test_read_original_requires_identity(self):
        client = EdrmsClient(base_url="http://example.invalid")
        with self.assertRaises(EdrmsAuthError):
            client.read_original("cat", "f", 1, None, "关键词")


class ModelAgentEdgeTests(unittest.TestCase):
    def _agent(self, service, script, max_rounds=3):
        handler = script
        state = {"step": 0}

        class Handler:
            pass

        from http.server import BaseHTTPRequestHandler

        class RealHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                state["step"] += 1
                message = handler[min(state["step"] - 1, len(handler) - 1)]
                body = json.dumps({"choices": [{"index": 0, "message": message}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), RealHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        agent = ModelAgent(service, "Bearer token", api_base="http://127.0.0.1:%d/v1" % server.server_address[1],
                           api_key="k", model="m", max_rounds=max_rounds)
        return agent, server

    def test_loop_stops_at_max_rounds(self):
        service = _EndlessService()
        script = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "archive_search", "arguments": "{}"}}]}]
        agent, server = self._agent(service, script, max_rounds=2)
        try:
            result = agent.ask("一直调用")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result["stoppedBy"], "max_rounds")
        self.assertEqual(result["rounds"], 2)

    def test_unknown_tool_error_is_sent_back_to_model(self):
        service = _EndlessService()
        script = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "archive_unknown", "arguments": "{}"}}]},
            {"role": "assistant", "content": "结束", "tool_calls": []}]
        agent, server = self._agent(service, script)
        try:
            result = agent.ask("调用未知工具")
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("未知工具", result["trace"][0]["outputSummary"])

    def test_long_tool_output_is_truncated_for_the_model(self):
        service = _HugeService()
        script = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "archive_search", "arguments": "{}"}}]},
            {"role": "assistant", "content": "结束", "tool_calls": []}]
        agent, server = self._agent(service, script)
        try:
            result = agent.ask("超长输出")
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result["trace"][0]["outputTruncated"])


class _EndlessService:
    def execute_rag(self, request, auth_header):
        from models import ArchiveItem, ArchiveQueryResponse
        return ArchiveQueryResponse(
            data=[ArchiveItem(name="档案", year=2026, attachmentsId=[1], fileId="f",
                              caseId=None, archiveType="一文一件", meta={}, archiveTypeId="c")],
            msg="找到")


class _HugeService(_EndlessService):
    def execute_rag(self, request, auth_header):
        from models import ArchiveItem, ArchiveQueryResponse
        return ArchiveQueryResponse(
            data=[ArchiveItem(name="档案" * 20000, year=2026, attachmentsId=[1], fileId="f",
                              caseId=None, archiveType="一文一件", meta={}, archiveTypeId="c")],
            msg="找到")


class LiveCheckTests(unittest.TestCase):
    def test_run_check_records_failures_and_latency(self):
        class FlakyClient:
            base_url = "http://example.invalid"

            def search_archives(self, query, auth_header, archive_type_id, page_index, page_size):
                from models import ArchiveItem
                items = [ArchiveItem(name="文件", year=2026, attachmentsId=[1], fileId="f1",
                                     caseId=None, archiveType="一文一件", meta={}, archiveTypeId="c")]
                return items, "ok"

            def search_cases(self, query, auth_header):
                return [{"caseId": "c1", "name": "案卷1", "archiveTypeId": "cat"}]

            def get_archive_detail(self, archive_type_id, entity_type, entity_id, auth_header, *args):
                if entity_type == "case":
                    return {"entityType": "case", "entity": {}, "filePage": {"total": 0}}
                raise EdrmsIntegrationError("档案不存在或当前用户无权访问。")

            def list_originals(self, *args, **kwargs):
                raise EdrmsIntegrationError("档案不存在或当前用户无权访问。")

            def read_original(self, *args, **kwargs):
                raise EdrmsIntegrationError("档案不存在或当前用户无权访问。")

        report = live_check.run_check(FlakyClient(), "token", ["档案"], "案卷", 1000, False)
        self.assertEqual(len(report["cases"]), 1)
        self.assertTrue(report["notes"])
        self.assertTrue(report["failures"])
        self.assertIn("search", report["latencyMs"])

    def test_catalog_summary_counts_files_with_and_without_originals(self):
        class CountingClient:
            base_url = "http://example.invalid"

            def search_archives(self, query, auth_header, archive_type_id, page_index, page_size):
                from models import ArchiveItem
                items = [
                    ArchiveItem(name="有原文", year=2026, attachmentsId=[1], fileId="f1",
                                caseId=None, archiveType="一文一件", meta={}, archiveTypeId="c"),
                    ArchiveItem(name="无原文", year=2026, attachmentsId=[], fileId="f2",
                                caseId=None, archiveType="一文一件", meta={}, archiveTypeId="c")
                ]
                return items, "ok"

            def get_archive_detail(self, archive_type_id, entity_type, entity_id, auth_header, *args):
                return {"entityType": "file", "entity": {"fileId": entity_id}}

            def list_originals(self, archive_type_id, file_id, auth_header, page_index, page_size):
                count = 1 if file_id == "f1" else 0
                return {"fileId": file_id, "items": [{"attachmentId": 1}] if count else [],
                        "page": {"total": count}}

            def read_original(self, *args, **kwargs):
                return {"status": "ok", "format": "zip", "truncated": False, "parser": "zip_container",
                        "integrity": "size_verified_only",
                        "fragments": [{"text": "片段", "source": {"path": "a.json"}}]}

        report = live_check.run_check(CountingClient(), "token", [""], "", 1000, False)
        self.assertEqual(len(report["files"]), 2)
        self.assertEqual(sum(1 for item in report["files"] if item.get("originalCount")), 1)


class ParserEdgeTests(unittest.TestCase):
    def test_xlsx_formula_and_inline_string(self):
        sheet = """<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
        <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>内联</t></is></c>
        <c r="B1"><f>SUM(1,2)</f><v>3</v></c></row></sheetData></worksheet>"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/workbook.xml", """<?xml version="1.0"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
             xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
            <sheets><sheet name="表一" sheetId="1" r:id="rId1"/></sheets></workbook>""")
            archive.writestr("xl/_rels/workbook.xml.rels", """<?xml version="1.0"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>""")
            archive.writestr("xl/worksheets/sheet1.xml", sheet)
        parsed = extract_archive_text(buffer.getvalue(), "a.xlsx")
        self.assertEqual(parsed["fragments"][0]["source"]["sheet"], "表一")
        self.assertIn("内联", parsed["fragments"][0]["text"])
        self.assertIn("公式", parsed["fragments"][0]["text"])

    def test_nested_zip_inside_zip_is_reported_not_executed(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("a.txt", "内层文本")
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr("inner.zip", inner.getvalue())
            archive.writestr("b.txt", "外层文本")
        parsed = extract_archive_text(outer.getvalue(), "outer.zip")
        texts = [fragment["text"] for fragment in parsed["fragments"]]
        self.assertIn("外层文本", texts)
        self.assertTrue(any("inner.zip" in warning for warning in parsed["warnings"]))


if __name__ == "__main__":
    unittest.main()

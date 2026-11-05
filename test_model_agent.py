import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import model_agent
from model_agent import ModelAgent


class FakeModelHandler(BaseHTTPRequestHandler):
    """模拟 OpenAI 兼容模型：第一轮要求调用检索工具，第二轮给出最终答案。"""

    script = []
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        FakeModelHandler.requests.append(payload)
        step = len(FakeModelHandler.requests) - 1
        message = FakeModelHandler.script[min(step, len(FakeModelHandler.script) - 1)]
        body = {
            "id": "chatcmpl-test",
            "model": payload.get("model"),
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]
        }
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class FakeService:
    def __init__(self):
        self.calls = []

    def execute_rag(self, request, auth_header):
        self.calls.append(("search", request.query, auth_header))
        from models import ArchiveItem, ArchiveQueryResponse
        return ArchiveQueryResponse(
            data=[ArchiveItem(name="测试档案", year=2026, attachmentsId=[71], fileId="file-1",
                              caseId=None, archiveType="一文一件", meta={}, archiveTypeId="cat-1")],
            msg="找到 1 份档案"
        )

    def get_detail(self, *args, **kwargs):
        self.calls.append(("detail", args[2], args[3]))
        return {"entityType": "file", "entity": {"fileId": args[2], "name": "测试档案"}}

    def list_originals(self, *args, **kwargs):
        self.calls.append(("originals", args[1], args[2]))
        return {"fileId": args[1], "items": [{"attachmentId": 71, "name": "a.zip"}], "page": {"total": 1}}

    def read_original(self, *args, **kwargs):
        self.calls.append(("read", args[1], args[2], args[3]))
        return {
            "fileId": args[1], "attachmentId": args[2], "format": "zip", "status": "ok",
            "fragments": [{"text": "原文片段", "source": {"fileId": args[1], "attachmentId": args[2], "page": 1}}],
            "truncated": False, "parser": "zip_container", "warnings": []
        }


class ModelAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeModelHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/v1" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        FakeModelHandler.script = []
        FakeModelHandler.requests = []

    def _agent(self, service):
        return ModelAgent(service, "Bearer user-token", api_base=self.base, api_key="test-key", model="test-model")

    def test_tool_schemas_follow_mcp_definitions(self):
        schemas = ModelAgent.tool_schemas()
        self.assertEqual(
            [item["function"]["name"] for item in schemas],
            ["archive_search", "archive_get_detail", "archive_list_originals", "archive_read_original"]
        )
        self.assertEqual(schemas[0]["type"], "function")
        self.assertIn("query", schemas[0]["function"]["parameters"]["properties"])

    def test_model_tool_call_loop_produces_answer_and_citations(self):
        FakeModelHandler.script = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "archive_search", "arguments": json.dumps({"query": "风险防控"})}}
            ]},
            {"role": "assistant", "content": "根据有权档案，找到《测试档案》。", "tool_calls": []}
        ]
        service = FakeService()
        result = self._agent(service).ask("有哪些风险防控档案？")
        self.assertIn("测试档案", result["answer"])
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(service.calls[0][0], "search")
        self.assertEqual(service.calls[0][2], "Bearer user-token")
        self.assertEqual(result["citations"][0]["fileId"], "file-1")
        self.assertEqual(len(FakeModelHandler.requests), 2)
        self.assertEqual(FakeModelHandler.requests[0]["tools"][0]["function"]["name"], "archive_search")

    def test_read_original_tool_call_is_traced_with_source(self):
        FakeModelHandler.script = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "archive_read_original", "arguments": json.dumps({
                     "archiveTypeId": "cat-1", "fileId": "file-1", "attachmentId": 71, "query": "风险防控"
                 })}}
            ]},
            {"role": "assistant", "content": "原文显示……", "tool_calls": []}
        ]
        service = FakeService()
        result = self._agent(service).ask("原文写了什么？")
        self.assertEqual(service.calls[0][0], "read")
        self.assertEqual(service.calls[0][3], "Bearer user-token")
        self.assertEqual(result["citations"][0]["page"], 1)
        self.assertEqual(result["trace"][0]["tool"], "archive_read_original")

    def test_tool_failure_is_returned_to_model_without_breaking_the_loop(self):
        class FailingService(FakeService):
            def execute_rag(self, request, auth_header):
                raise RuntimeError("上游检索失败")

        FakeModelHandler.script = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "archive_search", "arguments": json.dumps({"query": "x"})}}
            ]},
            {"role": "assistant", "content": "未能检索到有权档案。", "tool_calls": []}
        ]
        result = self._agent(FailingService()).ask("查一下")
        self.assertIn("未能检索到", result["answer"])
        self.assertIn("上游检索失败", result["trace"][0]["outputSummary"])

    def test_missing_model_configuration_is_explicit(self):
        agent = ModelAgent(FakeService(), "Bearer user-token", api_base="", api_key="", model="")
        with patch.object(model_agent.config, "LLM_API_BASE", ""), \
             patch.object(model_agent.config, "LLM_API_KEY", ""):
            with self.assertRaises(RuntimeError):
                agent.ask("测试")


if __name__ == "__main__":
    unittest.main()

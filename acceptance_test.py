"""整体验收测试：把技术方案中的可测要求逐条落成检查项。

用法：
    python3 acceptance_test.py                 # 只跑本地契约检查
    EDRMS_TOKEN=<用户令牌> python3 acceptance_test.py --live   # 追加实机检查

退出码 0 表示全部通过；非 0 表示存在失败项。
"""

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
import models
from archive_service import ArchiveRagService
from edrms_client import EdrmsAuthError, EdrmsClient, EdrmsIntegrationError
from model_agent import ModelAgent
from original_parser import UnsupportedFormat, extract_archive_text

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Results:
    def __init__(self):
        self.items: List[Tuple[str, str, str]] = []

    def add(self, check: str, status: str, detail: str = "") -> None:
        self.items.append((check, status, detail))

    def run(self, check: str, callable_: Callable[[], Any], skip_reason: str = "") -> None:
        try:
            detail = callable_()
        except SkipCheck as exc:
            self.add(check, SKIP, str(exc) or skip_reason)
            return
        except Exception as exc:
            self.add(check, FAIL, "%s: %s" % (type(exc).__name__, exc))
            return
        self.add(check, PASS, str(detail) if detail else "")

    @property
    def failed(self) -> int:
        return sum(1 for _, status, _ in self.items if status == FAIL)

    @property
    def skipped(self) -> int:
        return sum(1 for _, status, _ in self.items if status == SKIP)

    def report(self) -> None:
        for check, status, detail in self.items:
            print("[%s] %s%s" % (status, check, (" —— " + detail) if detail else ""))


class SkipCheck(Exception):
    pass


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


def start_server() -> Tuple[HTTPServer, str]:
    import app_standalone

    class Quiet(app_standalone.MCPRequestHandler):
        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def http_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
    import urllib.error
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body}


def local_contract_checks(results: Results, server_url: str) -> None:
    def check_tools_registered():
        names = {tool["name"] for tool in models.MCP_ARCHIVE_TOOLS}
        expected = {"archive_search", "archive_get_detail", "archive_list_originals", "archive_read_original"}
        assert names == expected, "工具集合不符：%s" % names
        return "四个工具齐全"

    def check_initialize():
        status, data = http_json(server_url + "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "acc", "version": "1"}}
        })
        assert status == 200, "HTTP %s" % status
        assert data["result"]["serverInfo"]["name"], "缺少 serverInfo"
        return "协议版本 %s" % data["result"]["protocolVersion"]

    def check_tools_list():
        status, data = http_json(server_url + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert status == 200, "HTTP %s" % status
        assert len(data["result"]["tools"]) == 4, "工具数量不符"
        return "tools/list 返回 4 个工具"

    def check_rag_contract():
        status, data = http_json(server_url + "/mcp/archive/rag", {"query": "风险防控"},
                                 {"Authorization": "Bearer token_normal_user"})
        assert status == 200, "HTTP %s" % status
        assert "data" in data and "msg" in data, "缺少 data/msg 字段"
        assert isinstance(data["data"], list), "data 不是数组"
        if data["data"]:
            item = data["data"][0]
            for field in ("name", "year", "attachmentsId", "fileId", "caseId", "archiveType", "meta"):
                assert field in item, "缺少固定字段 %s" % field
        return "固定字段与 data/msg 结构符合接口技术方案"

    def check_missing_token_rejected():
        status, data = http_json(server_url + "/mcp/archive/rag", {"query": "风险防控"})
        assert status in (401, 502), "HTTP %s" % status
        assert not data.get("data"), "无令牌时仍返回了数据"
        return "无令牌返回 %s 且无数据" % status

    def check_no_result_message():
        status, data = http_json(server_url + "/mcp/archive/rag", {"query": "这个词一定查不到xyzzy"},
                                 {"Authorization": "Bearer token_normal_user"})
        assert status == 200, "HTTP %s" % status
        assert data["data"] == [], "无结果时应返回空数组"
        assert "未查询到" in data["msg"], "msg 未表达未查询到"
        return "无结果语义：%s" % data["msg"][:40]

    def mock_file():
        status, data = http_json(server_url + "/mcp/archive/rag", {"query": "风险防控"},
                                 {"Authorization": "Bearer token_normal_user"})
        assert status == 200 and data.get("data"), "未取得 Mock 业务样本"
        return data["data"][0]

    def check_http_detail_route():
        item = mock_file()
        status, data = http_json(server_url + "/mcp/archive/detail",
                                 {"archiveTypeId": item["archiveTypeId"], "entityType": "file", "entityId": item["fileId"]},
                                 {"Authorization": "Bearer token_normal_user"})
        assert status == 200, "HTTP %s" % status
        assert data["entity"]["fileId"] == item["fileId"], "详情文件关联错误"
        return "真实本地 HTTP 调用返回匹配的文件详情"

    def check_http_originals_route():
        item = mock_file()
        status, data = http_json(server_url + "/mcp/archive/originals",
                                 {"archiveTypeId": item["archiveTypeId"], "fileId": item["fileId"]},
                                 {"Authorization": "Bearer token_normal_user"})
        assert status == 200, "HTTP %s" % status
        assert data["fileId"] == item["fileId"] and data["items"], "原文列表缺失或关联错误"
        return "真实本地 HTTP 调用返回非空原文列表"

    def check_http_read_route():
        item = mock_file()
        status, data = http_json(server_url + "/mcp/archive/original/read",
                                 {"archiveTypeId": item["archiveTypeId"], "fileId": item["fileId"],
                                  "attachmentId": item["attachmentsId"][0]},
                                 {"Authorization": "Bearer token_normal_user"})
        assert status == 200, "HTTP %s" % status
        assert data["fragments"] and data["sourceType"] == "MOCK", "缺少 Mock 文本证据"
        assert data["fragments"][0]["source"]["fileId"] == item["fileId"], "原文来源错误"
        return "真实本地 HTTP 调用返回片段和来源（Mock 文本，非业务解析验收）"

    def check_invalid_arguments():
        status, data = http_json(server_url + "/mcp", {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "archive_read_original", "arguments": {"fileId": "x"}}
        })
        assert status in (200, 400), "HTTP %s" % status
        assert data.get("error", {}).get("code") == -32602, "未按 JSON-RPC 拒绝非法参数"
        return "非法参数返回 JSON-RPC -32602（HTTP %s）" % status

    def check_upstream_failure_not_masked():
        import unittest.mock as mock
        service = archive_service_service()
        with mock.patch.object(service, "execute_rag", side_effect=EdrmsIntegrationError("上游异常")):
            with mock.patch("app_standalone.service", service):
                status, data = http_json(server_url + "/mcp/archive/rag", {"query": "x"},
                                         {"Authorization": "Bearer token"})
        assert status == 502 and data["data"] == [], "上游异常未以 502 且空数据返回"
        return "上游异常返回 502 且不伪造数据"

    def check_parser_formats():
        import io
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml",
                             "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
                             "<w:body><w:p><w:r><w:t>正文</w:t></w:r></w:p></w:body></w:document>")
        parsed = extract_archive_text(buffer.getvalue(), "a.docx")
        assert parsed["fragments"], "docx 未解析出文本"
        for payload, name in ((b"%PDF-1.7 broken", "a.pdf"), (b"\x89PNG\r\n\x1a\nx", "a.png")):
            try:
                extract_archive_text(payload, name)
            except UnsupportedFormat:
                continue
            raise AssertionError("%s 未明确报不支持" % name)
        return "docx 可解析，PDF/图片明确报不支持"

    def check_no_token_leak():
        secret = "secret-token-value-for-check"
        try:
            EdrmsClient(base_url="http://example.invalid").get_archive_detail("c", "file", "f", "Bearer " + secret)
        except Exception as exc:
            assert secret not in str(exc), "异常信息包含令牌"
        return "异常信息不含令牌"

    def check_stdlib_only():
        import ast
        import pathlib
        core_files = ["app_standalone.py", "archive_service.py", "config.py", "edrms_client.py",
                      "models.py", "original_parser.py", "original_text.py", "model_agent.py",
                      "live_check.py", "permission_check.py", "run_demo.py"]
        local_modules = {"config", "models", "edrms_client", "archive_service", "original_text",
                         "original_parser", "model_agent", "app_standalone", "permission_check", "audit"}
        violations = []
        for name in core_files:
            path = pathlib.Path(name)
            if not path.exists():
                violations.append("缺少核心模块 %s" % name)
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if root not in sys.stdlib_module_names and root not in local_modules:
                            violations.append("%s: %s" % (name, alias.name))
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    root = node.module.split(".")[0]
                    if root not in sys.stdlib_module_names and root not in local_modules:
                        violations.append("%s: %s" % (name, node.module))
        assert not violations, "存在第三方依赖：%s" % violations
        return "核心模块仅依赖标准库"

    def check_model_tool_loop():
        handler = FakeModelServer()
        server, base = handler.start()
        try:
            agent = ModelAgent(FakeService(), "Bearer user-token",
                               api_base=base + "/v1", api_key="k", model="m")
            outcome = agent.ask("有哪些档案？")
            assert outcome["answer"], "模型未给出答案"
            assert outcome["trace"], "缺少工具调用轨迹"
            assert outcome["citations"], "缺少来源引用"
            return "模型完成工具调用并返回引用"
        finally:
            server.shutdown()
            server.server_close()

    results.run("标准 MCP 注册了四个工具", check_tools_registered)
    results.run("MCP initialize 正常", check_initialize)
    results.run("MCP tools/list 正常", check_tools_list)
    results.run("HTTP 兼容接口固定字段符合接口技术方案", check_rag_contract)
    results.run("无令牌请求被拒绝且不返回数据", check_missing_token_rejected)
    results.run("无结果时不编造、msg 语义正确", check_no_result_message)
    results.run("HTTP 详情端点可达", check_http_detail_route)
    results.run("HTTP 原文列表端点可达", check_http_originals_route)
    results.run("HTTP 原文读取端点可达", check_http_read_route)
    results.run("非法工具参数被 JSON-RPC 拒绝", check_invalid_arguments)
    results.run("上游异常不降级为假数据", check_upstream_failure_not_masked)
    results.run("多格式解析：支持格式可解析、不支持格式明确报错", check_parser_formats)
    results.run("错误信息不含令牌", check_no_token_leak)
    results.run("核心模块无第三方依赖（信创零依赖）", check_stdlib_only)
    results.run("模型工具调用闭环可用", check_model_tool_loop)


def archive_service_service():
    import app_standalone
    return app_standalone.service


class FakeService:
    def execute_rag(self, request, auth_header):
        from models import ArchiveItem, ArchiveQueryResponse
        return ArchiveQueryResponse(
            data=[ArchiveItem(name="档案", year=2026, attachmentsId=[1], fileId="f1",
                              caseId=None, archiveType="一文一件", meta={}, archiveTypeId="c1")],
            msg="找到 1 份")

    def get_detail(self, *args, **kwargs):
        return {"entityType": "file", "entity": {"fileId": args[2]}}

    def list_originals(self, *args, **kwargs):
        return {"fileId": args[1], "items": [{"attachmentId": 1}]}

    def read_original(self, *args, **kwargs):
        return {"fileId": args[1], "attachmentId": args[2],
                "fragments": [{"text": "片段", "source": {"fileId": args[1], "page": 1}}]}


class FakeModelServer:
    def __init__(self):
        self.step = 0

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                outer.step += 1
                if outer.step == 1:
                    message = {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "archive_search", "arguments": json.dumps({"query": "档案"})}}]}
                else:
                    message = {"role": "assistant", "content": "根据有权档案，共 1 份。", "tool_calls": []}
                body = json.dumps({"choices": [{"index": 0, "message": message}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, "http://127.0.0.1:%d" % server.server_address[1]


def live_checks(results: Results, token: str, base_url: str, server_url: str = "") -> None:
    config.RUN_MODE = "LIVE"
    original_query = os.getenv("EDRMS_ORIGINAL_QUERY", "").strip()
    if not base_url or not original_query:
        results.add("实机检查", SKIP, "需要EDRMS_BASE_URL及EDRMS_ORIGINAL_QUERY，不使用硬编码客户样本")
        return
    client = EdrmsClient(base_url=base_url)
    import app_standalone
    app_standalone.service.client = client

    def rpc_tool(name, arguments):
        status, body = http_json(server_url + "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}
        }, {"Authorization": "Bearer " + token})
        assert status == 200 and "error" not in body, "MCP 协议或 HTTP 返回错误"
        result = body["result"]
        assert not result.get("isError"), "MCP 工具调用失败"
        structured = result["structuredContent"]
        assert json.loads(result["content"][0]["text"]) == structured, "结构化与文本结果不一致"
        return structured

    def check_live_mcp_chain():
        if not server_url:
            raise SkipCheck("未启动 MCP HTTP 服务")
        search = rpc_tool("archive_search", {"query": original_query})
        assert len(search["data"]) == 1, "目标文件不存在或不唯一"
        item = search["data"][0]
        common = {"archiveTypeId": item["archiveTypeId"], "fileId": item["fileId"]}
        detail = rpc_tool("archive_get_detail", {
            "archiveTypeId": item["archiveTypeId"], "entityType": "file", "entityId": item["fileId"]})
        assert detail["entity"]["fileId"] == item["fileId"], "详情关联错误"
        originals = rpc_tool("archive_list_originals", common)
        expected_count = int(os.getenv("EDRMS_EXPECTED_ORIGINAL_COUNT", "0"))
        assert originals["items"], "目标样本没有可读取的原文"
        if expected_count:
            assert len(originals["items"]) == expected_count, "原文数量与配置的预期不一致"
        for original in originals["items"]:
            read = rpc_tool("archive_read_original", dict(common,
                attachmentId=original["attachmentId"], query=original_query, maxChars=100000, maxChunks=100))
            assert read["sourceType"] == "LIVE" and read["fragments"], "未返回实机片段"
            assert not read["truncated"], "测试 ZIP 未完整返回"
            assert all(fragment["source"]["fileId"] == item["fileId"] and
                       fragment["source"]["attachmentId"] == original["attachmentId"]
                       for fragment in read["fragments"]), "片段来源关联错误"
        return "标准 MCP HTTP 四工具闭环，%d 条原文完整读取且来源一致" % len(originals["items"])

    def check_live_search():
        items, message = client.search_archives(original_query, "Bearer " + token, None, 1, 20)
        assert items, "实机检索未返回记录：%s" % message
        item = items[0]
        for field in ("name", "year", "attachmentsId", "fileId", "caseId", "archiveType", "meta"):
            assert hasattr(item, field), "缺少字段 %s" % field
        return "返回 %d 条，字段完整" % len(items)

    def check_live_detail():
        items, _ = client.search_archives(original_query, "Bearer " + token, None, 1, 20)
        assert items, "未检索到目标文件"
        item = items[0]
        detail = client.get_archive_detail(item.archiveTypeId, "file", item.fileId, "Bearer " + token)
        assert detail["entity"]["name"], "详情缺少题名"
        return "详情题名：%s" % detail["entity"]["name"]

    def check_live_originals():
        items, _ = client.search_archives(original_query, "Bearer " + token, None, 1, 20)
        item = items[0]
        originals = client.list_originals(item.archiveTypeId, item.fileId, "Bearer " + token, 1, 100)
        assert originals["items"], "未返回原文记录"
        return "原文记录 %d 条" % originals["page"]["total"]

    def check_live_read():
        items, _ = client.search_archives(original_query, "Bearer " + token, None, 1, 20)
        item = items[0]
        originals = client.list_originals(item.archiveTypeId, item.fileId, "Bearer " + token, 1, 100)
        attachment_id = originals["items"][0]["attachmentId"]
        result = client.read_original(item.archiveTypeId, item.fileId, attachment_id,
                                      "Bearer " + token, original_query, 50000, 50)
        assert result["fragments"], "未返回文本片段"
        assert all("fileId" in f["source"] for f in result["fragments"]), "片段缺少来源"
        return "读取 %s，共 %d 字符" % (result["format"], sum(len(f["text"]) for f in result["fragments"]))

    def check_live_cases():
        case_query = os.getenv("EDRMS_CASE_QUERY", "").strip()
        if not case_query:
            raise SkipCheck("未配置EDRMS_CASE_QUERY，案卷实测未完成")
        cases = client.search_cases(case_query, "Bearer " + token)
        return "案卷 %d 个" % len(cases)

    results.run("实机检索返回完整字段", check_live_search)
    results.run("实机文件详情可读", check_live_detail)
    results.run("实机原文列表可读", check_live_originals)
    results.run("实机原文读取并带来源", check_live_read)
    results.run("实机案卷目录可检索", check_live_cases)
    results.run("标准 MCP 实机四工具及全部 ZIP 闭环", check_live_mcp_chain)


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP 整体验收测试")
    parser.add_argument("--live", action="store_true", help="追加实机检查")
    parser.add_argument("--token-env", default="EDRMS_TOKEN")
    parser.add_argument("--base-url", default=config.EDRMS_BASE_URL)
    args = parser.parse_args()

    results = Results()
    config.RUN_MODE = "MOCK"
    server, server_url = start_server()
    try:
        local_contract_checks(results, server_url)
        if args.live:
            token = os.getenv(args.token_env, "").strip()
            if not token:
                results.add("实机检查", SKIP, "未提供 %s" % args.token_env)
            else:
                live_checks(results, token, args.base_url, server_url)
    finally:
        server.shutdown()
        server.server_close()

    print("=== 整体验收结果 ===")
    results.report()
    print("通过 %d 项，失败 %d 项，跳过 %d 项" % (
        len(results.items) - results.failed - results.skipped, results.failed, results.skipped))
    if results.failed:
        return 1
    return 2 if results.skipped else 0


if __name__ == "__main__":
    sys.exit(main())

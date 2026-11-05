import json
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import audit
import config
from models import ArchiveQueryRequest, MCP_ARCHIVE_TOOLS
from archive_service import ArchiveRagService
from edrms_client import EdrmsAuthError, EdrmsIntegrationError

service = ArchiveRagService()

# Minimal stateless JSON-response Streamable HTTP; no SSE or server sessions.
MCP_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")


def _dispatch_archive_tool(tool_name, arguments, auth_header):
    """按工具名分发到业务层，HTTP 与 MCP 两个入口共用同一实现。"""
    if tool_name == "archive_search":
        response = service.execute_rag(ArchiveQueryRequest.from_dict(arguments), auth_header)
        return response.to_dict()
    if tool_name == "archive_get_detail":
        return service.get_detail(
            arguments["archiveTypeId"], arguments["entityType"], arguments["entityId"], auth_header,
            arguments.get("includeFiles", False), arguments.get("filePageIndex", 1),
            arguments.get("filePageSize", 20)
        )
    if tool_name == "archive_list_originals":
        return service.list_originals(
            arguments["archiveTypeId"], arguments["fileId"], auth_header,
            arguments.get("pageIndex", 1), arguments.get("pageSize", 20)
        )
    return service.read_original(
        arguments["archiveTypeId"], arguments["fileId"], arguments["attachmentId"], auth_header,
        arguments.get("query"), arguments.get("maxChars", 20000), arguments.get("maxChunks", 10)
    )


def _result_count(body):
    """统计结果条数用于审计，不记录具体内容。"""
    if not isinstance(body, dict):
        return None
    for key in ("data", "items", "fragments"):
        value = body.get(key)
        if isinstance(value, list):
            return len(value)
    return None


class MCPRequestHandler(BaseHTTPRequestHandler):
    def _send_json_response(self, status_code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 默认同源访问；如确需跨域，由部署方显式设置 MCP_ALLOW_CORS_ORIGIN，禁止通配符放开。
        allowed_origin = os.getenv("MCP_ALLOW_CORS_ORIGIN", "").strip()
        if allowed_origin and allowed_origin != "*" and self.path != "/mcp" and self.headers.get("Origin") == allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, MCP-Protocol-Version")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if self.path == "/mcp" and not self._mcp_transport_ok():
            return
        allowed_origin = os.getenv("MCP_ALLOW_CORS_ORIGIN", "").strip()
        self.send_response(204)
        if allowed_origin and allowed_origin != "*" and self.path != "/mcp" and self.headers.get("Origin") == allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, MCP-Protocol-Version")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()

    def do_GET(self):
        if self.path == "/mcp":
            if self._mcp_transport_ok():
                self._send_empty_response(405, allow="POST, OPTIONS")
            return
        if self.path == "/health":
            self._send_json_response(200, {
                "status": "healthy",
                "mode": config.RUN_MODE,
                "edrms_target": config.EDRMS_BASE_URL or "LOCAL_MOCK",
                "system": service.client.get_system_version()
            })
            return

        # 默认返回轻量可视化交互控制台页面
        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>华泰证券 EDRMS MCP 智能检索交互控制台</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", sans-serif; margin: 0; background: #f0f2f5; color: #1f2328; line-height: 1.5; }}
        .header {{ background: #003366; color: #fff; padding: 20px 30px; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }}
        .header h1 {{ margin: 0; font-size: 22px; font-weight: 600; }}
        .header p {{ margin: 5px 0 0; opacity: 0.85; font-size: 13px; }}
        .container {{ max-width: 1000px; margin: 25px auto; padding: 0 20px; }}
        .card {{ background: #fff; border-radius: 8px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); border: 1px solid #e1e4e8; }}
        .form-row {{ display: flex; gap: 15px; margin-bottom: 15px; flex-wrap: wrap; }}
        .form-group {{ flex: 1; min-width: 250px; }}
        label {{ display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; color: #444; }}
        input, select {{ width: 100%; padding: 10px 12px; border: 1px solid #d0d7de; border-radius: 6px; font-size: 14px; outline: none; }}
        input:focus, select:focus {{ border-color: #0969da; box-shadow: 0 0 0 3px rgba(9,105,218,0.15); }}
        .btn-primary {{ background: #0052cc; color: white; border: none; padding: 10px 22px; border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer; transition: background 0.2s; }}
        .btn-primary:hover {{ background: #0043a8; }}
        .quick-tags {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; align-items: center; }}
        .quick-tag {{ background: #eef2f6; border: 1px solid #cfd8dc; color: #24292f; padding: 4px 10px; border-radius: 14px; font-size: 12px; cursor: pointer; transition: all 0.15s; }}
        .quick-tag:hover {{ background: #0052cc; color: #fff; border-color: #0052cc; }}
        .alert-box {{ background: #e8f4fd; border-left: 4px solid #1976d2; padding: 14px 18px; border-radius: 4px; margin-bottom: 20px; font-size: 14px; color: #0d47a1; }}
        .result-section {{ display: none; }}
        .ai-bubble {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 8px; padding: 16px; margin-bottom: 18px; position: relative; }}
        .ai-title {{ font-size: 13px; font-weight: bold; color: #57606a; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }}
        .ai-msg {{ font-size: 15px; color: #003366; font-weight: 500; }}
        .doc-card {{ border: 1px solid #e1e4e8; border-radius: 6px; padding: 14px 18px; margin-bottom: 12px; background: #fafbfc; }}
        .doc-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
        .doc-title {{ font-size: 15px; font-weight: 600; color: #0969da; }}
        .badge {{ font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: bold; }}
        .badge-case {{ background: #e1f5fe; color: #0288d1; }}
        .badge-single {{ background: #ede7f6; color: #512da8; }}
        .doc-meta {{ font-size: 13px; color: #656d76; margin-bottom: 6px; }}
        .attach-list {{ font-size: 12px; color: #57606a; background: #fff; border: 1px dashed #d0d7de; padding: 6px 10px; border-radius: 4px; display: inline-block; }}
        pre {{ background: #24292e; color: #f6f8fa; padding: 15px; border-radius: 6px; overflow-x: auto; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🏛️ 华泰证券数字档案大模型 MCP 服务 (路线 A 原型控制台)</h1>
        <p>EDRMS-V6.6.0.0 适配层 · 信创零依赖原生运行 · 物理级权限前置过滤</p>
    </div>

    <div class="container">
        <div class="card">
            <div class="alert-box">
                💡 <strong>当前运行模式：</strong> {config.RUN_MODE} | <strong>后端引擎：</strong> {config.EDRMS_BASE_URL or "本地高仿真档案库 (Mock)"} | <strong>服务端口：</strong> {config.SERVER_PORT}
            </div>

            <div class="form-row">
                <div class="form-group" style="flex: 2;">
                    <label>🔍 检索问题 / 关键词 (Query)</label>
                    <input type="text" id="queryInput" value="2026年 风险防控" placeholder="请输入如：2026年 风险防控通知">
                </div>
                <div class="form-group" style="flex: 1;">
                    <label>👤 模拟提问身份 (Token 权限标签)</label>
                    <select id="tokenSelect">
                        <option value="token_normal_user">普通员工 (张三) - 仅内部公开</option>
                        <option value="token_compliance_officer">合规专员 (李四) - 秘密级合规档案</option>
                        <option value="token_risk_director">风控总监 (王五) - 机密级全面风控</option>
                    </select>
                </div>
                <div style="display: flex; align-items: flex-end;">
                    <button class="btn-primary" onclick="doSearch()">🚀 执行 MCP 检索</button>
                </div>
            </div>

            <div class="quick-tags">
                <span style="font-size: 12px; color: #666;">快捷测试场景：</span>
                <span class="quick-tag" onclick="setTest('2026年 风险防控', 'token_normal_user')">场景1: 普通人查公开风控通知</span>
                <span class="quick-tag" onclick="setTest('全面风险管理 工作方案', 'token_risk_director')">场景2: 风控总监查机密工作方案</span>
                <span class="quick-tag" onclick="setTest('反洗钱 客户尽职调查', 'token_compliance_officer')">场景3: 合规专员查反洗钱秘密文件</span>
                <span class="quick-tag" onclick="setTest('衍生品业务双重风控审批机制', 'token_normal_user')" style="color:#d32f2f;">场景4: 普通人越权查机密(测拦截)</span>
                <span class="quick-tag" onclick="setTest('证监会 合规管理办法', 'token_normal_user')">场景5: 跨门类查监管收文</span>
            </div>
        </div>

        <div class="card result-section" id="resultCard">
            <h3 style="margin-top: 0; font-size: 16px; border-bottom: 1px solid #eee; padding-bottom: 10px;">📋 检索与问答输出结果</h3>

            <div class="ai-bubble">
                <div class="ai-title">🤖 LLM 自然语言回答 (msg) - 严格防幻觉与权限过滤</div>
                <div class="ai-msg" id="aiMsg">检索中...</div>
            </div>

            <h4 style="font-size: 14px; margin: 15px 0 10px;">📦 命中有权档案列表 (data 数组 · <span id="hitCount">0</span> 份)</h4>
            <div id="docList"></div>

            <details style="margin-top: 20px;">
                <summary style="font-size: 13px; color: #666; cursor: pointer;">🔍 查看底层原始 JSON 报文 (符合客户技术文档规范)</summary>
                <pre id="jsonRaw"></pre>
            </details>
        </div>
    </div>

    <script>
        function setTest(query, token) {{
            document.getElementById('queryInput').value = query;
            document.getElementById('tokenSelect').value = token;
            doSearch();
        }}

        async function doSearch() {{
            const query = document.getElementById('queryInput').value.trim();
            const token = document.getElementById('tokenSelect').value;
            const resultCard = document.getElementById('resultCard');
            const aiMsg = document.getElementById('aiMsg');
            const docList = document.getElementById('docList');
            const hitCount = document.getElementById('hitCount');
            const jsonRaw = document.getElementById('jsonRaw');

            resultCard.style.display = 'block';
            aiMsg.innerText = '正在调取 EDRMS 执行物理级权限过滤与检索...';
            docList.innerHTML = '';

            try {{
                const resp = await fetch('/mcp/archive/rag', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json',
                        'Authorization': 'Bearer ' + token
                    }},
                    body: JSON.stringify({{ query: query }})
                }});

                const res = await resp.json();
                jsonRaw.innerText = JSON.stringify(res, null, 2);
                aiMsg.innerText = res.msg || '无回答';
                hitCount.innerText = res.data ? res.data.length : 0;

                if (!res.data || res.data.length === 0) {{
                    docList.innerHTML = '<div style="color: #d32f2f; padding: 15px; background: #fff5f5; border-radius: 6px; font-size: 14px;">⛔ 物理拦截成功：当前身份在此关键词下无任何可查阅档案权限，0 记录泄露。</div>';
                    return;
                }}

                res.data.forEach(item => {{
                    const badgeClass = item.archiveType === '立卷' ? 'badge-case' : 'badge-single';
                    const caseLabel = item.caseId ? ('所属案卷：' + item.caseId) : '一文一件';
                    const docNo = (item.meta && item.meta.docNo) ? item.meta.docNo : '-';
                    const unit = (item.meta && item.meta.formUnit) ? item.meta.formUnit : '-';
                    const summary = (item.meta && item.meta.summary) ? item.meta.summary : '无';
                    const attachStr = (item.attachmentsId && item.attachmentsId.length) ? item.attachmentsId.join(', ') : '无';

                    const html = `
                        <div class="doc-card">
                            <div class="doc-header">
                                <span class="doc-title">${{item.name}}</span>
                                <span class="badge ${{badgeClass}}">${{item.archiveType}}</span>
                            </div>
                            <div class="doc-meta">
                                <strong>年度：</strong>${{item.year}}年 | <strong>文号：</strong>${{docNo}} | <strong>${{caseLabel}}</strong> | <strong>主键ID：</strong>${{item.fileId}}
                            </div>
                            <div class="doc-meta">
                                <strong>形成部门：</strong>${{unit}} | <strong>摘要：</strong>${{summary}}
                            </div>
                            <div class="attach-list">
                                📎 <strong>挂接电子原文附件ID：</strong> [${{attachStr}}]
                            </div>
                        </div>
                    `;
                    docList.innerHTML += html;
                }});
            }} catch (e) {{
                aiMsg.innerText = '请求失败: ' + e.message;
                docList.innerHTML = '<div style="color: red;">无法连接服务，请检查端口是否正常</div>';
            }}
        }}

        // 页面打开自动搜一次默认词
        window.onload = doSearch;
    </script>
</body>
</html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty_response(self, status_code, allow=None):
        self.send_response(status_code)
        self.send_header("Content-Length", "0")
        if allow:
            self.send_header("Allow", allow)
        self.end_headers()

    def _mcp_error(self, request_id, code, message, status=200):
        self._send_json_response(status, {
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}
        })

    def _mcp_transport_ok(self):
        origin = self.headers.get("Origin")
        if origin:
            allowed = {f"http://127.0.0.1:{self.server.server_port}",
                       f"http://localhost:{self.server.server_port}",
                       f"http://[::1]:{self.server.server_port}"}
            if origin not in allowed:
                self._mcp_error(None, -32000, "Origin is not allowed", 403)
                return False
        version = self.headers.get("MCP-Protocol-Version")
        if version and version not in MCP_PROTOCOL_VERSIONS:
            self._mcp_error(None, -32600, "Unsupported MCP protocol version", 400)
            return False
        return True

    @staticmethod
    def _validate_tool_arguments(tool_name, arguments):
        if not isinstance(arguments, dict):
            return "Tool arguments must be an object"

        def required_string(field):
            return isinstance(arguments.get(field), str) and bool(arguments[field].strip())

        def valid_page(index_field, size_field, default_size):
            for field, default in ((index_field, 1), (size_field, default_size)):
                value = arguments.get(field, default)
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    return False
                if field == size_field and value > 100:
                    return False
            return True

        if tool_name == "archive_search":
            if not required_string("query"):
                return "A non-empty query string is required"
            if arguments.get("archiveTypeId") is not None and not isinstance(arguments["archiveTypeId"], str):
                return "archiveTypeId must be a string"
            if not valid_page("pageIndex", "pageSize", 10):
                return "Invalid pagination parameter"
            from search_filters import validate_filters
            try:
                validate_filters(arguments.get("filters"))
            except ValueError as exc:
                return str(exc)
        elif tool_name == "archive_get_detail":
            if not all(required_string(field) for field in ("archiveTypeId", "entityType", "entityId")):
                return "archiveTypeId, entityType and entityId are required strings"
            if arguments["entityType"] not in ("file", "case"):
                return "entityType must be file or case"
            if not isinstance(arguments.get("includeFiles", False), bool):
                return "includeFiles must be boolean"
            if not valid_page("filePageIndex", "filePageSize", 20):
                return "Invalid file pagination parameter"
        elif tool_name == "archive_list_originals":
            if not all(required_string(field) for field in ("archiveTypeId", "fileId")):
                return "archiveTypeId and fileId are required strings"
            if not valid_page("pageIndex", "pageSize", 20):
                return "Invalid pagination parameter"
        elif tool_name == "archive_read_original":
            if not all(required_string(field) for field in ("archiveTypeId", "fileId")):
                return "archiveTypeId and fileId are required strings"
            attachment_id = arguments.get("attachmentId")
            if isinstance(attachment_id, bool) or not isinstance(attachment_id, int) or attachment_id < 1:
                return "A positive integer attachmentId is required"
            if "query" in arguments and not required_string("query"):
                return "query must be a non-empty string when provided"
            for field, default, maximum in (("maxChars", 20000, 100000), ("maxChunks", 10, 100)):
                value = arguments.get(field, default)
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                    return f"Invalid {field}"
        return None

    @staticmethod
    def _call_archive_tool(tool_name, arguments, auth_header):
        started = time.perf_counter()
        try:
            body = _dispatch_archive_tool(tool_name, arguments, auth_header)
        except (EdrmsAuthError, EdrmsIntegrationError) as exc:
            audit.log_event("tool_call", tool=tool_name, user=audit.fingerprint(auth_header),
                            outcome="error", error=type(exc).__name__,
                            elapsed_ms=round((time.perf_counter() - started) * 1000, 1))
            raise
        audit.log_event("tool_call", tool=tool_name, user=audit.fingerprint(auth_header),
                        outcome="ok", result_count=_result_count(body),
                        elapsed_ms=round((time.perf_counter() - started) * 1000, 1))
        return body

    def _handle_mcp(self, req_json, auth_header):
        if not isinstance(req_json, dict) or req_json.get("jsonrpc") != "2.0":
            self._mcp_error(None, -32600, "Invalid JSON-RPC request", 400)
            return
        request_id = req_json.get("id")
        if "id" in req_json and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
            self._mcp_error(None, -32600, "Invalid request id", 400)
            return
        method = req_json.get("method")
        if method is None and "id" in req_json and ("result" in req_json or "error" in req_json):
            self._send_empty_response(202)
            return
        if not isinstance(method, str):
            self._mcp_error(request_id, -32600, "Invalid method", 400)
            return
        params = req_json.get("params", {})
        if not isinstance(params, dict):
            self._mcp_error(request_id, -32602, "Parameters must be an object", 400)
            return
        # Notifications never execute tools and never receive a JSON-RPC response.
        if "id" not in req_json:
            self._send_empty_response(202)
            return
        result = None
        if method == "initialize":
            requested = params.get("protocolVersion")
            if not isinstance(requested, str) or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict):
                self._mcp_error(request_id, -32602, "Invalid initialization parameters")
                return
            result = {
                "protocolVersion": requested if requested in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "edrms-archive-mcp", "version": "0.1.0"}
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            if params.get("cursor"):
                self._mcp_error(request_id, -32602, "Unknown pagination cursor")
                return
            result = {"tools": MCP_ARCHIVE_TOOLS}
        elif method == "tools/call":
            tool_name = params.get("name")
            if tool_name not in {tool["name"] for tool in MCP_ARCHIVE_TOOLS}:
                self._mcp_error(request_id, -32602, "Unknown tool")
                return
            arguments = params.get("arguments", {})
            validation_error = self._validate_tool_arguments(tool_name, arguments)
            if validation_error:
                self._mcp_error(request_id, -32602, validation_error)
                return
            try:
                body = self._call_archive_tool(tool_name, arguments, auth_header)
                result = {
                    "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
                    "structuredContent": body,
                    "isError": False
                }
            except (EdrmsAuthError, EdrmsIntegrationError) as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            except Exception:
                self._mcp_error(request_id, -32603, "Internal error")
                return
        else:
            self._mcp_error(request_id, -32601, "Method not found")
            return
        self._send_json_response(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

    def do_DELETE(self):
        if self.path == "/mcp" and self._mcp_transport_ok():
            self._send_empty_response(405, allow="POST, OPTIONS")
        elif self.path != "/mcp":
            self._send_empty_response(404)

    def do_POST(self):
        is_mcp = self.path == "/mcp"
        if is_mcp and not self._mcp_transport_ok():
            return
        if is_mcp and self.headers.get_content_type() != "application/json":
            self._mcp_error(None, -32600, "Content-Type must be application/json", 415)
            return
        auth_header = self.headers.get("Authorization", "")
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length < 0 or content_length > 1024 * 1024:
                raise ValueError("Invalid body size")
            raw_body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
            req_json = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError):
            if is_mcp:
                self._mcp_error(None, -32700, "Parse error", 400)
            else:
                self._send_json_response(400, {"error": "Invalid JSON body"})
            return
        if is_mcp:
            self._handle_mcp(req_json, auth_header)
            return
        compatible_tools = {
            "/mcp/archive/detail": "archive_get_detail",
            "/mcp/archive/originals": "archive_list_originals",
            "/mcp/archive/original/read": "archive_read_original"
        }
        if self.path in compatible_tools:
            if not isinstance(req_json, dict):
                self._send_json_response(400, {"error": "JSON body must be an object"})
                return
            tool_name = compatible_tools[self.path]
            validation_error = self._validate_tool_arguments(tool_name, req_json)
            if validation_error:
                self._send_json_response(400, {"error": validation_error})
                return
            try:
                body = self._call_archive_tool(tool_name, req_json, auth_header)
            except EdrmsAuthError as exc:
                self._send_json_response(401, {"error": str(exc), "data": [], "msg": str(exc)})
                return
            except EdrmsIntegrationError as exc:
                self._send_json_response(502, {"error": str(exc), "data": [], "msg": str(exc)})
                return
            self._send_json_response(200, body)
            return
        if self.path in ["/mcp/archive/rag", "/api/archive/rag"]:
            if not isinstance(req_json, dict):
                self._send_json_response(400, {"error": "JSON body must be an object"})
                return
            validation_error = self._validate_tool_arguments("archive_search", req_json)
            if validation_error:
                self._send_json_response(400, {"error": validation_error, "data": [], "msg": validation_error})
                return
            query_req = ArchiveQueryRequest.from_dict(req_json)
            try:
                resp = service.execute_rag(query_req, auth_header)
            except EdrmsAuthError as exc:
                audit.log_event("archive_search", user=audit.fingerprint(auth_header),
                                outcome="error", error="auth")
                self._send_json_response(401, {"data": [], "msg": str(exc)})
                return
            except EdrmsIntegrationError as exc:
                audit.log_event("archive_search", user=audit.fingerprint(auth_header),
                                outcome="error", error="integration")
                self._send_json_response(502, {"data": [], "msg": str(exc)})
                return
            audit.log_event("archive_search", user=audit.fingerprint(auth_header), outcome="ok",
                            query=query_req.query, result_count=len(resp.data))
            self._send_json_response(200, resp.to_dict())
            return
        self._send_json_response(404, {"error": "Path not found"})

def run():
    server_address = (config.SERVER_HOST, config.SERVER_PORT)
    httpd = HTTPServer(server_address, MCPRequestHandler)
    print(f"================================================================")
    print(f"🚀 华泰证券 EDRMS MCP 服务端 (路线 A · 零依赖原生版) 已启动")
    print(f"📡 监听地址: http://{config.SERVER_HOST}:{config.SERVER_PORT}")
    print(f"📌 REST 接口: POST http://localhost:{config.SERVER_PORT}/mcp/archive/rag")
    print(f"📌 标准 MCP 接口: POST http://localhost:{config.SERVER_PORT}/mcp")
    print(f"🩺 健康状态: GET  http://localhost:{config.SERVER_PORT}/health")
    print(f"================================================================")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
        httpd.server_close()
        sys.exit(0)

if __name__ == "__main__":
    run()

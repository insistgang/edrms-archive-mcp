import json
import urllib.error
import urllib.parse
import urllib.request


class MCPClientError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MCPClient:
    def __init__(self, url, auth_header, timeout=30.0):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('MCP 地址必须是不含凭据的 HTTP(S) URL')
        if parsed.query or parsed.fragment:
            raise ValueError('MCP 地址不能包含查询参数或片段')
        if not auth_header or not auth_header.strip():
            raise ValueError('调用 MCP 需要当前用户的 EDRMS 令牌')
        if timeout <= 0:
            raise ValueError('超时必须大于0')
        self.url = url
        self.auth_header = auth_header
        self.timeout = timeout
        self.protocol_version = None
        self.session_id = None
        self.tools = []
        self.next_id = 1
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _request(self, method, params=None, notification=False):
        body = {'jsonrpc': '2.0', 'method': method}
        request_id = None
        if not notification:
            request_id = self.next_id
            self.next_id += 1
            body['id'] = request_id
        if params is not None:
            body['params'] = params
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'Authorization': self.auth_header}
        if self.protocol_version:
            headers['MCP-Protocol-Version'] = self.protocol_version
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        request = urllib.request.Request(self.url, data=json.dumps(body).encode('utf-8'), headers=headers, method='POST')
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
                session_id = response.headers.get('Mcp-Session-Id')
                if session_id:
                    self.session_id = session_id
                if notification and response.status in (200, 202, 204) and not payload:
                    return None
                content_type = response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            with exc:
                code = exc.code
            raise MCPClientError('MCP HTTP 请求失败（状态码 %d），未返回业务数据' % code) from None
        except (OSError, ValueError):
            raise MCPClientError('MCP 服务不可达或连接失败') from None
        if len(payload) > 4 * 1024 * 1024:
            raise MCPClientError('MCP 响应超过4MiB客户端限制')
        if content_type != 'application/json':
            raise MCPClientError('当前客户端仅支持 JSON 响应，不支持 SSE 流')
        try:
            result = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise MCPClientError('MCP 响应不是有效 JSON') from None
        if not isinstance(result, dict) or result.get('jsonrpc') != '2.0' or result.get('id') != request_id:
            raise MCPClientError('MCP 响应版本或请求 ID 不匹配')
        if 'error' in result:
            error = result['error']
            code = error.get('code') if isinstance(error, dict) else None
            raise MCPClientError('MCP 协议错误（code=%s）' % code)
        if not isinstance(result.get('result'), dict):
            raise MCPClientError('MCP 响应缺少 result 对象')
        return result['result']

    def initialize(self):
        supported = ('2025-11-25', '2025-06-18', '2025-03-26')
        result = self._request('initialize', {
            'protocolVersion': supported[0], 'capabilities': {},
            'clientInfo': {'name': 'edrms-model-client', 'version': '0.2.0'}
        })
        version = result.get('protocolVersion')
        if version not in supported:
            raise MCPClientError('服务端协商了客户端不支持的协议版本')
        if not isinstance(result.get('capabilities'), dict) or 'tools' not in result['capabilities']:
            raise MCPClientError('MCP 服务端未声明工具能力')
        self.protocol_version = version
        self._request('notifications/initialized', notification=True)
        self.tools = self.list_tools()
        return result

    def list_tools(self):
        result = self._request('tools/list')
        if result.get('nextCursor'):
            raise MCPClientError('当前客户端不支持分页工具列表')
        tools = result.get('tools')
        if not isinstance(tools, list) or any(not isinstance(tool, dict) or not isinstance(tool.get('name'), str)
                                              or not isinstance(tool.get('inputSchema'), dict) for tool in tools):
            raise MCPClientError('MCP 工具定义无效')
        if len({tool['name'] for tool in tools}) != len(tools):
            raise MCPClientError('MCP 服务端返回重复工具名')
        return tools

    def call_tool(self, name, arguments):
        if not self.protocol_version:
            raise MCPClientError('必须先初始化 MCP 客户端')
        if name not in {tool['name'] for tool in self.tools}:
            raise MCPClientError('工具未在当前 MCP 服务端注册')
        if not isinstance(arguments, dict):
            raise ValueError('工具参数必须为对象')
        result = self._request('tools/call', {'name': name, 'arguments': arguments})
        if result.get('isError'):
            raise MCPClientError('MCP 工具执行失败；请检查身份、参数、权限及上游可用性')
        structured = result.get('structuredContent')
        if isinstance(structured, dict):
            return structured
        for item in result.get('content', []):
            if isinstance(item, dict) and item.get('type') == 'text':
                try:
                    value = json.loads(item.get('text', ''))
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    return value
        raise MCPClientError('工具未返回可解析的 JSON 业务结果')

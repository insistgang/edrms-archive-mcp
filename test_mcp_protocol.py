import http.client
import json
import threading
import unittest
from http.server import HTTPServer
from unittest.mock import patch

import app_standalone as app
from edrms_client import EdrmsAuthError, EdrmsIntegrationError
from models import ArchiveQueryResponse


class QuietHandler(app.MCPRequestHandler):
    def log_message(self, *args):
        pass


class MCPProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, obj=None, method='POST', headers=None, raw=None, path='/mcp'):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        body = raw if raw is not None else json.dumps(obj)
        req_headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        req_headers.update(headers or {})
        conn.request(method, path, body if method == 'POST' else None, req_headers)
        res = conn.getresponse()
        status, response_headers, data = res.status, dict(res.getheaders()), res.read()
        conn.close()
        return status, response_headers, json.loads(data) if data else None

    def rpc(self, method, params=None, id=1):
        req = {'jsonrpc': '2.0', 'id': id, 'method': method}
        if params is not None:
            req['params'] = params
        return self.request(req)

    def test_initialize(self):
        status, headers, data = self.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
        self.assertEqual(status, 200)
        self.assertEqual(data['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(data['result']['capabilities'], {'tools': {}})
        self.assertNotIn('MCP-Session-Id', headers)

    def test_version_negotiation(self):
        status, _, data = self.rpc('initialize', {'protocolVersion': '2099-01-01', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
        self.assertEqual(status, 200)
        self.assertEqual(data['result']['protocolVersion'], '2025-11-25')

    def test_initialized_notification(self):
        status, _, data = self.request({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertEqual((status, data), (202, None))

    def test_tools_list(self):
        status, _, data = self.rpc('tools/list')
        self.assertEqual(status, 200)
        self.assertEqual(
            {tool['name'] for tool in data['result']['tools']},
            {'archive_search', 'archive_get_detail', 'archive_list_originals', 'archive_read_original'}
        )

    def test_original_tool_registered_and_returns_content(self):
        args = {'archiveTypeId': 'cat', 'fileId': 'file-1', 'attachmentId': 71}
        body = {'fragments': [{'text': 'hello', 'source': {'fileId': 'file-1'}}], 'sourceType': 'MOCK'}
        with patch.object(app.service, 'read_original', return_value=body) as read:
            _, _, data = self.request({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'archive_read_original','arguments':args}}, headers={'Authorization':'Bearer token'})
        self.assertFalse(data['result']['isError'])
        self.assertEqual(json.loads(data['result']['content'][0]['text']), body)
        self.assertEqual(data['result']['structuredContent'], body)
        read.assert_called_once_with('cat', 'file-1', 71, 'Bearer token', None, 20000, 10)

    def test_original_tool_rejects_bad_ids_and_returns_auth_errors(self):
        for args in (
            {'archiveTypeId':'cat','fileId':'f','attachmentId':True},
            {'archiveTypeId':'cat','fileId':'','attachmentId':1}
        ):
            _, _, data = self.rpc('tools/call', {'name':'archive_read_original','arguments':args})
            self.assertEqual(data['error']['code'], -32602)
        args = {'archiveTypeId':'cat','fileId':'f','attachmentId':1}
        with patch.object(app.service, 'read_original', side_effect=EdrmsAuthError('需要令牌')):
            _, _, data = self.rpc('tools/call', {'name':'archive_read_original','arguments':args})
        self.assertTrue(data['result']['isError'])

    def test_detail_and_original_list_tools_return_structured_content(self):
        cases = [
            ('archive_get_detail', 'get_detail', {'archiveTypeId':'cat','entityType':'file','entityId':'f'}, {'entityType':'file'}),
            ('archive_list_originals', 'list_originals', {'archiveTypeId':'cat','fileId':'f'}, {'fileId':'f','items':[]})
        ]
        for tool, method, args, body in cases:
            with self.subTest(tool=tool), patch.object(app.service, method, return_value=body):
                _, _, data = self.rpc('tools/call', {'name':tool,'arguments':args})
            self.assertFalse(data['result']['isError'])
            self.assertEqual(data['result']['structuredContent'], body)

    def test_mock_four_tool_mcp_closed_loop_without_service_patches(self):
        auth = {'Authorization':'Bearer token_normal_user'}
        with patch.object(app.config, 'RUN_MODE', 'MOCK'), patch.object(app.service.client, 'base_url', ''):
            _, _, search = self.request({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
                'name':'archive_search','arguments':{'query':'风险防控'}
            }}, headers=auth)
            item = search['result']['structuredContent']['data'][0]
            common = {'archiveTypeId':item['archiveTypeId'],'fileId':item['fileId']}
            _, _, detail = self.request({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
                'name':'archive_get_detail','arguments':{
                    'archiveTypeId':item['archiveTypeId'],'entityType':'file','entityId':item['fileId']
                }
            }}, headers=auth)
            _, _, originals = self.request({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{
                'name':'archive_list_originals','arguments':common
            }}, headers=auth)
            attachment_id = originals['result']['structuredContent']['items'][0]['attachmentId']
            _, _, read = self.request({'jsonrpc':'2.0','id':4,'method':'tools/call','params':{
                'name':'archive_read_original','arguments':dict(common, attachmentId=attachment_id)
            }}, headers=auth)
        self.assertEqual(detail['result']['structuredContent']['entity']['fileId'], item['fileId'])
        self.assertEqual(read['result']['structuredContent']['sourceType'], 'MOCK')
        self.assertEqual(
            read['result']['structuredContent']['fragments'][0]['source']['attachmentId'],
            attachment_id
        )

    def test_tools_call_returns_json_as_standard_text(self):
        payload = ArchiveQueryResponse(data=[], msg='真实查询完成')
        with patch.object(app.service, 'execute_rag', return_value=payload) as execute:
            status, _, data = self.rpc('tools/call', {'name': 'archive_search', 'arguments': {'query': 'test'}})
        self.assertEqual(status, 200)
        content = data['result']['content']
        self.assertTrue(all(x['type'] == 'text' for x in content))
        self.assertEqual(json.loads(content[-1]['text']), payload.to_dict())
        self.assertEqual(data['result']['structuredContent'], payload.to_dict())
        execute.assert_called_once()

    def test_auth_and_upstream_errors_do_not_return_data(self):
        for exc in (EdrmsAuthError('需要令牌'), EdrmsIntegrationError('上游异常')):
            with self.subTest(exc=exc), patch.object(app.service, 'execute_rag', side_effect=exc):
                status, _, data = self.rpc('tools/call', {'name': 'archive_search', 'arguments': {'query': 'test'}})
            self.assertEqual(status, 200)
            self.assertTrue(data['result']['isError'])
            self.assertNotIn('structuredContent', data['result'])

    def test_unknown_method_and_invalid_arguments(self):
        _, _, data = self.rpc('no/such/method')
        self.assertEqual(data['error']['code'], -32601)
        for arguments in ([], {'query': 9}, {'query': 'test', 'pageSize': -1}):
            with self.subTest(arguments=arguments):
                _, _, data = self.rpc('tools/call', {'name': 'archive_search', 'arguments': arguments})
                self.assertEqual(data['error']['code'], -32602)

    def test_parse_error_and_invalid_version(self):
        _, _, data = self.request(raw='{')
        self.assertEqual(data['error']['code'], -32700)
        status, _, _ = self.request({'jsonrpc':'2.0','id':1,'method':'ping'}, headers={'MCP-Protocol-Version':'invalid'})
        self.assertEqual(status, 400)

    def test_no_sse_and_origin_rejection(self):
        status, _, _ = self.request(method='GET')
        self.assertEqual(status, 405)
        status, _, _ = self.request({'jsonrpc':'2.0','id':1,'method':'ping'}, headers={'Origin':'https://untrusted.example'})
        self.assertEqual(status, 403)

    def test_rest_endpoint_keeps_auth_and_failure_statuses(self):
        for exc, expected in ((EdrmsAuthError('需要令牌'), 401), (EdrmsIntegrationError('上游异常'), 502)):
            with self.subTest(expected=expected), patch.object(app.service, 'execute_rag', side_effect=exc):
                status, _, data = self.request({'query': 'test'}, path='/mcp/archive/rag')
            self.assertEqual(status, expected)
            self.assertEqual(data['data'], [])

    def test_http_detail_original_list_and_read_endpoints(self):
        cases = [
            ('/mcp/archive/detail', 'get_detail', {'archiveTypeId':'cat','entityType':'file','entityId':'f'}, {'entityType':'file'}),
            ('/mcp/archive/originals', 'list_originals', {'archiveTypeId':'cat','fileId':'f'}, {'fileId':'f','items':[]}),
            ('/mcp/archive/original/read', 'read_original', {'archiveTypeId':'cat','fileId':'f','attachmentId':1}, {'fragments':[]})
        ]
        for path, method, args, body in cases:
            with self.subTest(path=path), patch.object(app.service, method, return_value=body) as call:
                status, _, data = self.request(args, path=path, headers={'Authorization':'Bearer token'})
            self.assertEqual(status, 200)
            self.assertEqual(data, body)
            call.assert_called_once()

    def test_http_new_endpoints_reject_invalid_arguments(self):
        status, _, data = self.request({'fileId':'f'}, path='/mcp/archive/originals')
        self.assertEqual(status, 400)
        self.assertIn('error', data)

    def test_cors_rejects_wildcard_and_requires_exact_origin(self):
        with patch.dict(app.os.environ, {'MCP_ALLOW_CORS_ORIGIN': '*'}):
            _, headers, _ = self.request({'fileId':'f'}, path='/mcp/archive/originals',
                                          headers={'Origin':'https://untrusted.example'})
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        with patch.dict(app.os.environ, {'MCP_ALLOW_CORS_ORIGIN': 'https://approved.example'}):
            _, allowed, _ = self.request({'fileId':'f'}, path='/mcp/archive/originals',
                                          headers={'Origin':'https://approved.example'})
            _, denied, _ = self.request({'fileId':'f'}, path='/mcp/archive/originals',
                                         headers={'Origin':'https://untrusted.example'})
        self.assertEqual(allowed.get('Access-Control-Allow-Origin'), 'https://approved.example')
        self.assertNotIn('Access-Control-Allow-Origin', denied)


if __name__ == '__main__':
    unittest.main()

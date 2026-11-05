import contextlib
import io
import json
import os
import sys
import threading
import unittest
from http.server import HTTPServer
from unittest.mock import patch

import app_standalone as app
import ask_archive
import config
from mcp_client import MCPClient, MCPClientError
from model_agent import ModelAgent
from test_model_agent import FakeModelHandler


class MCPClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Quiet(app.MCPRequestHandler):
            def log_message(self, *args):
                pass

        cls.server = HTTPServer(('127.0.0.1', 0), Quiet)
        cls.model_server = HTTPServer(('127.0.0.1', 0), FakeModelHandler)
        cls.threads = []
        for server in (cls.server, cls.model_server):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls.threads.append(thread)
        cls.url = 'http://127.0.0.1:%d/mcp' % cls.server.server_address[1]
        cls.model_url = 'http://127.0.0.1:%d/v1' % cls.model_server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.model_server):
            server.shutdown()
            server.server_close()
        for thread in cls.threads:
            thread.join()

    def setUp(self):
        self.mode = patch.object(config, 'RUN_MODE', 'MOCK')
        self.mode.start()
        self.llm = patch.multiple(config, LLM_API_BASE='', LLM_API_KEY='')
        self.llm.start()
        FakeModelHandler.requests = []
        FakeModelHandler.script = []

    def tearDown(self):
        self.llm.stop()
        self.mode.stop()

    def client(self):
        return MCPClient(self.url, 'Bearer token_normal_user')

    def script(self):
        category = '文书档案（立卷）'
        calls = [
            ('archive_search', {'query': '风险防控'}),
            ('archive_get_detail', {'archiveTypeId': category, 'entityType': 'file', 'entityId': 'F2026-WS-001'}),
            ('archive_list_originals', {'archiveTypeId': category, 'fileId': 'F2026-WS-001'}),
            ('archive_read_original', {'archiveTypeId': category, 'fileId': 'F2026-WS-001', 'attachmentId': 10101})
        ]
        FakeModelHandler.script = [
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call%d' % number,
                'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
            for number, (name, arguments) in enumerate(calls)
        ] + [{'role': 'assistant', 'content': '依据原文，需落实应急值守制度。[文件F2026-WS-001，原文10101]'}]

    def test_model_to_mcp_to_mock_archive_over_real_http(self):
        self.script()
        client = self.client()
        agent = ModelAgent(None, None, api_base=self.model_url, api_key='model-key',
                           model='local-simulator', mcp_client=client)
        result = agent.ask('风险防控通知要求什么？')
        self.assertEqual(result['rounds'], 5)
        self.assertEqual(len(result['trace']), 4)
        self.assertIn('应急值守', result['answer'])
        self.assertTrue(any(source.get('attachmentId') == 10101 for source in result['citations']))
        transmitted = json.dumps(FakeModelHandler.requests, ensure_ascii=False)
        self.assertNotIn('token_normal_user', transmitted)
        self.assertIn('应急值守制度', transmitted)
        self.assertEqual(len(client.tools), 4)

    def test_call_requires_initialization(self):
        with self.assertRaises(MCPClientError):
            self.client().call_tool('archive_search', {'query': 'x'})

    def test_unknown_tool_is_rejected(self):
        client = self.client()
        client.initialize()
        with self.assertRaises(MCPClientError):
            client.call_tool('archive_delete', {})

    def test_tool_error_does_not_return_business_data(self):
        client = self.client()
        client.initialize()
        with self.assertRaises(MCPClientError):
            client.call_tool('archive_get_detail', {
                'archiveTypeId': '文书档案（立卷）', 'entityType': 'file', 'entityId': 'F2026-WS-002'})

    def test_credentials_in_url_and_missing_identity_are_rejected(self):
        for url, auth in [('http://user:pass@localhost/mcp', 'Bearer t'),
                          ('file:///tmp/a', 'Bearer t'), (self.url, '')]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                MCPClient(url, auth)

    def test_search_does_not_reenter_model_without_explicit_opt_in(self):
        from archive_service import ArchiveRagService
        from models import ArchiveItem
        service = ArchiveRagService()
        item = ArchiveItem('目录', 2026, [], 'f', None, '一文一件')
        with patch.multiple(config, LLM_API_BASE=self.model_url, LLM_API_KEY='model-key', LLM_GENERATE_MSG=False), \
             patch.object(service, '_call_external_llm') as model:
            service._generate_answer('目录', [item])
        model.assert_not_called()

    def test_cli_uses_real_mcp_transport(self):
        self.script()
        output = io.StringIO()
        with patch.object(sys, 'argv', ['ask_archive.py', '请读取风险通知', '--mcp-url', self.url, '--json']), \
             patch.dict(os.environ, {'EDRMS_TOKEN': 'token_normal_user'}), \
             patch.multiple(config, LLM_API_BASE=self.model_url, LLM_API_KEY='model-key', LLM_MODEL='simulator'), \
             contextlib.redirect_stdout(output):
            code = ask_archive.main()
        self.assertEqual(code, 0)
        self.assertIn('应急值守', json.loads(output.getvalue())['answer'])
        self.assertNotIn('token_normal_user', output.getvalue())


if __name__ == '__main__':
    unittest.main()

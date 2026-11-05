import unittest
from unittest.mock import patch

import app_standalone as app
import config
from edrms_client import EdrmsClient, EdrmsIntegrationError
from models import ArchiveQueryRequest
from search_filters import matches_filters, validate_filters


class SearchFilterTests(unittest.TestCase):
    def test_invalid_values_and_unknown_fields_are_rejected(self):
        for value in ([], {'sql': 'anything'}, {'yearFrom': True}, {'yearFrom': 2027, 'yearTo': 2026},
                      {'dateFrom': '2026-01-01'}, {'dateField': 'formedDate'},
                      {'dateField': 'formedDate', 'dateFrom': '2026-02-30'},
                      {'dateField': 'formedDate', 'dateFrom': '20260101'},
                      {'dateField': 'formedDate', 'dateFrom': '2026-02-01', 'dateTo': '2026-01-01'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_filters(value)

    def test_inclusive_boundaries_and_missing_dates(self):
        filters = validate_filters({'yearFrom': 2026, 'yearTo': 2026, 'dateField': 'formedDate',
                                    'dateFrom': '2026-01-01', 'dateTo': '2026-03-31'})
        self.assertTrue(matches_filters({'year': 2026, 'meta': {'formedDate': '2026-01-01'}}, filters))
        self.assertTrue(matches_filters({'year': 2026, 'meta': {'formedDate': '2026-03-31'}}, filters))
        self.assertFalse(matches_filters({'year': 2026, 'meta': {'formedDate': '2026-04-01'}}, filters))
        self.assertFalse(matches_filters({'year': 2026, 'meta': {}}, filters))

    def test_mock_filter_runs_before_pagination(self):
        client = EdrmsClient(base_url='')
        client._mock_data = [
            {'fileId': 'old', 'caseId': None, 'name': '通知', 'year': 2025, 'attachmentsId': [],
             'category': 'cat', 'archiveType': '一文一件', 'meta': {'formedDate': '2025-01-01'}},
            {'fileId': 'new', 'caseId': None, 'name': '通知', 'year': 2026, 'attachmentsId': [],
             'category': 'cat', 'archiveType': '一文一件', 'meta': {'formedDate': '2026-03-31'}}]
        with patch.object(config, 'RUN_MODE', 'MOCK'):
            result, _ = client.search_archives('通知', 'Bearer token_normal_user', page_size=1,
                                               filters={'yearFrom': 2026})
        self.assertEqual([item.fileId for item in result], ['new'])

    def test_live_does_not_silently_ignore_filters(self):
        client = EdrmsClient(base_url='https://example.invalid')
        with patch.object(config, 'RUN_MODE', 'LIVE'), patch.object(client, '_call_live_edrms_search') as upstream:
            with self.assertRaises(EdrmsIntegrationError):
                client.search_archives('通知', 'Bearer t', filters={'yearFrom': 2026})
        upstream.assert_not_called()

    def test_request_and_tool_validator_preserve_filter_contract(self):
        args = {'query': '通知', 'filters': {'yearFrom': 2026}}
        self.assertEqual(ArchiveQueryRequest.from_dict(args).filters, args['filters'])
        self.assertIsNone(app.MCPRequestHandler._validate_tool_arguments('archive_search', args))
        self.assertIsNotNone(app.MCPRequestHandler._validate_tool_arguments('archive_search',
                              {'query': '通知', 'filters': {'yearFrom': False}}))


class SearchFilterHTTPTests(unittest.TestCase):
    def test_mcp_filter_survives_http_transport(self):
        import threading
        from http.server import HTTPServer
        from mcp_client import MCPClient
        class Quiet(app.MCPRequestHandler):
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(config, 'RUN_MODE', 'MOCK'):
                client = MCPClient('http://127.0.0.1:%d/mcp' % server.server_address[1], 'Bearer token_normal_user')
                client.initialize()
                result = client.call_tool('archive_search', {'query': '风险防控', 'filters': {'yearFrom': 2026}})
                self.assertTrue(result['data'])
                self.assertTrue(all(item['year'] >= 2026 for item in result['data']))
                empty = client.call_tool('archive_search', {'query': '风险防控', 'filters': {'yearFrom': 2030}})
                self.assertEqual(empty['data'], [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()

import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

import acceptance_test as acceptance


class AcceptanceIntegrityTests(unittest.TestCase):
    def test_http_502_cannot_pass_business_endpoint_checks(self):
        def response(url, payload, headers=None):
            if url.endswith('/mcp/archive/rag'):
                if not headers:
                    return 401, {'data': []}
                return 200, {'data': [{'archiveTypeId': 'cat', 'fileId': 'f', 'attachmentsId': [1]}], 'msg': 'ok'}
            return 502, {'error': 'upstream failed', 'data': []}

        results = acceptance.Results()
        with patch.object(acceptance, 'http_json', side_effect=response):
            acceptance.local_contract_checks(results, 'http://example.invalid')
        endpoints = [item for item in results.items if item[0] in (
            'HTTP 详情端点可达', 'HTTP 原文列表端点可达', 'HTTP 原文读取端点可达')]
        self.assertEqual(len(endpoints), 3)
        self.assertTrue(all(status == acceptance.FAIL for _, status, _ in endpoints))

    def test_live_requested_without_token_is_not_success(self):
        class Server:
            def shutdown(self):
                pass

            def server_close(self):
                pass

        with patch.object(sys, 'argv', ['acceptance_test.py', '--live']), \
             patch.dict(os.environ, {'EDRMS_TOKEN': ''}), \
             patch.object(acceptance, 'start_server', return_value=(Server(), 'http://example.invalid')), \
             patch.object(acceptance, 'local_contract_checks'), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(acceptance.main(), 2)


if __name__ == '__main__':
    unittest.main()

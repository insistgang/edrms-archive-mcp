import io
import json
import unittest
import zipfile
from unittest.mock import patch

import config
from archive_service import ArchiveRagService
from edrms_client import EdrmsAuthError, EdrmsClient
from model_agent import MAX_TOOL_OUTPUT_CHARS, ModelAgent
from models import ArchiveQueryRequest
from original_parser import UnsupportedFormat, extract_archive_text, parse_docx, parse_xlsx
from test_original_parser import DOCX_XML, XLSX_RELS, XLSX_SHEET, XLSX_SHARED, XLSX_WORKBOOK, build_zip


class ParserReviewTests(unittest.TestCase):
    def test_direct_docx_rejects_traversal_members(self):
        payload = build_zip({'word/document.xml': DOCX_XML, '../unsafe.txt': 'unsafe'})
        with self.assertRaises(UnsupportedFormat):
            parse_docx(payload)

    def test_direct_docx_rejects_compression_bombs(self):
        payload = build_zip({'word/document.xml': DOCX_XML, 'word/bomb.bin': b'0' * 200000})
        with self.assertRaises(UnsupportedFormat):
            parse_docx(payload)

    def test_office_xml_entities_are_rejected(self):
        document = '<!DOCTYPE doc [<!ENTITY x "value">]>' + DOCX_XML
        with self.assertRaises(UnsupportedFormat):
            parse_docx(build_zip({'word/document.xml': document}))

    def test_invalid_office_xml_is_a_controlled_error(self):
        with self.assertRaises(UnsupportedFormat):
            parse_docx(build_zip({'word/document.xml': '<broken'}))

    def test_absolute_workbook_target_is_resolved(self):
        payload = build_zip({
            'xl/workbook.xml': XLSX_WORKBOOK,
            'xl/_rels/workbook.xml.rels': XLSX_RELS.replace('worksheets/sheet1.xml', '/xl/worksheets/sheet1.xml'),
            'xl/sharedStrings.xml': XLSX_SHARED,
            'xl/worksheets/sheet1.xml': XLSX_SHEET
        })
        self.assertEqual(len(parse_xlsx(payload)), 2)

    def test_external_workbook_target_is_rejected(self):
        relationships = XLSX_RELS.replace('Target="worksheets/sheet1.xml"',
                                         'Target="https://example.invalid/sheet" TargetMode="External"')
        payload = build_zip({'xl/workbook.xml': XLSX_WORKBOOK, 'xl/_rels/workbook.xml.rels': relationships})
        with self.assertRaises(UnsupportedFormat):
            parse_xlsx(payload)

    def test_zip_text_keeps_blank_lines_and_source_count(self):
        content = '\n第一行\n\n第三行\n'
        result = extract_archive_text(build_zip({'note.txt': content}), 'a.zip')
        self.assertEqual(result['fragments'][0]['text'], content)
        self.assertEqual(result['fragments'][0]['source']['lineCount'], 4)

    def test_parser_marks_its_own_truncation(self):
        result = extract_archive_text(build_zip({'note.txt': 'x' * 200}), 'a.zip', 25)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['fragments'][0]['text']), 25)

    def test_container_without_supported_text_is_not_empty_success(self):
        payload = build_zip({'image.png': b'\x89PNG\r\n\x1a\nfake'})
        with self.assertRaises(UnsupportedFormat):
            extract_archive_text(payload, 'a.zip')

    def test_live_tool_preserves_parser_truncation_and_table_source(self):
        client = EdrmsClient(base_url='http://example.invalid')
        parsed = {'format': 'docx', 'parser': 'docx_ooxml', 'truncated': True, 'warnings': [],
                  'fragments': [{'text': 'row', 'source': {'table': 2, 'row': 3, 'cells': 4}}]}
        with patch.object(config, 'RUN_MODE', 'LIVE'), \
             patch.object(client, '_download_original_bytes', return_value={
                 'payload': b'placeholder', 'fileName': 'a.docx', 'integrity': 'size_verified_only'}), \
             patch('original_parser.extract_archive_text', return_value=parsed):
            result = client.read_original('cat', 'file', 1, 'Bearer test', 'query')
        self.assertTrue(result['truncated'])
        self.assertEqual(result['fragments'][0]['source']['table'], 2)
        self.assertEqual(result['fragments'][0]['source']['cells'], 4)


class ModelReviewTests(unittest.TestCase):
    def test_context_overflow_is_valid_json_and_has_no_unseen_citations(self):
        class Service:
            def read_original(self, *args):
                return {'fragments': [{'text': 'x' * (MAX_TOOL_OUTPUT_CHARS + 1),
                                       'source': {'fileId': 'file', 'attachmentId': 1}}]}

        agent = ModelAgent(Service(), 'Bearer test')
        script = [
            {'content': '', 'tool_calls': [{'id': 'call1', 'function': {
                'name': 'archive_read_original',
                'arguments': json.dumps({'archiveTypeId': 'cat', 'fileId': 'file', 'attachmentId': 1})}}]},
            {'content': '请缩小读取范围'}
        ]
        sent_messages = []

        def model(messages):
            sent_messages.append(list(messages))
            return script[len(sent_messages) - 1]

        with patch.object(agent, '_call_model', side_effect=model):
            result = agent.ask('read')
        tool_message = next(message for message in sent_messages[-1] if message['role'] == 'tool')
        self.assertTrue(json.loads(tool_message['content'])['truncated'])
        self.assertTrue(result['trace'][0]['outputTruncated'])
        self.assertEqual(result['citations'], [])

    def test_empty_query_cannot_bypass_identity(self):
        service = ArchiveRagService()
        with self.assertRaises(EdrmsAuthError):
            service.execute_rag(ArchiveQueryRequest(query=''), None)


class BatchIntegrityTests(unittest.TestCase):
    def test_detail_outage_is_not_empty_search_success(self):
        from test_live_contract import Response
        client = EdrmsClient(base_url='http://example.invalid')
        search = {'code': 0, 'rawList': [{'rawData': {'id': 'f', 'archive_type_id': 'c'}}]}
        for detail in ({'success': False}, {'success': True, 'data': []},
                       {'success': True, 'data': [{'id': 'other'}]}):
            with self.subTest(detail=detail), patch('urllib.request.urlopen', side_effect=[
                Response(search), Response(detail)]):
                items, error = client._call_live_edrms_search('q', 'Bearer test', None, 1, 10)
            self.assertIsNone(items)
            self.assertTrue(error)


if __name__ == '__main__':
    unittest.main()

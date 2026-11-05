import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request

from edrms_client import _NoRedirect
from original_parser import UnsupportedFormat, extract_archive_text, read_zip_members


SECRET_KEYS = {'token', 'authorization', 'password', 'passwd', 'accesstoken', 'refreshtoken', 'cookie', 'setcookie'}


def scrub(value):
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()
                if re.sub(r'[^a-z]', '', key.lower()) not in SECRET_KEYS}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scrub(value), ensure_ascii=False, indent=2), encoding='utf-8')


def safe_name(value):
    value = re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', str(value)).strip(' .')
    return value[:150] or 'unnamed'


class Exporter:
    def __init__(self, base_url, token, output):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.output = Path(output)
        self.errors = []
        self.opener = urllib.request.build_opener(_NoRedirect)

    def post(self, path, body):
        request = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode('utf-8'),
                                         headers={'Content-Type': 'application/json', 'token': self.token}, method='POST')
        with self.opener.open(request, timeout=30) as response:
            payload = response.read(20 * 1024 * 1024 + 1)
        if len(payload) > 20 * 1024 * 1024:
            raise ValueError('接口响应超过20MiB上限')
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise ValueError('接口未返回JSON对象')
        return result

    def search_all(self, metadata_type):
        records = []
        seen = set()
        total = None
        page = 1
        while True:
            result = self.post('/edrms/api/search/searchData', {
                'keyWord': '', 'archiveTypeId': '', 'pageIndex': page, 'pageSize': 100,
                'permType': 1, 'metaDataType': metadata_type, 'isFuzzySearch': True
            })
            if result.get('code') != 0 or not isinstance(result.get('rawList'), list):
                raise ValueError('目录检索失败或返回结构未知')
            current_total = result.get('total')
            if isinstance(current_total, bool) or not isinstance(current_total, int) or current_total < 0:
                raise ValueError('目录总数无效')
            if total is not None and current_total != total:
                raise ValueError('分页期间目录总数变化，请重试导出')
            total = current_total
            added = 0
            for wrapper in result['rawList']:
                raw = wrapper.get('rawData')
                row = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(row, dict) or not row.get('id') or not row.get('archive_type_id'):
                    raise ValueError('目录记录缺少标识')
                key = (str(row['archive_type_id']), str(row['id']))
                if key in seen:
                    raise ValueError('分页出现重复记录，无法保证完整性')
                seen.add(key)
                records.append(row)
                added += 1
            if len(records) == total:
                return records, total
            if len(records) > total or not added or page >= max(1, math.ceil(total / 100)) + 1:
                raise ValueError('分页不完整或超出声明总数')
            page += 1

    def originals_all(self, category_id, file_id):
        records = []
        seen = set()
        total = None
        page = 1
        while True:
            result = self.post('/edrms/api/recordCollate/loadOriginalByFileIds', {
                'nodeId': category_id, 'fileIds': [file_id], 'pageIndex': page, 'pageSize': 100
            })
            count = result.get('totalCount')
            data = result.get('data')
            if data is None and count == 0:
                data = []
            if result.get('success') is not True or not isinstance(data, list) or isinstance(count, bool) or not isinstance(count, int):
                raise ValueError('原文列表失败或总数无效')
            if total is not None and count != total:
                raise ValueError('分页期间原文数量变化')
            total = count
            for record in data:
                if not isinstance(record, dict) or str(record.get('arch_id')) != file_id or not record.get('fileid'):
                    raise ValueError('原文与文件关联不匹配')
                key = (str(record['fileid']), str(record.get('fileverid')))
                if key in seen:
                    raise ValueError('原文分页存在重复记录')
                seen.add(key)
                records.append(record)
            if len(records) == total:
                return records
            if len(records) > total or not data:
                raise ValueError('原文列表分页不完整')
            page += 1

    def download(self, record):
        version = record.get('fileverid')
        size = record.get('source_elec_size')
        name = record.get('source_file_name')
        if isinstance(version, bool) or not isinstance(version, int) or not isinstance(name, str):
            raise ValueError('原文缺少有效版本或名称')
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= 50 * 1024 * 1024:
            raise ValueError('原文大小无效或超过50MiB导出上限')
        query = urllib.parse.urlencode({'fileId': version, 'fileName': name})
        request = urllib.request.Request(self.base_url + '/edrms/api/ecmconversion/ecmdownload?' + query,
                                         headers={'token': self.token})
        for attempt in range(3):
            with self.opener.open(request, timeout=30) as response:
                payload = response.read(50 * 1024 * 1024 + 1)
            try:
                problem = json.loads(payload) if payload.startswith(b'{') else None
            except (ValueError, UnicodeDecodeError):
                problem = None
            if isinstance(problem, dict) and problem.get('success') is False and problem.get('code') == 'System:InternalError' and attempt < 2:
                continue
            break
        if len(payload) != size:
            raise ValueError('下载内容长度不匹配，不保存为成功附件')
        expected_md5 = record.get('source_elec_file_md5')
        if expected_md5 and hashlib.md5(payload).hexdigest().lower() != str(expected_md5).lower():
            raise ValueError('上游MD5校验失败')
        return payload

    def export(self):
        os.umask(0o077)
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        files, file_total = self.search_all(1)
        cases, case_total = self.search_all(0)
        save_json(self.output / '目录/文件目录_原始字段.json', files)
        save_json(self.output / '目录/案卷目录_原始字段.json', cases)
        print('分页枚举完成：文件%d，案卷%d' % (file_total, case_total), flush=True)
        entities = []
        attachments = []
        all_fragments = []
        table = []
        for kind, rows in (('case', cases), ('file', files)):
            for row in rows:
                entity_id, category_id = str(row['id']), str(row['archive_type_id'])
                entry = {'entityType': kind, 'entityId': entity_id, 'archiveTypeId': category_id,
                         'name': row.get('title'), 'searchFields': row}
                try:
                    endpoint = 'getRecordDetailByIds' if kind == 'case' else 'getFileDetailByIds'
                    detail = self.post('/edrms/api/recordCollate/' + endpoint, {
                        'archiveTypeId': category_id, 'ids': [entity_id], 'dataType': 0 if kind == 'case' else 1
                    })
                    data = detail.get('data')
                    if detail.get('success') is not True or not isinstance(data, list) or len(data) != 1:
                        raise ValueError('详情失败或记录不唯一')
                    entry['detail'] = data[0]
                    record = data[0].get('recordData') if kind == 'case' else data[0]
                    if not isinstance(record, dict) or str(record.get('id')) != entity_id:
                        raise ValueError('详情身份不匹配')
                    save_json(self.output / '详情' / (kind + '_' + safe_name(entity_id) + '.json'), entry)
                    table.append({'层级': kind, 'ID': entity_id, '门类ID': category_id,
                                  '门类': row.get('archive_type_name'), '题名': record.get('title'),
                                  '年度': record.get('year'), '档号': record.get('reference_code'),
                                  '父案卷ID': record.get('dossier_id')})
                    if kind == 'file':
                        originals = self.originals_all(category_id, entity_id)
                        entry['originalRecords'] = originals
                        for original in originals:
                            attachment_id = original['fileid']
                            name = original['source_file_name']
                            directory = self.output / '附件' / safe_name(entity_id) / ('%s_%s' % (attachment_id, original.get('fileverid')))
                            directory.mkdir(parents=True, exist_ok=False)
                            attachment = {'fileId': entity_id, 'archiveTypeId': category_id,
                                          'attachmentId': attachment_id, 'name': name, 'record': original}
                            try:
                                payload = self.download(original)
                                target = directory / safe_name(name)
                                target.write_bytes(payload)
                                attachment['originalPath'] = str(target.relative_to(self.output))
                                attachment['sizeBytes'] = len(payload)
                                attachment['sha256'] = hashlib.sha256(payload).hexdigest()
                                attachment['integrity'] = 'md5_verified' if original.get('source_elec_file_md5') else 'size_verified_only'
                                if payload.startswith(b'PK') and name.lower().endswith('.zip'):
                                    for member_path, data in read_zip_members(payload):
                                        target_member = directory / '解压成员' / member_path
                                        target_member.parent.mkdir(parents=True, exist_ok=True)
                                        target_member.write_bytes(data)
                                try:
                                    parsed = extract_archive_text(payload, name, 200000)
                                    save_json(directory / '提取片段.json', parsed)
                                    text = '\n\n'.join(fragment['text'] for fragment in parsed['fragments'])
                                    (directory / '提取文本.txt').write_text(text, encoding='utf-8')
                                    attachment['parser'] = parsed['parser']
                                    attachment['truncated'] = parsed.get('truncated', False)
                                    attachment['characterCount'] = sum(len(f['text']) for f in parsed['fragments'])
                                    attachment['warnings'] = parsed['warnings']
                                    if attachment['truncated']:
                                        self.errors.append({'fileId': entity_id, 'attachmentId': attachment_id, 'step': 'parse',
                                                            'error': '提取文本超过20万字符，已保存原件但文本不完整'})
                                    for fragment in parsed['fragments']:
                                        all_fragments.append({'text': fragment['text'], 'source': {
                                            'archiveTypeId': category_id, 'fileId': entity_id, 'caseId': record.get('dossier_id'),
                                            'attachmentId': attachment_id, 'originalName': name, **fragment['source']}})
                                except UnsupportedFormat as exc:
                                    attachment['parseError'] = str(exc)
                                    self.errors.append({'fileId': entity_id, 'attachmentId': attachment_id, 'step': 'parse', 'error': str(exc)})
                            except Exception as exc:
                                attachment['error'] = type(exc).__name__
                                self.errors.append({'fileId': entity_id, 'attachmentId': attachment_id, 'step': 'download',
                                                    'error': type(exc).__name__})
                            attachments.append(attachment)
                        save_json(self.output / '详情' / ('file_' + safe_name(entity_id) + '.json'), entry)
                    print('%s %s：完成' % (kind, row.get('title')), flush=True)
                except Exception as exc:
                    entry['error'] = type(exc).__name__
                    self.errors.append({'entityId': entity_id, 'step': 'detail_or_list', 'error': type(exc).__name__})
                entities.append(entry)
        save_json(self.output / '全部目录与详情.json', entities)
        save_json(self.output / '原文记录与文件清单.json', attachments)
        save_json(self.output / '全部原文片段.json', all_fragments)
        save_json(self.output / '提取异常.json', self.errors)
        with (self.output / '目录一览.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['层级', 'ID', '门类ID', '门类', '题名', '年度', '档号', '父案卷ID'])
            writer.writeheader()
            writer.writerows(table)
        combined = []
        for fragment in all_fragments:
            source = fragment['source']
            combined.append('【文件%s / %s / %s】\n%s' % (
                source['fileId'], source['originalName'], source.get('path', ''), fragment['text']))
        (self.output / '全部原文文本.txt').write_text('\n\n'.join(combined), encoding='utf-8')
        summary = {'exportedAt': time.strftime('%Y-%m-%d %H:%M:%S'), 'source': self.base_url,
                   'scope': '当前账号通过检索接口可见的案卷和文件；不等于网站全部业务数据库',
                   'files': file_total, 'cases': case_total, 'detailsExported': len(table),
                   'originalRecords': len(attachments), 'downloadedOriginals': sum('originalPath' in a for a in attachments),
                   'fragments': len(all_fragments), 'characters': sum(len(f['text']) for f in all_fragments),
                   'errors': len(self.errors), 'output': str(self.output)}
        save_json(self.output / '导出清单.json', summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return summary


def main():
    parser = argparse.ArgumentParser(description='只读导出当前账号可见的目录与原文，不覆盖已有导出')
    parser.add_argument('--output', required=True)
    parser.add_argument('--base-url', default=os.getenv('EDRMS_BASE_URL', ''))
    args = parser.parse_args()
    token = os.getenv('EDRMS_TOKEN', '').strip()
    if not token:
        raise SystemExit('未注入 EDRMS_TOKEN，停止导出')
    summary = Exporter(args.base_url, token, args.output).export()
    return 1 if summary['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())

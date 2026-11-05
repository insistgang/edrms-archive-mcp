import json
import os
import urllib.request

from mcp_client import MCPClient


def main():
    base = os.getenv('MCP_SMOKE_BASE', 'http://127.0.0.1:18000')
    with urllib.request.urlopen(base + '/health', timeout=10) as response:
        health = json.load(response)
    assert health['mode'] == 'MOCK', '冒烟测试只允许Mock环境'
    client = MCPClient(base + '/mcp', 'Bearer token_normal_user')
    client.initialize()
    search = client.call_tool('archive_search', {
        'query': '风险防控', 'filters': {'yearFrom': 2026, 'yearTo': 2026}})
    assert search['data'], 'Mock样本未返回'
    item = search['data'][0]
    assert item['year'] == 2026
    detail = client.call_tool('archive_get_detail', {
        'archiveTypeId': item['archiveTypeId'], 'entityType': 'file', 'entityId': item['fileId']})
    assert detail['entity']['fileId'] == item['fileId']
    common = {'archiveTypeId': item['archiveTypeId'], 'fileId': item['fileId']}
    originals = client.call_tool('archive_list_originals', common)
    assert originals['items']
    attachment_id = originals['items'][0]['attachmentId']
    read = client.call_tool('archive_read_original', dict(common, attachmentId=attachment_id))
    assert read['sourceType'] == 'MOCK' and read['fragments']
    assert read['fragments'][0]['source']['attachmentId'] == attachment_id
    print('容器健康检查、MCP四工具和年度筛选Mock冒烟通过；非实机或客户验收。')


if __name__ == '__main__':
    main()

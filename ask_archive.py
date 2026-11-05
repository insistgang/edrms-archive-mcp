import argparse
import json
import os
import sys

import config
from mcp_client import MCPClient, MCPClientError
from model_agent import ModelAgent


def main():
    parser = argparse.ArgumentParser(description='通过标准 MCP 查询档案的客户私有模型接入示例')
    parser.add_argument('question')
    parser.add_argument('--mcp-url', default=os.getenv('MCP_SERVER_URL', 'http://127.0.0.1:8000/mcp'))
    parser.add_argument('--token-env', default='EDRMS_TOKEN')
    parser.add_argument('--max-rounds', type=int, default=6)
    parser.add_argument('--json', action='store_true', help='输出答案和来源；不输出完整工具轨迹')
    args = parser.parse_args()
    token = os.getenv(args.token_env, '').strip()
    if not token:
        print('未注入当前用户的 EDRMS 令牌', file=sys.stderr)
        return 2
    if not config.LLM_API_BASE or not config.LLM_API_KEY:
        print('请先配置经客户批准的 LLM_API_BASE、LLM_API_KEY、LLM_MODEL', file=sys.stderr)
        return 2
    try:
        client = MCPClient(args.mcp_url, 'Bearer ' + token)
        agent = ModelAgent(None, None, mcp_client=client, max_rounds=args.max_rounds)
        result = agent.ask(args.question)
    except (MCPClientError, ValueError, RuntimeError, OSError):
        print('问答未完成：请检查 MCP、模型配置、当前身份及服务日志；未输出凭据或上游异常正文。', file=sys.stderr)
        return 1
    if result.get('stoppedBy'):
        print('模型达到工具调用轮数上限，未形成最终答案', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({'answer': result['answer'], 'citations': result['citations'],
                          'rounds': result['rounds']}, ensure_ascii=False, indent=2))
    else:
        print(result['answer'])
        if result['citations']:
            print('\n工具返回的来源（不代表逐句引用核验）：')
            for source in result['citations']:
                print(json.dumps(source, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())

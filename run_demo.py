import json
import time
from models import ArchiveQueryRequest
from archive_service import ArchiveRagService

def run_tests():
    service = ArchiveRagService()

    print("=" * 80)
    print(" 🏛️ 华泰证券 EDRMS MCP 对接服务 (路线 A) · 功能与权限前置过滤验证")
    print("=" * 80)

    test_cases = [
        {
            "title": "测试 1：普通员工身份检索「风险防控」",
            "token": "Bearer token_normal_user",
            "query": "2026年 风险防控",
            "expect": "只能看到内部公开档案（如元旦春节疫情防控及业务连续性保障），涉密风控方案应被过滤。"
        },
        {
            "title": "测试 2：风控总监身份检索「全面风险管理」",
            "token": "Bearer token_risk_director",
            "query": "全面风险管理 工作方案",
            "expect": "拥有机密权限，能够召回《华泰证券2026年度全面风险管理工作计划与合规底线要求》及附件。"
        },
        {
            "title": "测试 3：合规专员身份检索「反洗钱 客户尽职调查」",
            "token": "Bearer token_compliance_officer",
            "query": "反洗钱 客户尽职调查",
            "expect": "拥有秘密权限，能够召回《华泰证券关于进一步加强分支机构反洗钱与客户尽职调查风险防控的通知》。"
        },
        {
            "title": "测试 4：普通员工越权尝试检索「机密衍生品风控」",
            "token": "Bearer token_normal_user",
            "query": "衍生品业务双重风控审批机制",
            "expect": "物理隔离拦截，返回 0 条并输出合规未查阅提示，杜绝数据泄露与模型幻觉。"
        },
        {
            "title": "测试 5：跨门类检索监管收文「证监会 合规管理办法」",
            "token": "Bearer token_normal_user",
            "query": "证监会 合规管理办法",
            "expect": "成功召回收文（一文一件）门类中的证监会新规文件及附件。"
        }
    ]

    for idx, tc in enumerate(test_cases, 1):
        print(f"\n▶【Case {idx}】{tc['title']}")
        print(f"   🔑 身份令牌: {tc['token']}")
        print(f"   🔍 检索问句: {tc['query']}")
        print(f"   🎯 预期表现: {tc['expect']}")

        req = ArchiveQueryRequest(query=tc["query"])
        resp = service.execute_rag(req, auth_header=tc["token"])

        print(f"   📦 返回条目数: {len(resp.data)} 条")
        for item in resp.data:
            case_info = f" | 案卷ID: {item.caseId}" if item.caseId else " | 一文一件"
            print(f"      • [{item.archiveType}] {item.name} ({item.year}年{case_info})")
            print(f"        文号: {item.meta.get('docNo', '-')} | 附件ID: {item.attachmentsId}")

        print(f"   💬 自然语言输出 (msg):")
        print(f"      \"{resp.msg}\"")
        print("-" * 80)

    print("\n5 组 Mock 演示用例执行完毕；结果仅用于接口演示，不代表实机权限或客户验收通过。")

if __name__ == "__main__":
    run_tests()

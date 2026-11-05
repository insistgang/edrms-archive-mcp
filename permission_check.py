"""权限对照验证：用两个不同权限的用户检查检索、详情、原文列表和原文读取。

用法：
    EDRMS_TOKEN_A=<用户A令牌> EDRMS_TOKEN_B=<用户B令牌> \
    python3 permission_check.py --query "案卷1" --file-id <文件ID> --attachment-id 71

只有提供经业务方确认的预期矩阵时才判定 PASS / FAIL；没有预期时返回 REVIEW。
身份失效、接口异常和无法区分的“不可见”不会被当作权限验证通过。
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

import config
from edrms_client import EdrmsAuthError, EdrmsClient, EdrmsIntegrationError


class Probe:
    """对单个用户执行一条只读探测，并记录是否放行。"""

    def __init__(self, client: EdrmsClient, token: str, label: str):
        self.client = client
        self.token = token
        self.label = label

    def run(self, step: str, callable_, *args, **kwargs) -> Dict[str, Any]:
        try:
            value = callable_(*args, **kwargs)
        except EdrmsAuthError:
            return {"step": step, "user": self.label, "allowed": None, "reason": "身份无效，无法验证授权"}
        except EdrmsIntegrationError:
            return {"step": step, "user": self.label, "allowed": None, "reason": "上游异常或目标不可见，不能据此证明权限拒绝"}
        except Exception as exc:
            return {"step": step, "user": self.label, "allowed": None, "reason": "异常：" + type(exc).__name__}
        return {"step": step, "user": self.label, "allowed": True, "value": value}


def identifiers_of(value: Any, *keys: str) -> List[str]:
    """从检索/列表结果中提取可比较的标识，用于跨用户比对。"""
    found: List[str] = []
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if isinstance(value, (list, tuple)):
        for item in value:
            found.extend(identifiers_of(item, *keys))
    elif isinstance(value, dict):
        for key in keys:
            if key in value and value[key] is not None:
                found.append("%s=%s" % (key, value[key]))
        for child in value.values():
            if isinstance(child, (dict, list, tuple)):
                found.extend(identifiers_of(child, *keys))
    return list(dict.fromkeys(found))


def compare(results: List[Dict[str, Any]], step: str,
            expectations: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    by_user = {result["user"]: result for result in results if result["step"] == step}
    if len(by_user) < 2:
        return {"step": step, "verdict": "SKIP", "detail": "缺少对照用户"}
    expected = (expectations or {}).get(step)
    if not expected:
        return {"step": step, "verdict": "REVIEW", "detail": "未提供业务方确认的预期矩阵，不判定权限通过"}
    for user, result in by_user.items():
        rule = expected.get(user)
        if not isinstance(rule, dict) or not isinstance(rule.get("allowed"), bool):
            return {"step": step, "verdict": "REVIEW", "detail": "用户 %s 缺少明确预期" % user}
        if result["allowed"] is None:
            return {"step": step, "verdict": "FAIL", "detail": "用户 %s 的探测异常，无法验证授权" % user}
        if result["allowed"] != rule["allowed"]:
            return {"step": step, "verdict": "FAIL", "detail": "用户 %s 的实际可见性与预期不符" % user}
        if rule["allowed"]:
            expected_ids = rule.get("identifiers")
            if not isinstance(expected_ids, list):
                return {"step": step, "verdict": "REVIEW", "detail": "用户 %s 缺少预期记录标识" % user}
            actual = set(identifiers_of(result.get("value"), "fileId", "attachmentId"))
            if actual != set(expected_ids):
                return {"step": step, "verdict": "FAIL", "detail": "用户 %s 返回的记录标识与预期不符" % user}
    return {"step": step, "verdict": "PASS", "detail": "所有用户可见性及记录标识均与确认矩阵一致"}


def run(client: EdrmsClient, tokens: Dict[str, str], queries: List[str],
        file_id: str, attachment_id: Optional[int], archive_type_id: str,
        expectations: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if len(tokens) < 2 or len(set(tokens.values())) != len(tokens):
        raise ValueError("必须提供至少两个不同用户的独立令牌")
    if file_id and not archive_type_id:
        raise ValueError("直接 ID 探测必须提供门类 ID")
    if attachment_id and not queries:
        raise ValueError("原文读取探测需要对应检索词")
    read_query = queries[0] if queries else ""
    probes = {label: Probe(client, token, label) for label, token in tokens.items()}
    results: List[Dict[str, Any]] = []

    for query in queries:
        for label, probe in probes.items():
            results.append(probe.run(
                "search:%s" % query,
                lambda probe=probe, query=query: probe.client.search_archives(query, probe.token, None, 1, 20)
            ))

    if file_id:
        for label, probe in probes.items():
            results.append(probe.run(
                "detail:file",
                lambda probe=probe: probe.client.get_archive_detail(
                    archive_type_id, "file", file_id, probe.token
                )
            ))
            results.append(probe.run(
                "originals:file",
                lambda probe=probe: probe.client.list_originals(
                    archive_type_id, file_id, probe.token, 1, 100
                )
            ))
            if attachment_id:
                results.append(probe.run(
                    "read:original",
                    lambda probe=probe: probe.client.read_original(
                        archive_type_id, file_id, attachment_id, probe.token, read_query, 5000, 10
                    )
                ))

    steps = []
    seen = set()
    for result in results:
        if result["step"] not in seen:
            seen.add(result["step"])
            steps.append(compare(results, result["step"], expectations))

    return {
        "baseUrl": client.base_url,
        "users": sorted(tokens),
        "steps": steps,
        "detail": [
            {
                "step": result["step"],
                "user": result["user"],
                "allowed": result["allowed"],
                "reason": result.get("reason", "")[:200],
                "visibleIdentifiers": identifiers_of(result.get("value"), "fileId", "attachmentId", "name")[:20]
            }
            for result in results
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="鸿翼 EDRMS 两用户权限对照验证")
    parser.add_argument("--query", action="append", default=[], help="检索词，可重复")
    parser.add_argument("--file-id", default="", help="用于详情/原文探测的文件 ID")
    parser.add_argument("--attachment-id", type=int, default=0, help="用于原文读取探测的原文记录 ID")
    parser.add_argument("--archive-type-id", default="", help="文件所属门类 ID")
    parser.add_argument("--base-url", default=config.EDRMS_BASE_URL)
    parser.add_argument("--token-a-env", default="EDRMS_TOKEN_A")
    parser.add_argument("--token-b-env", default="EDRMS_TOKEN_B")
    parser.add_argument("--out", default="", help="JSON 报告输出路径")
    parser.add_argument("--expected", default="", help="经业务方确认的预期矩阵 JSON 文件")
    args = parser.parse_args()

    tokens = {
        "A": os.getenv(args.token_a_env, "").strip(),
        "B": os.getenv(args.token_b_env, "").strip()
    }
    missing = [label for label, token in tokens.items() if not token]
    if missing:
        print("缺少令牌环境变量，用户 %s 未配置" % "、".join(missing), file=sys.stderr)
        return 2
    if not args.query and not args.file_id:
        print("至少需要一个 --query 或 --file-id", file=sys.stderr)
        return 2

    config.RUN_MODE = "LIVE"
    client = EdrmsClient(base_url=args.base_url)
    try:
        expected = None
        if args.expected:
            with open(args.expected, encoding="utf-8") as handle:
                expected = json.load(handle)
            if not isinstance(expected, dict):
                raise ValueError("预期矩阵必须是 JSON 对象")
        report = run(client, tokens, args.query, args.file_id,
                     args.attachment_id or None, args.archive_type_id, expected)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print("=== 权限对照结果 ===")
    for step in report["steps"]:
        print("[%s] %s —— %s" % (step["verdict"], step["step"], step["detail"]))
    print("--- 明细 ---")
    for item in report["detail"]:
        print("%s | %s | 允许=%s | %s" % (
            item["step"], item["user"], item["allowed"], item["reason"] or ""
        ))
        if item["visibleIdentifiers"]:
            print("    可见标识：%s" % ", ".join(item["visibleIdentifiers"]))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print("报告已写入 %s" % args.out)

    if any(step["verdict"] == "FAIL" for step in report["steps"]):
        return 1
    return 0 if report["steps"] and all(step["verdict"] == "PASS" for step in report["steps"]) else 2


if __name__ == "__main__":
    sys.exit(main())

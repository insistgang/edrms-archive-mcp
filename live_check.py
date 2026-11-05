"""实机巡检：只读核对检索、详情、案卷卷内关系与电子原文读取。

用法：
    EDRMS_TOKEN=<用户令牌> python3 live_check.py --query "案卷1" --out report.json

令牌只从环境变量读取，不写入报告、不打印。
"""

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List

import config
from edrms_client import EdrmsAuthError, EdrmsClient, EdrmsIntegrationError


def timed(callable_, *args, **kwargs):
    start = time.perf_counter()
    try:
        return callable_(*args, **kwargs), (time.perf_counter() - start) * 1000, None
    except (EdrmsAuthError, EdrmsIntegrationError) as exc:
        return None, (time.perf_counter() - start) * 1000, str(exc)


def run_check(client: EdrmsClient, token: str, queries: List[str], case_query: str,
              max_chars: int, skip_read: bool, catalog: bool = False) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "baseUrl": client.base_url,
        "queries": queries,
        "cases": [],
        "files": [],
        "catalog": {},
        "latencyMs": {"search": [], "caseSearch": [], "caseDetail": [], "detail": [], "listOriginals": [], "read": []},
        "failures": [],
        "notes": []
    }

    if case_query:
        cases, elapsed, error = timed(client.search_cases, case_query, token)
        report["latencyMs"]["caseSearch"].append(round(elapsed, 1))
        if error:
            report["failures"].append({"step": "caseSearch", "query": case_query, "error": error})
        else:
            for case in cases:
                entry = dict(case)
                detail, case_elapsed, case_error = timed(
                    client.get_archive_detail,
                    case.get("archiveTypeId") or "",
                    "case",
                    case["caseId"],
                    token,
                    True
                )
                report["latencyMs"]["caseDetail"].append(round(case_elapsed, 1))
                if case_error:
                    entry["detailError"] = case_error
                    report["failures"].append({"step": "caseDetail", "caseId": case["caseId"], "error": case_error})
                else:
                    page = detail.get("filePage") or {}
                    entry["filesInCase"] = page.get("total")
                    entry["filesReturned"] = page.get("returnedCount")
                    entry["fileCountingScope"] = page.get("countingScope")
                    if not page.get("total"):
                        report["notes"].append(
                            "案卷 %s 未返回卷内文件，立卷关联无法在该数据集上验证。" % case["caseId"]
                        )
                report["cases"].append(entry)

    for query in queries:
        result, elapsed, error = timed(client.search_archives, query, token, None, 1, 200)
        report["latencyMs"]["search"].append(round(elapsed, 1))
        if error:
            report["failures"].append({"step": "search", "query": query, "error": error})
            continue
        items = result[0] if isinstance(result, tuple) else result
        for item in items:
            entry = {
                "fileId": item.fileId,
                "name": item.name,
                "year": item.year,
                "archiveTypeId": item.archiveTypeId,
                "caseId": item.caseId,
                "attachmentsId": item.attachmentsId,
                "attachments": []
            }
            detail, detail_elapsed, detail_error = timed(
                client.get_archive_detail, item.archiveTypeId, "file", item.fileId, token
            )
            report["latencyMs"]["detail"].append(round(detail_elapsed, 1))
            if detail_error:
                entry["detailError"] = detail_error
                report["failures"].append({"step": "detail", "fileId": item.fileId, "error": detail_error})
            else:
                entity = detail.get("entity", {})
                entry["detail"] = {
                    "name": entity.get("name"),
                    "year": entity.get("year"),
                    "caseId": entity.get("caseId"),
                    "docNo": (entity.get("meta") or {}).get("docNo")
                }

            originals, list_elapsed, list_error = timed(
                client.list_originals, item.archiveTypeId, item.fileId, token, 1, 100
            )
            report["latencyMs"]["listOriginals"].append(round(list_elapsed, 1))
            if list_error:
                entry["originalsError"] = list_error
                report["failures"].append({"step": "listOriginals", "fileId": item.fileId, "error": list_error})
                report["files"].append(entry)
                continue
            entry["originalCount"] = originals["page"]["total"]
            for original in originals["items"]:
                attachment = {
                    "attachmentId": original["attachmentId"],
                    "name": original.get("name"),
                    "format": original.get("format"),
                    "sizeBytes": original.get("sizeBytes")
                }
                if not skip_read:
                    result, read_elapsed, read_error = timed(
                        client.read_original, item.archiveTypeId, item.fileId,
                        original["attachmentId"], token, query or item.name, max_chars, 100
                    )
                    report["latencyMs"]["read"].append(round(read_elapsed, 1))
                    if read_error:
                        attachment["status"] = "error"
                        attachment["error"] = read_error
                        report["failures"].append({
                            "step": "read",
                            "fileId": item.fileId,
                            "attachmentId": original["attachmentId"],
                            "error": read_error
                        })
                    else:
                        attachment["status"] = result["status"]
                        attachment["format"] = result["format"]
                        attachment["truncated"] = result["truncated"]
                        attachment["parser"] = result["parser"]
                        attachment["integrity"] = result.get("integrity")
                        attachment["members"] = [
                            {"path": fragment["source"].get("path"), "characters": len(fragment["text"])}
                            for fragment in result["fragments"]
                        ]
                        attachment["characterTotal"] = sum(len(f["text"]) for f in result["fragments"])
                entry["attachments"].append(attachment)
            report["files"].append(entry)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="鸿翼 EDRMS MCP 实机只读巡检")
    parser.add_argument("--query", action="append", default=[], help="文件检索词，可重复")
    parser.add_argument("--case-query", default="", help="案卷检索词")
    parser.add_argument("--base-url", default=config.EDRMS_BASE_URL)
    parser.add_argument("--token-env", default="EDRMS_TOKEN", help="存放用户令牌的环境变量名")
    parser.add_argument("--out", default="", help="JSON 报告输出路径")
    parser.add_argument("--max-chars", type=int, default=20000)
    parser.add_argument("--skip-read", action="store_true", help="只查列表，不下载原文")
    parser.add_argument("--catalog", action="store_true", help="枚举账号可见的全部文件与案卷")
    args = parser.parse_args()

    token = os.getenv(args.token_env, "").strip()
    if not token:
        print("未找到令牌环境变量 %s" % args.token_env, file=sys.stderr)
        return 2
    queries = list(args.query)
    if args.catalog:
        queries.append("")
    if not queries and not args.case_query:
        print("至少需要一个 --query、--case-query 或 --catalog", file=sys.stderr)
        return 2

    config.RUN_MODE = "LIVE"
    client = EdrmsClient(base_url=args.base_url)
    report = run_check(client, token, queries, args.case_query, args.max_chars, args.skip_read)
    report["catalog"] = {
        "enabled": args.catalog,
        "fileCount": len(report["files"]),
        "filesWithOriginals": sum(1 for item in report["files"] if item.get("originalCount")),
        "filesWithoutOriginals": sum(1 for item in report["files"] if item.get("originalCount") == 0)
    }

    print("=== 实机巡检结果 ===")
    print("案卷：%d 个；文件：%d 个；失败步骤：%d" % (
        len(report["cases"]), len(report["files"]), len(report["failures"])))
    if args.catalog:
        print("全量目录：有原文的文件 %d 个，无原文的文件 %d 个" % (
            report["catalog"]["filesWithOriginals"], report["catalog"]["filesWithoutOriginals"]))
    for name, values in report["latencyMs"].items():
        if values:
            print("  %s 延迟 ms：最小 %.1f，最大 %.1f，平均 %.1f" % (
                name, min(values), max(values), sum(values) / len(values)))
    for note in report["notes"]:
        print("  提示：%s" % note)
    for failure in report["failures"]:
        print("  失败：%s %s" % (failure["step"], failure.get("error")))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print("报告已写入 %s" % args.out)
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())

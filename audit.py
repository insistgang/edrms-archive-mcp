"""最小审计日志：记录请求、身份指纹、工具与结果数量，不记录令牌与原文。

启用方式：设置环境变量 EDRMS_AUDIT_LOG 为写入路径（JSONL，每行一条）。
未设置时完全不写盘，便于信创环境按需开启。
"""

import hashlib
import json
import os
import time
from typing import Any, Dict, Optional

FORBIDDEN_KEYS = {"token", "authorization", "password", "content", "text", "fragments", "payload"}


def fingerprint(token: Optional[str]) -> Optional[str]:
    """把令牌折叠成不可逆短指纹，用于关联同一用户的多次请求。"""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _scrub(fields: Dict[str, Any]) -> Dict[str, Any]:
    clean = {}
    for key, value in fields.items():
        if key.lower() in FORBIDDEN_KEYS:
            continue
        if isinstance(value, str) and len(value) > 200:
            value = value[:200] + "…"
        clean[key] = value
    return clean


def log_event(event: str, **fields: Any) -> None:
    """写一条审计记录；未配置路径时静默跳过。"""
    path = os.getenv("EDRMS_AUDIT_LOG", "").strip()
    if not path:
        return
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
    record.update(_scrub(fields))
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # 审计写入失败不能影响业务主流程。
        pass

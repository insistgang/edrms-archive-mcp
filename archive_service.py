import json
import urllib.request
from typing import List, Dict, Any, Optional
import config
from models import ArchiveItem, ArchiveQueryRequest, ArchiveQueryResponse
from edrms_client import EdrmsAuthError, EdrmsClient

class ArchiveRagService:
    def __init__(self):
        self.client = EdrmsClient()

    def get_detail(
        self,
        archive_type_id: str,
        entity_type: str,
        entity_id: str,
        auth_header: Optional[str],
        include_files: bool = False,
        file_page_index: int = 1,
        file_page_size: int = 20
    ) -> Dict[str, Any]:
        return self.client.get_archive_detail(
            archive_type_id, entity_type, entity_id, auth_header,
            include_files, file_page_index, file_page_size
        )

    def list_originals(
        self,
        archive_type_id: str,
        file_id: str,
        auth_header: Optional[str],
        page_index: int = 1,
        page_size: int = 20
    ) -> Dict[str, Any]:
        return self.client.list_originals(
            archive_type_id, file_id, auth_header, page_index, page_size
        )

    def read_original(
        self,
        archive_type_id: str,
        file_id: str,
        attachment_id: int,
        auth_header: Optional[str],
        query: Optional[str] = None,
        max_chars: int = 20000,
        max_chunks: int = 10
    ) -> Dict[str, Any]:
        return self.client.read_original(
            archive_type_id, file_id, attachment_id, auth_header,
            query, max_chars, max_chunks
        )

    def execute_rag(self, req: ArchiveQueryRequest, auth_header: Optional[str] = None) -> ArchiveQueryResponse:
        """执行端到端检索并组装 {data, msg} 输出"""
        if not auth_header or not auth_header.strip():
            raise EdrmsAuthError("档案查询需要有效的用户令牌。")
        query = req.query.strip()
        if not query:
            return ArchiveQueryResponse(data=[], msg="请输入有效的检索关键词或查询问题。")

        # 1. 执行有权检索召回
        items, retrieval_summary = self.client.search_archives(
            query=query,
            auth_header=auth_header,
            archive_type_id=req.archiveTypeId,
            page_index=req.pageIndex,
            page_size=req.pageSize,
            **({"filters": req.filters} if req.filters is not None else {})
        )

        # 2. 生成自然语言回答 msg
        if not items:
            msg = f"未查询到与「{query}」相关的档案资料。请核对关键词或联系档案管理员。"
        else:
            msg = self._generate_answer(query, items)

        return ArchiveQueryResponse(data=items, msg=msg)

    def _generate_answer(self, query: str, items: List[ArchiveItem]) -> str:
        """默认返回目录摘要；只有显式批准时才调用客户模型生成兼容接口的 msg。"""
        if config.LLM_GENERATE_MSG and config.LLM_API_BASE and config.LLM_API_KEY:
            try:
                return self._call_external_llm(query, items)
            except Exception as e:
                print(f"[Warning] Call external LLM failed: {e}, using local template")

        # 本地精准模板（完全基于召回事实，严格防幻觉）
        years = sorted(list({str(item.year) for item in items}))
        year_str = "、".join(years) + "年" if years else ""
        file_names = "；".join([f"《{item.name}》（{item.meta.get('docNo', '无文号')}）" for item in items[:3]])

        reply = (
            f"已为您检索到{year_str}相关档案共 {len(items)} 份，"
            f"主要包括：{file_names}。"
            f"请按系统权限查阅相应档案。"
        )
        return reply

    def _call_external_llm(self, query: str, items: List[ArchiveItem]) -> str:
        """调用华泰提供的私有化大模型接口生成回答"""
        context_lines = []
        for idx, item in enumerate(items, 1):
            context_lines.append(
                f"[{idx}] 标题：{item.name} | 年度：{item.year} | 文号：{item.meta.get('docNo', '')} | "
                f"形成单位：{item.meta.get('formUnit', '')} | 摘要：{item.meta.get('summary', '')}"
            )
        context_str = "\n".join(context_lines)

        prompt = (
            "你是一个证券档案管理助手。请严格仅依据以下提供的有权限档案上下文，回答用户的问题。\n"
            "严禁编造任何未在上下文中出现的档案名称、文号或结论。\n\n"
            f"【档案上下文】：\n{context_str}\n\n"
            f"【用户问题】：{query}\n\n"
            "【回答要求】：简明扼要，说明找到了哪些文件，并提醒用户查阅对应原文附件。"
        )

        url = f"{config.LLM_API_BASE.rstrip('/')}/chat/completions"
        payload = {
            "model": config.LLM_MODEL,
            "messages": [
                {"role": "system", "content": "你是华泰证券数字档案助手，恪守合规与保密红线。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.LLM_API_KEY}"
        }

        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()

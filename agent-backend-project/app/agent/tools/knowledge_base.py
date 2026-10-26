"""知识库管理原子能力：让模型能"看见"用户有哪些库、库里有什么资料，并能建库。

**这是 tool，不是 worker**（划分标准见 `app/agent/tools/__init__.py`）：它们没有角色人格，
只是把知识库的现状读出来 / 建一个新库，所以归 tool。

为什么需要（第 23 轮，用户 2026-10-19 要求）：出题参数改成"在对话里问用户"之后，
模型要能回答"我有哪些知识库""那个库里有什么"并把用户说的库名解析成 `knowledge_base_id`
（`delegate_quiz` 的入参）。没有这几个工具，模型只能反问用户"知识库ID是多少"。

写权限：只有 `create_knowledge_base`（用户判定的低风险写；`save_note` 之外的第二把）。
它不做确认卡——建空库可撤销（库里没资料，删掉零损失），而"先确认再建"会让一个
纯对话动作变成两轮。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from langchain_core.tools import tool
from sqlalchemy import func, select

from app.agent.tools.tool_context import InjectedToolContext
from app.db.models import KnowledgeChunk, Resource
from app.services.knowledge_base_service import KnowledgeBaseService

# 单次返回的文档条数上限（模型一次看几十条没意义，还会挤爆上下文）
MAX_DOCUMENTS = 50
# 时间分布最多列出的月份数
MAX_MONTHS = 12


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _resource_kind(file_name: Any, resource_type: Any) -> str:
    """资料类型：优先看扩展名（用户认知里的"类型"就是它），退回 resource_type。"""
    name = str(file_name or "")
    if "." in name:
        ext = name.rsplit(".", 1)[-1].strip().lower()
        if ext:
            return ext
    return {0: "文件", 1: "图片", 2: "音频"}.get(int(resource_type or 0), "文件")


def _owns_knowledge_base(db: Any, user_id: int, knowledge_base_id: int) -> bool:
    """这个库是不是当前用户的（工具里不抛异常，让模型看到可读结果）。"""
    try:
        KnowledgeBaseService.get_or_404(
            db, user_id=user_id, knowledge_base_id=int(knowledge_base_id)
        )
    except Exception:  # noqa: BLE001 - 越权与不存在对外都是"这个库不可用"
        return False
    return True


@tool
def list_knowledge_bases(*, ctx: InjectedToolContext) -> str:
    """列出用户的知识库：名称、id、文档数、最近更新时间。

    用户问"我有哪些知识库""用哪个库出题"时先调它；**不要**让用户报知识库ID。
    拿到 id 后传给 `delegate_quiz(knowledge_base_id=...)` 或 `search_material` 前先指定范围。
    """
    user_id = int(ctx["user_id"])
    db = ctx["db"]
    items = KnowledgeBaseService.list_knowledge_bases(db, user_id=user_id)
    bases = [
        {
            "knowledge_base_id": int(item["id"]),
            "name": str(item.get("name") or ""),
            "document_count": int(item.get("resource_count") or 0),
            "ready_count": int(item.get("ready_count") or 0),
            "updated_at": _format_ts(item.get("quiz_extracted_at") or item.get("created_at")),
            "can_quiz": bool(item.get("can_quiz")),
        }
        for item in items
    ]
    payload: dict[str, Any] = {"count": len(bases), "knowledge_bases": bases}
    if not bases:
        payload["note"] = "用户还没有知识库；可以建议先建一个（create_knowledge_base）再上传资料"
    return _ok(payload)


@tool
def list_documents(knowledge_base_id: int, limit: int = 20, *, ctx: InjectedToolContext) -> str:
    """列出某个知识库里的资料：文件名、类型、大小（解析后的正文字数）、入库状态与时间。

    用于"那个库里都有什么""有没有讲过 X 的资料"。**库里不存文件字节大小**，
    所以这里给的是清洗后的正文字数与切片数（更能代表"能用来出题的内容有多少"）。

    Args:
        knowledge_base_id: 知识库 id（先用 list_knowledge_bases 拿）。
        limit: 返回条数（1–50，默认 20；按入库时间倒序）。
    """
    user_id = int(ctx["user_id"])
    db = ctx["db"]
    knowledge_base_id = int(knowledge_base_id)
    if not _owns_knowledge_base(db, user_id, knowledge_base_id):
        return _ok({"error": "知识库不存在或不属于当前用户", "knowledge_base_id": knowledge_base_id})

    size = max(1, min(int(limit) if limit else 20, MAX_DOCUMENTS))
    rows = list(
        db.scalars(
            select(Resource)
            .where(Resource.knowledge_base_id == knowledge_base_id)
            .order_by(Resource.create_time.desc(), Resource.id.desc())
            .limit(size)
        )
    )
    documents = [
        {
            "resource_id": int(resource.id),
            "file_name": str(resource.file_name or ""),
            "kind": _resource_kind(resource.file_name, resource.resource_type),
            "char_count": int(resource.content_chars or 0),
            "ingest_status": str(resource.ingest_status or ""),
            "group_name": resource.group_name,
            "created_at": _format_ts(resource.create_time),
        }
        for resource in rows
    ]
    payload: dict[str, Any] = {
        "knowledge_base_id": knowledge_base_id,
        "count": len(documents),
        "documents": documents,
    }
    if not documents:
        payload["note"] = "这个知识库里还没有资料"
    return _ok(payload)


@tool
def get_document_summary(knowledge_base_id: int, *, ctx: InjectedToolContext) -> str:
    """汇总某个知识库的构成：按资料类型计数、总量、正文字数与入库时间分布。

    适合回答"这个库值不值得用来出题""我传了多少资料"。比 `list_documents` 更省上下文。
    """
    user_id = int(ctx["user_id"])
    db = ctx["db"]
    knowledge_base_id = int(knowledge_base_id)
    if not _owns_knowledge_base(db, user_id, knowledge_base_id):
        return _ok({"error": "知识库不存在或不属于当前用户", "knowledge_base_id": knowledge_base_id})

    rows = db.execute(
        select(
            Resource.file_name,
            Resource.resource_type,
            Resource.content_chars,
            Resource.create_time,
            Resource.ingest_status,
        ).where(Resource.knowledge_base_id == knowledge_base_id)
    ).all()
    chunk_count = int(
        db.scalar(
            select(func.count(KnowledgeChunk.id))
            .join(Resource, Resource.id == KnowledgeChunk.resource_id)
            .where(Resource.knowledge_base_id == knowledge_base_id)
        )
        or 0
    )
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_month: dict[str, int] = {}
    char_total = 0
    for file_name, resource_type, content_chars, create_time, ingest_status in rows:
        kind = _resource_kind(file_name, resource_type)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        status = str(ingest_status or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        char_total += int(content_chars or 0)
        month = _format_month(create_time)
        if month:
            by_month[month] = by_month.get(month, 0) + 1
    months = sorted(by_month.items(), reverse=True)[:MAX_MONTHS]
    return _ok(
        {
            "knowledge_base_id": knowledge_base_id,
            "document_count": len(rows),
            "char_total": char_total,
            "chunk_count": chunk_count,
            "by_kind": by_kind,
            "by_ingest_status": by_status,
            "by_month": dict(months),
        }
    )


@tool
def create_knowledge_base(name: str, description: str = "", *, ctx: InjectedToolContext) -> str:
    """新建一个知识库（低风险写操作）。

    只在用户明确要"新建一个库"时调用；建完提醒用户去知识库页上传资料（这个工具不能上传文件）。
    库名会去掉首尾空格，最长 128 字符。

    Args:
        name: 知识库名称（如"机器学习"）。
        description: 一句话说明（可空）。
    """
    user_id = int(ctx["user_id"])
    db = ctx["db"]
    clean_name = str(name or "").strip()
    if not clean_name:
        return _ok({"error": "知识库名称不能为空"})
    if len(clean_name) > 128:
        return _ok({"error": "知识库名称最长 128 个字符"})
    try:
        item = KnowledgeBaseService.create(
            db, user_id=user_id, name=clean_name, description=str(description or "").strip()
        )
    except Exception as exc:  # noqa: BLE001 - 建库失败是"观察结果"，不该打断 loop
        return _ok({"error": f"创建知识库失败：{type(exc).__name__}: {exc}"})
    return _ok(
        {
            "knowledge_base_id": int(item["id"]),
            "name": str(item.get("name") or ""),
            "document_count": int(item.get("resource_count") or 0),
            "note": "知识库已创建；要让它能出题，还需要在知识库页上传资料",
        }
    )


def _format_ts(value: Any) -> str:
    """时间统一成 `YYYY-MM-DD HH:MM`（模型读字符串比读时间戳省事）。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, (int, float)) and value:
        try:
            return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            return ""
    return str(value or "")


def _format_month(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m")
    return ""

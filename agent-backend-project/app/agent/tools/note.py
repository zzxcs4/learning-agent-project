"""笔记原子能力工具：直接保存 Markdown 笔记。

**本文件是唯一带写权限的工具模块**（升级规划 §7.3「权限隔离的必要」）：
知识库里全是用户上传的 PDF，是一个真实的注入面（资料里可以藏指令）。
把写能力收窄到"保存笔记"这一个显式动作，答疑路径就不会因为读了恶意资料而写坏笔记与画像。

注意"归档一次测评"走的是 `delegate_archive`（worker，有自己的角色提示词），
不是这个工具——原子能力与角色行为的边界见升级规划 §7.1。
"""

from __future__ import annotations

import json
import time

from langchain_core.tools import tool

from app.agent.tools.tool_context import InjectedToolContext
from app.services.note_service import NoteManagementService

MAX_TITLE_CHARS = 200
MAX_CONTENT_CHARS = 20000


@tool
def save_note(
    content_md: str,
    title: str = "",
    tags: list[str] | None = None,
    *,
    ctx: InjectedToolContext,
) -> str:
    """把一段 Markdown 内容保存为用户的笔记。

    Args:
        content_md: 笔记正文（Markdown）。
        title: 笔记标题；留空则取正文首行。
        tags: 知识点标签数组，可留空。
    """
    body = (content_md or "").strip()
    if not body:
        return json.dumps({"saved": False, "reason": "正文为空，未保存"}, ensure_ascii=False)

    body = body[:MAX_CONTENT_CHARS]
    resolved_title = (title or "").strip() or _first_line(body)

    # 每次调用都是一条新笔记：用"会话+时间戳"占位，避免撞上 (user_id, source_run_id) 唯一约束。
    # 有 run_id 时用它，让同一场 loop 的重复调用幂等落在同一来源上（服务层会返回 409）。
    run_id = ctx.get("run_id")
    source_run_id = str(run_id) if run_id else f"lp-{ctx['session_id']}-{int(time.time() * 1000)}"

    note = NoteManagementService.create_note(
        db=ctx["db"],
        user_id=int(ctx["user_id"]),
        title=resolved_title[:MAX_TITLE_CHARS],
        content=body,
        note_type="agent_capture",
        tags=[str(tag)[:64] for tag in (tags or [])][:20],
        metadata={"source": "learning_partner", "session_id": ctx["session_id"]},
        source_run_id=source_run_id,
    )
    return json.dumps(
        {
            "saved": True,
            "note_id": int(note.id),
            "title": note.title,
            "chars": len(body),
        },
        ensure_ascii=False,
    )


def _first_line(body: str) -> str:
    """标题兜底：取正文第一行，去掉 Markdown 标题符号，截断到列宽。"""
    for line in body.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:MAX_TITLE_CHARS]
    return "未命名笔记"

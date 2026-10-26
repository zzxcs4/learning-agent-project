"""笔记归档服务：把学习测评结果整理成 Markdown 笔记结构。"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.agent.llm import get_chat_model
from app.agent.workflows.common import ensure_list
from app.core import raise_error
from app.db.models import Note
from app.services.prompt_template_service import AGENT_CONFIG, prompt_template_manager
from app.services.workflow_run_service import WorkflowRunService


NOTE_ARCHIVER_AGENT = "note_archiver"
REQUIRED_CONTENT_HEADINGS = (
    "## 测评概览",
    "## 薄弱点总结",
    "## 错题复盘",
    "## 复习建议",
)
PLACEHOLDER_PATTERN = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")


class NoteArchiveService:
    """负责把测评工作流状态转换成可保存的 Markdown 笔记。"""

    def __init__(self, db: Session, llm: Any = None, provider: str = "deepseek"):
        self.db = db
        self.llm = llm
        self.provider = provider

    def archive_from_run(self, user_id: int, run_id: str) -> dict[str, Any]:
        """通过 run_id 读取 workflow_runs.state_json 并生成笔记结构。"""
        run = WorkflowRunService.get_run_or_404(self.db, user_id, run_id)
        state = dict(run.state_json or {})
        state["run_id"] = run.run_id
        state["status"] = run.status
        return self.archive_from_state(user_id, state)

    def archive_from_state(self, user_id: int, state: dict[str, Any]) -> dict[str, Any]:
        """通过已有工作流 state 生成笔记结构，适合 A2A 自动派发。"""
        if state.get("user_id") is not None and int(state["user_id"]) != user_id:
            raise_error(code=403, message="无权归档其他用户的学习测评结果")

        prompt = self._build_prompt(state)
        try:
            return self._invoke_note_markdown(prompt, state)
        except Exception as exc:
            print(f"[NoteArchive] 笔记生成失败：run_id={state.get('run_id')}, prompt_len={len(prompt)}, error={exc}")
            raise_error(code=500, message=f"笔记生成失败: {exc}")

    def _build_prompt(self, state: dict[str, Any]) -> str:
        """优先读取数据库提示词模板，缺失时使用服务内置兜底模板。"""
        state_json = json.dumps(state, ensure_ascii=False, indent=2)
        scene = AGENT_CONFIG[NOTE_ARCHIVER_AGENT]["scene"]
        template = prompt_template_manager.get_full_prompt(NOTE_ARCHIVER_AGENT, scene, self.db)
        if not template:
            template = self._default_prompt_template()
        if "{state_json}" not in template:
            print(f"[NoteArchive] 提示词缺少 state_json 占位符，已自动追加：run_id={state.get('run_id')}")
            template = template.rstrip() + "\n\n输入 state JSON：\n{state_json}"
        return self._inject_template_vars(template, state).replace("{state_json}", state_json).strip()

    def _invoke_note_markdown(self, prompt: str, state: dict[str, Any]) -> dict[str, Any]:
        """调用笔记模型生成 Markdown 正文，并补齐归档字段。"""
        model = self.llm or get_chat_model(provider=self.provider)
        response = model.invoke(prompt)
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "".join(str(item) for item in content)
        text = str(content or "")
        print(f"[NoteArchive] 模型原始输出：run_id={state.get('run_id')}, length={len(text.strip())}, preview={text.strip()[:200]}")
        if not text.strip():
            raise ValueError("模型没有返回笔记内容")
        return self._archive_from_model_text(text, state)

    def _inject_template_vars(self, template: str, state: dict[str, Any]) -> str:
        """只替换测评相关变量，其他占位符直接清空。"""
        quiz = state.get("quiz") if isinstance(state.get("quiz"), dict) else {}
        weaknesses = [str(item).strip() for item in ensure_list(state.get("weaknesses")) if str(item).strip()]
        variables = {
            "request_text": str(state.get("request_text") or ""),
            "question_count": str(quiz.get("question_count") or state.get("question_count") or ""),
            "weak_topics": "、".join(weaknesses),
        }
        for key, value in variables.items():
            template = template.replace("{" + key + "}", str(value))
        for placeholder in sorted(set(PLACEHOLDER_PATTERN.findall(template)) - {"{state_json}"}):
            template = template.replace(placeholder, "")
        return template

    def _archive_from_model_text(self, text: str, state: dict[str, Any]) -> dict[str, Any]:
        """模型生成了 Markdown/文本时，保留原文并补齐归档字段。"""
        content = PLACEHOLDER_PATTERN.sub("", text.strip())
        weaknesses = [str(item).strip() for item in ensure_list(state.get("weaknesses")) if str(item).strip()]
        quiz = state.get("quiz") if isinstance(state.get("quiz"), dict) else {}
        questions = ensure_list(quiz.get("questions")) if isinstance(quiz, dict) else []
        title = self._extract_title_from_text(content)
        metadata = {
            "score": int(state.get("score") or 0),
            "source_run_id": str(state.get("run_id") or ""),
            "source_agent": "learning_evaluator",
            "weaknesses": weaknesses,
            "question_count": int(quiz.get("question_count") or len(questions)),
            "content_format": "markdown",
        }
        return {"title": title, "content": content, "tags": (weaknesses or ["学习测评"])[:3], "metadata": metadata}

    @staticmethod
    def _extract_title_from_text(content: str) -> str:
        first_line = ""
        for line in content.splitlines():
            stripped = line.strip()
            if not first_line and stripped:
                first_line = stripped
            if not stripped.startswith("#"):
                continue
            clean = stripped.lstrip("#").strip()
            if clean:
                return clean[:80]
        return first_line[:80] if first_line else "学习测评复盘"

    @staticmethod
    def _default_prompt_template() -> str:
        return """
你是 note_archiver 笔记归档智能体。请把工作流 state 整理成一份可保存、可渲染、可检索的 Markdown 学习笔记。

输入 state JSON：
{state_json}

只输出 Markdown 正文，不要输出 JSON，不要输出外层代码块，可适当生成 Emoji。

严格要求：
1. 正文必须完整包含：## 测评概览、## 薄弱点总结、## 错题复盘、## 复习建议。
2. 只围绕本次测评结果、薄弱点和错题复盘展开，不要输出目标岗位、学习者画像或学习者背景信息。
3. 不要编造不存在的题目、选项或答案。
4. 不要输出“好的”“已收到”等对话式开场白，直接从 Markdown 标题开始。
5. 只能输出 Markdown 正文。
""".strip()


class NoteService:
    """负责笔记幂等保存和工作流追踪字段回写。"""

    def __init__(self, db: Session):
        self.db = db

    def save_learning_quiz_note(
        self,
        *,
        user_id: int,
        session_id: int,
        source_run_id: str,
        archive: dict[str, Any],
        note_type: str | None = None,
    ) -> dict[str, Any]:
        """幂等保存笔记，已有则直接返回。"""
        note = (
            self.db.query(Note)
            .filter(
                Note.user_id == user_id,
                Note.source_run_id == source_run_id,
            )
            .first()
        )
        if note is None:
            print(f"[NoteArchive] 保存新笔记：run_id={source_run_id}, user_id={user_id}")
            note = Note(
                user_id=user_id,
                session_id=session_id,
                source_run_id=source_run_id,
                note_type=note_type,
                title=str(archive.get("title") or "学习测评复盘"),
                content=str(archive.get("content") or ""),
                tags=ensure_list(archive.get("tags")),
                metadata_json=archive.get("metadata") if isinstance(archive.get("metadata"), dict) else {},
            )
            self.db.add(note)
            self.db.commit()
            self.db.refresh(note)
        else:
            print(f"[NoteArchive] 笔记已存在：run_id={source_run_id}, note_id={note.id}")

        self._mark_workflow_note_completed(user_id, source_run_id, note.id)
        print(f"[NoteArchive] 归档完成：run_id={source_run_id}, note_id={note.id}")
        return self._to_dict(note)

    def _mark_workflow_note_completed(self, user_id: int, run_id: str, note_id: int) -> None:
        """只回写追踪字段，不把完整笔记正文塞回工作流状态。"""
        run = WorkflowRunService.get_run_or_404(self.db, user_id, run_id)
        state = dict(run.state_json or {})
        state["note_id"] = note_id
        state["note_status"] = "completed"
        WorkflowRunService.update_run(self.db, run, run.status, state, run.error_message)

    @staticmethod
    def _to_dict(note: Note) -> dict[str, Any]:
        return {
            "id": note.id,
            "user_id": note.user_id,
            "session_id": note.session_id,
            "source_run_id": note.source_run_id,
            "note_type": note.note_type,
            "title": note.title,
            "content": note.content,
            "tags": note.tags or [],
            "metadata": note.metadata_json or {},
            "created_at": note.created_at,
            "updated_at": note.updated_at,
        }

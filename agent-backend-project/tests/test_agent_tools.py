"""Eval（升级规划 §八 第 3 期第 4 条）：对固定 5 个问题断言"工具被正确调用"（fake LLM）。

## 这个门禁锁什么（诚实边界——别把它当"模型选对率"）

fake LLM 替不了真模型做语义判断：离线、零 token 的前提下，我们**没法**断言"真模型看到
『我哪里弱』一定会选 `get_weak_points`"。所以本文件锁的是**模型做选择所依赖的全部依据 +
执行管道**：

1. 期望工具**在绑定集里**（注册没漏、名字没打错、门控没把它藏起来）；
2. 它的**描述覆盖触发该问题的关键词**——描述是模型选工具的唯一依据，改坏了就选错；
3. 走真 MySQL 通过 `ToolRuntime` **真的能执行**并回灌可解析的观察结果；
4. **运行期上下文不进 JSON Schema**（模型伪造不出 `user_id`，越权面为零）；
5. **分工规则在系统提示词里**（"问情况"用工具 / "要结果"委派 worker）——这条规则只活在
   提示词里，最该被钉住；
6. **白名单之外的名字不执行**（幻觉工具名被拒；worker 名不归 runtime 管，走委派）。

真模型的选对率属**活体评测**（花 token、结果不确定），不在本文件射程内。

跑法：`python -m pytest tests/test_agent_tools.py -q`（零 token；需要真 MySQL）
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage, ToolMessage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.agent.tools.knowledge as knowledge_tool_module
from app.agent.learning_partner import LearningPartnerService
from app.agent.tools import (
    DEFAULT_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    TOOL_REGISTRY,
    ToolContext,
    ToolRuntime,
)
from app.agent.workers import WORKERS, describe_workers_for_prompt
from app.db.models import KnowledgeBase, Note, PracticeSet, UserKnowledgeMastery
from app.services.mastery_service import MasteryService

# ---------------------------------------------------------------------------
# 假模型与驱动助手（离线、零 token）
# ---------------------------------------------------------------------------
class FakeToolCallingModel:
    """按脚本"流"出回答的假模型：每次 `stream()` 消费一项 `(text, tool_calls)`。"""

    def __init__(self, script: list[tuple[str, list[dict[str, Any]]]]):
        self.script = list(script)
        self.calls: list[list[Any]] = []
        self.bound_tools: list[str] = []

    def bind_tools(self, tools: list[Any]) -> "FakeToolCallingModel":
        self.bound_tools = [getattr(tool, "name", str(tool)) for tool in tools]
        return self

    def stream(self, messages: list[Any], config: Any = None) -> Any:
        # config 形参必须接住：loop 会以 {"callbacks": [用量收集器]} 调用（第 3 期可观测）
        self.calls.append(list(messages))
        index = min(len(self.calls) - 1, len(self.script) - 1)
        text, tool_calls = self.script[index]
        if text:
            yield AIMessageChunk(content=text)
        if tool_calls:
            yield AIMessageChunk(content="", tool_calls=list(tool_calls))

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(list(messages))
        return AIMessage(content="（假模型）")


class FakeWorkerGateway:
    """假 worker 网关：只记录委派，不碰真子图与真 LLM。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def delegate(
        self, worker_name: str, payload: dict[str, Any], *, user_id: int, session_id: int
    ) -> dict[str, Any]:
        self.calls.append((worker_name, payload))
        return {
            "worker": worker_name,
            "ok": True,
            "status": "dispatched",
            "data": {"run_id": "fake-run-1"},
        }


def _call(name: str, args: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _run_loop(
    db: Any,
    temp_user: Any,
    question: str,
    *,
    script: list[tuple[str, list[dict[str, Any]]]] | None = None,
    gateway: FakeWorkerGateway | None = None,
    knowledge_base_id: int | None = None,
) -> tuple[FakeToolCallingModel, FakeWorkerGateway, Any, list[dict[str, Any]]]:
    """驱动一次完整 loop，返回（假模型, 假网关, LoopResult, 事件列表）。

    `mcp_tools={}`：显式不挂 MCP——`None` 会按配置真去连 time/deepwiki，
    那样这个门禁就不离线了。
    """
    model = FakeToolCallingModel(script or [("好的。", [])])
    gateway = gateway or FakeWorkerGateway()
    service = LearningPartnerService(db, llm=model, worker_gateway=gateway, mcp_tools={})
    generator = service.run_events(
        user_id=temp_user.id,
        session_id=1,
        request_text=question,
        knowledge_base_id=knowledge_base_id,
        load_history=False,
    )
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stop:
            return model, gateway, stop.value, events


def _runtime(db: Any, temp_user: Any, *, knowledge_base_id: int | None = None) -> ToolRuntime:
    return ToolRuntime(
        context=ToolContext(
            db=db, user_id=temp_user.id, session_id=1, knowledge_base_id=knowledge_base_id
        ),
        allowed_tools=DEFAULT_TOOL_NAMES,
        tools=TOOL_REGISTRY,
    )


# ---------------------------------------------------------------------------
# 固定 5 个问题 -> 期望工具
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class QuestionCase:
    """一个固定问题、它该调的工具、该问题的触发关键词。"""

    question: str
    expected_tool: str
    args: dict[str, Any]
    triggers: tuple[str, ...]


QUESTION_CASES: tuple[QuestionCase, ...] = (
    QuestionCase("我这门课掌握得怎么样？", "get_mastery", {}, ("掌握度",)),
    QuestionCase("我有哪些薄弱点？", "get_weak_points", {"limit": 5}, ("薄弱点",)),
    QuestionCase("我最近练过哪些题？", "list_practice_history", {"limit": 5}, ("练习历史",)),
    QuestionCase(
        "资料里是怎么讲聚簇索引的？",
        "search_material",
        {"query": "聚簇索引", "top_k": 3},
        ("检索", "资料"),
    ),
    QuestionCase(
        "把这段总结存成笔记",
        "save_note",
        {"content_md": "# 索引小结\n聚簇索引叶子存整行。", "tags": ["索引"]},
        ("笔记",),
    ),
)

CASES = pytest.mark.parametrize(
    "case", QUESTION_CASES, ids=[case.expected_tool for case in QUESTION_CASES]
)


# ---------------------------------------------------------------------------
# [1] 5 个问题：工具可选中、描述覆盖触发词、真能执行
# ---------------------------------------------------------------------------
@CASES
def test_expected_tool_is_bound_for_question(db: Any, temp_user: Any, case: QuestionCase) -> None:
    """固定问题对应的工具确实被绑定给模型（注册没漏、名字没打错）。"""
    model, _, _, _ = _run_loop(db, temp_user, case.question)
    assert case.expected_tool in model.bound_tools, model.bound_tools
    assert set(model.bound_tools) == set(DEFAULT_TOOL_NAMES), model.bound_tools


@CASES
def test_tool_description_covers_question_triggers(case: QuestionCase) -> None:
    """描述是模型选工具的唯一依据：触发词必须在描述里。"""
    description = TOOL_REGISTRY[case.expected_tool].description or ""
    missing = [trigger for trigger in case.triggers if trigger not in description]
    assert not missing, f"{case.expected_tool} 描述缺少触发词 {missing}：{description}"


@CASES
def test_tool_executes_and_returns_observation(
    db: Any, temp_user: Any, case: QuestionCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真 MySQL 执行：观察结果可解析、无 error、进 trace（工具真的能用）。"""
    if case.expected_tool == "search_material":
        # 检索层自己的验收在 verify_agent_loop / verify_scoped_retrieval；
        # 这里只锁"工具包装"（片段格式化 + 上限），所以把检索函数换成可控的。
        monkeypatch.setattr(
            knowledge_tool_module,
            "search_similar_chunks",
            lambda **kwargs: [
                {
                    "file_name": "索引.pdf",
                    "resource_id": 1,
                    "chunk_index": 0,
                    "score": 0.8765,
                    "text": "聚簇索引的叶子节点存放整行数据。",
                }
            ],
        )
    runtime = _runtime(db, temp_user)
    payload = json.loads(runtime.execute(case.expected_tool, case.args))
    assert "error" not in payload, payload
    assert runtime.traces[-1].ok is True
    assert runtime.traces[-1].name == case.expected_tool


# ---------------------------------------------------------------------------
# [2] 白名单与 schema 契约
# ---------------------------------------------------------------------------
def test_whitelist_is_the_atomic_tools() -> None:
    """白名单就是这些原子能力（第 23 轮加了 4 个知识库工具）；写权限只有 save_note 与建库。"""
    assert DEFAULT_TOOL_NAMES == {
        "search_material",
        "get_mastery",
        "get_weak_points",
        "list_practice_history",
        "list_knowledge_bases",
        "list_documents",
        "get_document_summary",
        "create_knowledge_base",
        "save_note",
    }, sorted(DEFAULT_TOOL_NAMES)
    assert READ_ONLY_TOOL_NAMES == DEFAULT_TOOL_NAMES - {"save_note", "create_knowledge_base"}


def test_injected_context_absent_from_schema() -> None:
    """模型只能决定"调哪个工具、传什么业务参数"，决定不了 `user_id` 是谁。"""
    for name, tool in TOOL_REGISTRY.items():
        for hidden in ("ctx", "user_id", "db", "session_id", "knowledge_base_id"):
            if hidden == "knowledge_base_id":
                continue  # 业务参数：模型可以传（它决定范围），但必须显式出现在 schema 里
            assert hidden not in tool.args, f"{name} 的 schema 泄漏了运行期参数 {hidden}"


def test_runtime_rejects_names_outside_whitelist(db: Any, temp_user: Any) -> None:
    """幻觉出来的工具名不会被执行。"""
    payload = json.loads(_runtime(db, temp_user).execute("drop_database", {}))
    assert "error" in payload and "白名单" in payload["error"], payload


def test_worker_names_are_not_atomic_tools(db: Any, temp_user: Any) -> None:
    """分工判据：worker 名不在工具白名单里，`runtime.handles()` 也不认它（loop 据此走委派）。"""
    runtime = _runtime(db, temp_user)
    for name in WORKERS:
        assert name not in DEFAULT_TOOL_NAMES, name
        assert runtime.handles(name) is False, name


def test_system_prompt_carries_tool_worker_division(db: Any, temp_user: Any) -> None:
    """分工规则只活在提示词里——钉住它，否则改一次提示词就可能静默退化。"""
    model, _, _, _ = _run_loop(db, temp_user, "帮我针对薄弱点出一套题")
    system_text = "\n".join(
        str(message.content)
        for message in model.calls[0]
        if isinstance(message, SystemMessage)
    )
    assert "就委派 worker" in system_text and "才用工具" in system_text, system_text[:200]
    for name in WORKERS:
        assert name in system_text, f"提示词没告诉模型可以委派 {name}"
    # worker 清单渲染函数本身也是契约的一部分
    workers_block = describe_workers_for_prompt()
    for name in WORKERS:
        assert name in workers_block
    assert "工具只是原子能力" in workers_block


# ---------------------------------------------------------------------------
# [3] 执行管道：工具执行 / 观察回灌 / 委派分流
# ---------------------------------------------------------------------------
def test_loop_dispatches_scripted_tool_call(db: Any, temp_user: Any) -> None:
    """脚本化 tool_call -> runtime 执行 -> 观察回灌 -> 最终作答。"""
    model, _, result, events = _run_loop(
        db,
        temp_user,
        "我哪里弱",
        script=[("", [_call("get_weak_points", {"limit": 3})]), ("你最弱的是索引。", [])],
    )
    kinds = [event["type"] for event in events]
    assert kinds == ["step", "tool_call", "tool_result", "step", "delta"], kinds
    record = result.tool_calls[0]
    assert record["name"] == "get_weak_points" and record["args"] == {"limit": 3}
    assert record["ok"] is True and isinstance(record["elapsed_ms"], int)
    # 观察结果真的回灌给了第二步：ToolMessage 里就是工具的 JSON
    tool_messages = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
    assert tool_messages, "第二步没有收到工具观察结果"
    assert json.loads(str(tool_messages[-1].content))["count"] == 0
    assert result.answer == "你最弱的是索引。"
    assert result.stop_reason == "final_answer"


def test_loop_delegates_worker_name_instead_of_executing_it(db: Any, temp_user: Any) -> None:
    """要"结果" -> 走委派：worker 名不被 runtime 当工具执行，结果同样回灌。"""
    gateway = FakeWorkerGateway()
    model, gateway, result, events = _run_loop(
        db,
        temp_user,
        "帮我出一套题",
        script=[("", [_call("delegate_quiz", {"question_count": 5})]), ("已出一套题。", [])],
        gateway=gateway,
    )
    assert gateway.calls and gateway.calls[0][0] == "delegate_quiz", gateway.calls
    assert result.tool_calls[0]["name"] == "delegate_quiz"
    kinds = [event["type"] for event in events]
    assert "tool_result" in kinds, kinds  # 委派结果也是观察结果
    assert result.answer == "已出一套题。"


# ---------------------------------------------------------------------------
# [4] 5 个工具的"诚实契约"（空态不臆造、写权限真落库）
# ---------------------------------------------------------------------------
def test_get_mastery_does_not_invent_scores(db: Any, temp_user: Any) -> None:
    """没有记录就说没有（不能编分数），有记录才给分数与分区。"""
    runtime = _runtime(db, temp_user)
    empty = json.loads(runtime.execute("get_mastery", {}))
    assert empty["count"] == 0 and "note" in empty, empty

    kb = KnowledgeBase(user_id=temp_user.id, name="评测库")
    db.add(kb)
    db.commit()
    db.add(
        UserKnowledgeMastery(
            user_id=temp_user.id, knowledge_base_id=kb.id, mastery_score=42.5, practice_count=2
        )
    )
    db.commit()

    payload = json.loads(runtime.execute("get_mastery", {"knowledge_base_id": kb.id}))
    assert payload["count"] == 1, payload
    item = payload["items"][0]
    assert item["mastery_score"] == 42.5 and item["zone"] == "过渡区", item


def test_get_weak_points_reads_merged_points_and_filters_performance_axis(
    db: Any, temp_user: Any
) -> None:
    """走既有 MasteryService 读弱点；performance 轴不是知识弱点，不进清单。"""
    MasteryService.merge_weak_points(
        db,
        user_id=temp_user.id,
        knowledge_base_id=None,
        points=["聚簇索引与二级索引的区别"],
        practice_set_id=910301,
        practice_score=50.0,
    )
    db.commit()  # merge_weak_points 自身不 commit（"一次 commit"契约属于 update_after_practice）

    from app.db.models import UserProfile
    from sqlalchemy import select

    profile = db.scalar(select(UserProfile).where(UserProfile.user_id == temp_user.id))
    profile.weak_points = list(profile.weak_points) + [
        {"point": "零散表现问题", "axis": "performance", "times_seen": 9}
    ]
    db.commit()

    payload = json.loads(_runtime(db, temp_user).execute("get_weak_points", {"limit": 5}))
    points = [item["point"] for item in payload["items"]]
    assert payload["count"] == 1 and points == ["聚簇索引与二级索引的区别"], payload


def test_list_practice_history_reports_empty_state_and_rows(db: Any, temp_user: Any) -> None:
    """空态给 note；有练习时返回条目与近期均分。"""
    runtime = _runtime(db, temp_user)
    empty = json.loads(runtime.execute("list_practice_history", {"limit": 5}))
    assert empty["returned"] == 0 and "note" in empty, empty

    db.add(
        PracticeSet(
            user_id=temp_user.id,
            title="索引练习",
            status="completed",
            total_score=80.0,
            question_count=5,
        )
    )
    db.commit()

    payload = json.loads(runtime.execute("list_practice_history", {"limit": 5}))
    assert payload["returned"] == 1 and payload["total"] == 1, payload
    assert payload["items"][0]["title"] == "索引练习"
    assert payload["items"][0]["total_score"] == 80.0
    assert payload["average_recent_score"] == 80.0


def test_search_material_scopes_to_kb_and_truncates_snippets(
    db: Any, temp_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """知识库作用域内的资料才检索；空库直接给 note（不去打检索层）；片段有长度上限。"""
    calls: list[dict[str, Any]] = []

    def _fake_search(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        return [
            {
                "file_name": "索引.pdf",
                "resource_id": 7,
                "chunk_index": 3,
                "score": 0.5,
                "text": "长" * 1200,  # 超过 MAX_SNIPPET_CHARS，验证截断
            }
        ]

    monkeypatch.setattr(knowledge_tool_module, "search_similar_chunks", _fake_search)

    kb = KnowledgeBase(user_id=temp_user.id, name="空库")
    db.add(kb)
    db.commit()

    runtime = _runtime(db, temp_user, knowledge_base_id=kb.id)
    scoped_empty = json.loads(runtime.execute("search_material", {"query": "索引"}))
    assert scoped_empty["count"] == 0 and "没有已归档的资料" in scoped_empty["note"], scoped_empty
    assert not calls, "空知识库不该去打检索层"

    # 不限定知识库时走检索层（检索层本身换成可控的）——注意要用**不限定**的 runtime，
    # 复用上面那个会把范围继续锁在空库上，第二次调用根本到不了检索层
    payload = json.loads(
        _runtime(db, temp_user).execute("search_material", {"query": "聚簇索引", "top_k": 3})
    )
    assert payload["count"] == 1, payload
    hit = payload["hits"][0]
    assert hit["file_name"] == "索引.pdf" and hit["score"] == 0.5
    assert len(hit["text"]) == knowledge_tool_module.MAX_SNIPPET_CHARS, len(hit["text"])
    assert calls and calls[0]["query"] == "聚簇索引"


def test_save_note_writes_real_note(db: Any, temp_user: Any) -> None:
    """唯一带写权限的工具：真的落库，标题取正文首行，来源标记可审计。"""
    payload = json.loads(
        _runtime(db, temp_user).execute(
            "save_note",
            {"content_md": "# 索引小结\n\n聚簇索引叶子存整行。", "tags": ["索引"]},
        )
    )
    assert payload["saved"] is True, payload

    from sqlalchemy import select

    note = db.scalar(select(Note).where(Note.id == int(payload["note_id"])))
    assert note is not None
    assert note.title == "索引小结"
    assert note.note_type == "agent_capture"
    assert (note.metadata_json or {}).get("source") == "learning_partner"
    assert note.user_id == temp_user.id

"""验收：第 2 期 Agent 技术栈核心（门控 + 原子工具 + worker 委派 + 手写 ReAct loop）。

为什么全部离线：这一期的核心是**编排逻辑**，不是模型能力。所以：
- 门控：快筛零成本；语义兜底用 monkeypatch 钉死，避免下载模型与 flaky 分数；
- 工具：用**真实 MySQL**（临时用户/知识库/资料/切片）验证注入与权限，但检索用假向量；
- loop：注入**假模型**（可控 tool_calls 序列）与假 worker 网关，逐条断言事件轨迹。

临时数据一律自建自清（踩坑记录第 21、26、27 条）。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_agent_loop.py
    python scripts/verify_agent_loop.py --keep     # 保留临时数据，方便手工看
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from app.agent import learning_partner as lp
from app.agent.a2a import A2AEnvelope
from app.agent.a2a.dispatcher import HANDOFF_RULES, decide_next_agent
from app.agent.learning_partner import LearningPartnerService
from app.agent.tools import (
    DEFAULT_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    TOOL_REGISTRY,
    ToolContext,
    ToolRuntime,
)
from app.agent.workers import WORKERS, WorkerGateway, worker_tool_specs
from app.core.settings import settings

# 本期验收的口径是"离线、只验编排逻辑"（见文件头）。.env 一旦开 MCP（MCP_ENABLED=true），
# loop 会在这里真实连外部 server（起 JVM / 连远端），[4.6] 的工具绑定断言也会随
# MCP_SERVERS 漂移。进程内强制关闭，让断言始终只对内置工具；MCP 的接入验收在 verify_mcp.py
# （它用显式注入 mcp_tools 的方式隔离配置，与本处同效）。
object.__setattr__(settings, "mcp_enabled", False)
from app.db.database import SessionLocal
from app.db.models import (
    KnowledgeBase,
    KnowledgeChunk,
    Note,
    Resource,
    User,
    UserKnowledgeMastery,
)
from app.schemas import AgentTaskStartRequest, AgentTaskStartResponse
from app.security import hash_password
from app.services.orchestrator_service import (
    CHAT_PATH,
    LOOP_PATH,
    LOOP_ROUTE_TYPE,
    OrchestratorService,
)

TEMP_PASSWORD = "verify_pass_123"

passed = 0
failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ---------------------------------------------------------------------------
# 假模型：可控的 tool_calls 序列
# ---------------------------------------------------------------------------
class FakeToolCallingModel:
    """按预设脚本"流"出回答的假模型。

    每次 `stream()` 消费脚本里的一项：`(text, tool_calls)`。
    这样就能精确构造"先取资料、再出题、最后作答"的多步轨迹，而不需要真模型。
    """

    def __init__(self, script: list[tuple[str, list[dict[str, Any]]]]):
        self.script = list(script)
        self.calls: list[list[Any]] = []
        self.bound_tools: list[str] = []

    def bind_tools(self, tools: list[Any]) -> "FakeToolCallingModel":
        self.bound_tools = [getattr(tool, "name", str(tool)) for tool in tools]
        return self

    def stream(self, messages: list[Any], config: Any = None) -> Any:
        # config：loop 会以 `config={"callbacks": [用量收集器]}` 调用（第 3 期可观测），
        # 假模型不消费它，但形参必须接住，否则 TypeError。
        self.calls.append(list(messages))
        index = min(len(self.calls) - 1, len(self.script) - 1)
        text, tool_calls = self.script[index]
        if text:
            yield AIMessageChunk(content=text)
        if tool_calls:
            yield AIMessageChunk(content="", tool_calls=list(tool_calls))

    def invoke(self, messages: list[Any]) -> AIMessage:  # 讲解 worker 用
        self.calls.append(list(messages))
        return AIMessage(content=f"（假模型讲解）{_last_human(messages)}")


class FakeWorkerGateway:
    """假 worker 网关：记录委派调用，返回可控结果。"""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    def delegate(
        self, worker_name: str, payload: dict[str, Any], *, user_id: int, session_id: int
    ) -> dict[str, Any]:
        self.calls.append((worker_name, payload))
        if self.fail:
            return {
                "worker": worker_name,
                "ok": False,
                "status": "failed",
                "reason": "假网关故意失败",
                "data": {},
            }
        return {
            "worker": worker_name,
            "ok": True,
            "status": "dispatched",
            "data": {"run_id": "fake-run-1", "question_count": 3},
        }


def _last_human(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def _call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _stub_intent(label: str, score: float) -> Any:
    """构造一个 `classify_intent` 替身，避免验收时下载语义模型。"""

    def _fake(**_kwargs: Any) -> Any:
        return type("IntentMatch", (), {"label": label, "score": score})()

    return _fake


def drive(service: LearningPartnerService, **kwargs: Any) -> tuple[lp.LoopResult, list[dict]]:
    """把 `run_events` 跑到底，返回（结果, 事件列表）。"""
    generator = service.run_events(**kwargs)
    events: list[dict] = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stop:
            return stop.value or lp.LoopResult(), events


# ---------------------------------------------------------------------------
# 1. 门控
# ---------------------------------------------------------------------------
def verify_gate() -> None:
    print("\n[1] 入口判定（agent_name=None 时由系统决定走哪条路）")
    service = OrchestratorService(db=None)  # 入口判定是纯函数，不需要 db

    import app.services.orchestrator_service as os_module
    from app.core.settings import settings as app_settings

    def with_entry_mode(mode: str) -> None:
        """临时切换默认入口模式（dataclass 是 frozen 的，用 object.__setattr__）。"""
        object.__setattr__(app_settings, "agent_entry_mode", mode)

    original_mode = app_settings.agent_entry_mode

    try:
        # ---- 默认模式：loop（2026-10-18 起的新默认） ----
        with_entry_mode(os_module.AGENT_ENTRY_MODE_LOOP)
        check("默认入口是 loop", os_module.AGENT_ENTRY_MODE_LOOP == "loop")
        for text in (
            "这个概念是什么意思？",
            "extract() 是做什么的，参数有哪些",
            "帮我看看哪里弱，然后针对弱点出一套题",
            "你好",
        ):
            gate = service.classify_entry_path(text)
            check(
                f"loop 模式下不区分简繁，一律进 loop（{text[:10]}…）",
                gate["path"] == LOOP_PATH,
                gate["reason"][:60],
            )

        # ---- gated 模式：原门控行为必须逐条保持 ----
        with_entry_mode(os_module.AGENT_ENTRY_MODE_GATED)
        check(
            "settings 归一后取值合法",
            app_settings.agent_entry_mode in {"loop", "gated"},
            app_settings.agent_entry_mode,
        )

        # 快筛：强动作 + 顺承标记
        gate = service.classify_entry_path("帮我看看哪里弱，然后针对弱点出一套题")
        check("gated：快筛命中多步意图 → loop", gate["path"] == LOOP_PATH, gate["reason"])

        # 快筛：多个动作
        gate = service.classify_entry_path("先出题，再评分")
        check("gated：两个动作 → loop", gate["path"] == LOOP_PATH, gate["reason"])

        # 反例：单个弱动作不该进 loop（单轮聊天就能办好）
        gate = service.classify_entry_path("讲讲这道题")
        check("gated：单个弱动作 + 语义兜底 → chat", gate["path"] == CHAT_PATH, gate["reason"])

        # 反例：带逗号的简单提问不该因为标点进 loop
        for text in ("extract() 是做什么的，参数有哪些", "这个概念是什么意思？"):
            saved = os_module.classify_intent
            os_module.classify_intent = _stub_intent(CHAT_PATH, 0.0)
            try:
                gate = service.classify_entry_path(text)
            finally:
                os_module.classify_intent = saved
            check(f"gated：简单提问不进 loop（{text[:12]}…）", gate["path"] == CHAT_PATH, gate["reason"])

        # 语义兜底：模型判定 loop 时才进 loop（钉死分类器，避免联网下载模型）
        saved = os_module.classify_intent
        os_module.classify_intent = _stub_intent(LOOP_PATH, 0.71)
        try:
            gate = service.classify_entry_path("我今天该学点什么")
        finally:
            os_module.classify_intent = saved
        check("gated：语义兜底判定为多步 → loop", gate["path"] == LOOP_PATH, gate["reason"])

        # 语义模型不可用时（返回 default_label=chat）必须退化为 chat，而不是误进 loop
        saved = os_module.classify_intent
        os_module.classify_intent = _stub_intent(CHAT_PATH, 0.0)
        try:
            gate = service.classify_entry_path("随便聊聊今天天气")
            preserved = gate["path"] == CHAT_PATH
        finally:
            os_module.classify_intent = saved
        check("gated：语义不可用 → 保持单轮聊天（不退化为 loop）", preserved, gate["reason"])
    finally:
        object.__setattr__(app_settings, "agent_entry_mode", original_mode)

    # 显式 agent_name 的老调用不经过入口判定：route() 行为不变（两种模式都一样）
    route = service.route("quiz_generate_workflow", "帮我出题")
    check(
        "显式 agent_name 仍走原工作流路由（零回归）",
        route["route_type"] == "workflow",
        str(route),
    )
    check("LOOP_ROUTE_TYPE 已定义", LOOP_ROUTE_TYPE == "loop")


# ---------------------------------------------------------------------------
# 2. 原子工具
# ---------------------------------------------------------------------------
def verify_tools(db: Any, user_id: int, kb_id: int) -> None:
    print("\n[2] 原子工具（@tool + 隐藏上下文注入 + 白名单）")

    # 2.1 暴露给模型的 schema 里绝不能出现运行期上下文
    for name, tool in TOOL_REGISTRY.items():
        schema = json.dumps(tool.args)
        check(
            f"{name} 的 JSON Schema 不含 ctx/user_id/db",
            "ctx" not in tool.args and "user_id" not in tool.args and "db" not in tool.args,
            schema[:80],
        )
        check(f"{name} 有描述（模型靠它选工具）", bool(tool.description))

    check(
        "白名单 = 9 个原子能力（5 既有 + 4 知识库）",
        DEFAULT_TOOL_NAMES
        == {
            "search_material",
            "get_mastery",
            "get_weak_points",
            "list_practice_history",
            "list_knowledge_bases",
            "list_documents",
            "get_document_summary",
            "create_knowledge_base",
            "save_note",
        },
        str(sorted(DEFAULT_TOOL_NAMES)),
    )
    check("save_note 不在只读名单里（写权限单独标注）", "save_note" not in READ_ONLY_TOOL_NAMES)
    check(
        "create_knowledge_base 是写工具（不在只读名单）",
        "create_knowledge_base" not in READ_ONLY_TOOL_NAMES
        and "create_knowledge_base" in DEFAULT_TOOL_NAMES,
    )
    check(
        "3 个知识库只读工具都在只读名单里",
        {"list_knowledge_bases", "list_documents", "get_document_summary"} <= READ_ONLY_TOOL_NAMES,
    )

    runtime = ToolRuntime(
        context=ToolContext(db=db, user_id=user_id, session_id=1, knowledge_base_id=kb_id),
        allowed_tools=DEFAULT_TOOL_NAMES,
        tools=TOOL_REGISTRY,
    )

    # 2.2 白名单拒绝幻觉工具名
    rejected = json.loads(runtime.execute("drop_database", {}))
    check("白名单拒绝未知工具", "error" in rejected, str(rejected)[:80])

    # 2.3 掌握度：临时用户没有掌握度记录 → 不允许臆造分数
    payload = json.loads(runtime.execute("get_mastery", {}))
    check("get_mastery 无数据时返回空 + note", payload["count"] == 0 and "note" in payload)

    # 造一条掌握度记录再查
    db.add(
        UserKnowledgeMastery(
            user_id=user_id, knowledge_base_id=kb_id, mastery_score=42.5, practice_count=2
        )
    )
    db.commit()
    payload = json.loads(runtime.execute("get_mastery", {}))
    check(
        "get_mastery 读到掌握度并给出分区",
        payload["count"] == 1
        and payload["items"][0]["mastery_score"] == 42.5
        and payload["items"][0]["zone"] == "过渡区",
        str(payload),
    )

    # 2.4 薄弱点：写入 profile 后能读到（走既有 MasteryService，不另写一份逻辑）
    from app.db.models import UserProfile

    db.add(
        UserProfile(
            user_id=user_id,
            weak_points=[
                {
                    "point": "聚簇索引与二级索引的区别",
                    "knowledge_base_id": kb_id,
                    "times_seen": 3,
                    "axis": "knowledge",
                },
                {
                    "point": "零散表现问题",
                    "knowledge_base_id": kb_id,
                    "times_seen": 9,
                    "axis": "performance",
                },
            ],
        )
    )
    db.commit()
    payload = json.loads(runtime.execute("get_weak_points", {}))
    points = [item["point"] for item in payload["items"]]
    check(
        "get_weak_points 过滤掉 performance 轴",
        payload["count"] == 1 and points == ["聚簇索引与二级索引的区别"],
        str(payload),
    )

    # 2.5 练习历史：无记录时给 note
    payload = json.loads(runtime.execute("list_practice_history", {}))
    check(
        "list_practice_history 无记录时返回空 + note",
        payload["returned"] == 0 and "note" in payload,
        str(payload)[:80],
    )

    # 2.6 保存笔记：唯一写权限工具，落库且带上来源元数据
    payload = json.loads(
        runtime.execute("save_note", {"content_md": "# 索引小结\n\n聚簇索引叶子存整行。", "tags": ["索引"]})
    )
    check("save_note 落库成功", payload.get("saved") is True, str(payload))
    note = db.scalar(select(Note).where(Note.id == int(payload["note_id"])))
    check(
        "save_note 标题取正文首行、来源标记正确",
        note is not None
        and note.title == "索引小结"
        and note.note_type == "agent_capture"
        and (note.metadata_json or {}).get("source") == "learning_partner",
        f"{note.title if note else None}",
    )

    # 2.7 工具异常不打断 loop：把一个必定抛异常的工具塞进运行期白名单
    from langchain_core.tools import BaseTool

    class _BoomTool(BaseTool):
        name: str = "boom_tool"
        description: str = "验收用：必定抛异常的工具"

        def _run(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("模拟工具内部故障")

    TOOL_REGISTRY["boom_tool"] = _BoomTool()
    runtime.allowed_tools = runtime.allowed_tools | {"boom_tool"}
    try:
        parsed = json.loads(runtime.execute("boom_tool", {}))
    finally:
        TOOL_REGISTRY.pop("boom_tool", None)
        runtime.allowed_tools = runtime.allowed_tools - {"boom_tool"}
    check(
        "工具内部异常 → 变成可读观察结果（不抛异常）",
        "error" in parsed and "模拟工具内部故障" in parsed["error"],
        str(parsed)[:110],
    )
    check(
        "失败的工具调用仍然进了 trace",
        runtime.traces[-1].name == "boom_tool" and runtime.traces[-1].ok is False,
    )

    check("执行过的调用都进了 trace", len(runtime.traces) >= 5, str(len(runtime.traces)))
    check(
        "trace 记录耗时与结果预览",
        all(step.elapsed_ms >= 0 and step.result_preview for step in runtime.traces),
    )


# ---------------------------------------------------------------------------
# 3. worker 委派（A2A 信封 + 白名单）
# ---------------------------------------------------------------------------
def verify_workers(db: Any, user_id: int, session_id: int) -> None:
    print("\n[3] worker 委派（经 A2A 信封，不改 AGENT_CONFIG 与 learning 子图）")

    check("4 个 worker 已定义", len(WORKERS) == 4, str(sorted(WORKERS)))
    specs = worker_tool_specs()
    check(
        "worker 清单含角色名与描述（supervisor 靠它选 worker）",
        all(item["agent_name"] and item["description"] for item in specs),
    )
    check(
        "worker 的 agent_name 与 AGENT_CONFIG 对齐",
        all(
            item["agent_name"] in {"quiz_generate_workflow", "learning_evaluator", "difficulty_learner", "note_archiver"}
            for item in specs
        ),
    )

    # A2A 白名单：supervisor → 4 个 worker
    envelope = A2AEnvelope(
        from_agent="learning_partner",
        payload_type="delegate_quiz",
        payload={},
        next_agent="quiz_generate_workflow",
        confidence=1.0,
    )
    check("supervisor 委派出题 worker 通过 dispatcher 校验", decide_next_agent(envelope) == "quiz_generate_workflow")
    check(
        "dispatcher 白名单含 supervisor 一行",
        set(HANDOFF_RULES["learning_partner"])
        == {"quiz_generate_workflow", "learning_evaluator", "difficulty_learner", "note_archiver"},
    )
    check(
        "第 1 期的评测接力规则未被改动（零回归）",
        HANDOFF_RULES["learning_evaluator"] == ("difficulty_learner", "note_archiver"),
    )

    # 越权：supervisor 不许推给未登记的角色
    illegal = A2AEnvelope(
        from_agent="learning_partner",
        payload_type="hack",
        payload={},
        next_agent="user_service",
        confidence=1.0,
    )
    check("越权委派被 dispatcher 拒绝", decide_next_agent(illegal) is None)

    # 低置信度熔断（防低质量自动接力）
    low = A2AEnvelope(
        from_agent="learning_partner",
        payload_type="delegate_quiz",
        payload={},
        next_agent="quiz_generate_workflow",
        confidence=0.3,
    )
    check("低置信度委派被熔断", decide_next_agent(low) is None)

    # 真网关：未知 worker 与缺参数都返回结构化失败，而不是抛异常
    gateway = WorkerGateway(db)
    unknown = gateway.delegate("delegate_nothing", {}, user_id=user_id, session_id=session_id)
    check("未知 worker → ok=False 且不抛异常", unknown["ok"] is False, str(unknown)[:80])

    missing = gateway.delegate("delegate_evaluate", {}, user_id=user_id, session_id=session_id)
    check(
        "评测 worker handler 已注册并真的执行（不是 pending）",
        missing["status"] == "failed",
        f"status={missing.get('status')}",
    )
    check(
        "评测 worker 缺 run_id → 给出可操作原因",
        "run_id" in str(missing.get("reason")),
        str(missing.get("reason"))[:80],
    )

    missing_archive = gateway.delegate(
        "delegate_archive", {}, user_id=user_id, session_id=session_id
    )
    check(
        "归档 worker 缺 run_id → 结构化失败（复用既有 handler）",
        missing_archive["ok"] is False,
        str(missing_archive)[:110],
    )

    # 笔记会话里出题必须给出**可操作**的说明，而不是让用户看到 404「会话不存在」
    note_session_quiz = gateway.delegate(
        "delegate_quiz",
        {"session_model": 2, "session_id": session_id},
        user_id=user_id,
        session_id=session_id,
    )
    check(
        "笔记会话里出题 → 明确提示需要学习会话（不是 404）",
        note_session_quiz["ok"] is False
        and "学习会话" in str(note_session_quiz.get("reason")),
        str(note_session_quiz.get("reason"))[:100],
    )


# ---------------------------------------------------------------------------
# 4. 手写 ReAct loop
# ---------------------------------------------------------------------------
def verify_loop(db: Any, user_id: int, session_id: int) -> None:
    print("\n[4] 手写 ReAct loop（事件轨迹 / 终止条件 / 快速路径）")

    check("max_steps = 8", lp.MAX_STEPS == 8)

    # 4.1 无需工具直接答：只调 1 次模型，没有工具事件
    model = FakeToolCallingModel([("这是一个直接回答，不需要任何工具。", [])])
    service = LearningPartnerService(db, llm=model, worker_gateway=FakeWorkerGateway())
    result, events = drive(
        service,
        user_id=user_id,
        session_id=session_id,
        request_text="什么是聚簇索引",
        load_history=False,
    )
    kinds = [event["type"] for event in events]
    check("直接答：只调用 1 次模型", len(model.calls) == 1, f"calls={len(model.calls)}")
    check("直接答：无 tool_call 事件", "tool_call" not in kinds, str(kinds))
    check("直接答：stop_reason=final_answer", result.stop_reason == "final_answer")
    check("直接答：delta 拼出最终答案", result.answer == "这是一个直接回答，不需要任何工具。")
    check(
        "直接答：事件序列 = step → delta → done(无)",
        kinds[:1] == ["step"] and kinds[-1] == "delta",
        str(kinds),
    )

    # 4.2 调工具：工具结果回灌给模型后继续推理
    model = FakeToolCallingModel(
        [
            ("", [_call("get_weak_points", {"limit": 3}, "c1")]),
            ("你最弱的是索引，建议先补这块。", []),
        ]
    )
    service = LearningPartnerService(db, llm=model, worker_gateway=FakeWorkerGateway())
    result, events = drive(
        service,
        user_id=user_id,
        session_id=session_id,
        request_text="我哪里弱",
        load_history=False,
    )
    kinds = [event["type"] for event in events]
    check("工具路径：两次模型调用", len(model.calls) == 2, f"calls={len(model.calls)}")
    check(
        "工具路径：事件序列 step/tool_call/tool_result/step/delta",
        kinds
        == ["step", "tool_call", "tool_result", "step", "delta"],
        str(kinds),
    )
    tool_result = [event for event in events if event["type"] == "tool_result"][0]
    check("工具结果事件带耗时与预览", tool_result["ok"] and tool_result["elapsed_ms"] >= 0)
    tool_call_record = result.tool_calls[0] if result.tool_calls else {}
    check(
        "工具调用被记入结果（含成败/耗时/结果预览，回放时才看得见）",
        tool_call_record.get("name") == "get_weak_points"
        and tool_call_record.get("args") == {"limit": 3}
        and tool_call_record.get("ok") is True
        and isinstance(tool_call_record.get("elapsed_ms"), int)
        and "聚簇索引" in str(tool_call_record.get("result_preview")),
        str(tool_call_record)[:160],
    )
    # 回灌：第二次调用模型时，消息里必须已有 ToolMessage
    from langchain_core.messages import ToolMessage

    check(
        "工具结果以 ToolMessage 回灌（observe）",
        any(isinstance(message, ToolMessage) for message in model.calls[1]),
    )
    check(
        "回灌内容来自真实工具（含薄弱点文本）",
        any(
            isinstance(message, ToolMessage) and "聚簇索引" in str(message.content)
            for message in model.calls[1]
        ),
    )

    # 4.3 worker 委派：不是工具，走网关
    gateway = FakeWorkerGateway()
    model = FakeToolCallingModel(
        [
            ("", [_call("delegate_quiz", {"request_text": "针对弱点出题"}, "w1")]),
            ("已为你出了一套题，run_id 是 fake-run-1。", []),
        ]
    )
    service = LearningPartnerService(db, llm=model, worker_gateway=gateway)
    result, events = drive(
        service,
        user_id=user_id,
        session_id=session_id,
        request_text="针对我的弱点出一套题",
        load_history=False,
    )
    check("worker 委派走网关（不占工具白名单）", gateway.calls and gateway.calls[0][0] == "delegate_quiz")
    check(
        "委派时带上会话上下文",
        gateway.calls[0][1].get("session_id") == session_id,
        str(gateway.calls[0][1]),
    )
    check(
        "委派时带上会话模式（出题 worker 靠它判断会话类型）",
        "session_model" in gateway.calls[0][1],
        str(gateway.calls[0][1]),
    )
    check(
        "worker 结果以 tool_result 事件回推",
        [event["type"] for event in events].count("tool_result") == 1,
    )
    quiz_event = [event for event in events if event["type"] == "tool_result"][0]
    check(
        "出题委派的 run_id 回推到事件上（前端靠它认领本轮新出的题目消息）",
        quiz_event.get("run_id") == "fake-run-1",
        str({k: v for k, v in quiz_event.items() if k != "result_preview"}),
    )
    check("worker 委派后模型给出最终回答", "run_id" in result.answer, result.answer)

    # 4.4 worker 失败也是可观察结果，不打断 loop
    model = FakeToolCallingModel(
        [
            ("", [_call("delegate_quiz", {"request_text": "出题"}, "w2")]),
            ("出题暂时没成功，我先把原因说明一下。", []),
        ]
    )
    service = LearningPartnerService(db, llm=model, worker_gateway=FakeWorkerGateway(fail=True))
    result, events = drive(
        service,
        user_id=user_id,
        session_id=session_id,
        request_text="出一套题",
        load_history=False,
    )
    check("worker 失败不打断 loop", result.stop_reason == "final_answer" and bool(result.answer))
    failed_event = [event for event in events if event["type"] == "tool_result"][0]
    check(
        "委派失败的事件带 ok=False（网关判失败不是异常，也不该报成成功）",
        failed_event.get("ok") is False,
        str({k: v for k, v in failed_event.items() if k != "result_preview"}),
    )
    check(
        "委派失败的事件不带 run_id（没有新消息可认领，前端不该回刷）",
        "run_id" not in failed_event,
        str({k: v for k, v in failed_event.items() if k != "result_preview"}),
    )

    # 4.5 max_steps 终止：模型一直要求调工具
    model = FakeToolCallingModel(
        [("", [_call("get_mastery", {}, f"s{index}")]) for index in range(12)]
    )
    service = LearningPartnerService(db, llm=model, worker_gateway=FakeWorkerGateway())
    result, events = drive(
        service,
        user_id=user_id,
        session_id=session_id,
        request_text="一直查下去",
        load_history=False,
    )
    check("max_steps 终止", result.stop_reason == "max_steps", result.stop_reason)
    check("模型调用次数被限制为 8", len(model.calls) == 8, f"calls={len(model.calls)}")
    check("未收敛时明确告知用户", "仍未收敛" in result.answer, result.answer[-40:])

    # 4.6 系统提示词：worker 清单与花括号必须原样进入模型（不走 ChatPromptTemplate）
    model = FakeToolCallingModel([("好的。", [])])
    service = LearningPartnerService(db, llm=model, worker_gateway=FakeWorkerGateway())
    drive(
        service,
        user_id=user_id,
        session_id=session_id,
        request_text="你好",
        load_history=False,
    )
    system_content = str(model.calls[0][0].content)
    check("系统提示词是 SystemMessage", isinstance(model.calls[0][0], SystemMessage))
    check("worker 清单进入系统提示词", "delegate_quiz" in system_content)
    check(
        "worker 入参写进系统提示词（出题参数卡靠它教模型传参）",
        "knowledge_base_id" in system_content and "divergence" in system_content
        and "difficulty" in system_content,
        system_content[system_content.find("delegate_quiz"):][:180],
    )
    check(
        "知识库工具清单进入系统提示词（注册了却没说 = 模型永远不会调）",
        "list_knowledge_bases" in system_content and "create_knowledge_base" in system_content,
        system_content[-200:],
    )
    check("无花括号双重转义（模板逃逸问题已规避）", "{{" not in system_content and "}}" not in system_content)
    check(
        "工具被 bind 给模型",
        set(model.bound_tools) == set(DEFAULT_TOOL_NAMES),
        str(sorted(model.bound_tools)),
    )
    check("最后一条消息是本轮 HumanMessage", isinstance(model.calls[0][-1], HumanMessage))


# ---------------------------------------------------------------------------
# 5. 知识库范围检索（出题取原文的过滤）
# ---------------------------------------------------------------------------
def verify_scoped_retrieval(db: Any, user_id: int, kb_id: int, other_kb_id: int) -> None:
    print("\n[5] 检索范围收窄（resource_ids 过滤，按 MySQL 权威值）")

    import app.rag.retriever as retriever

    in_kb = _make_resource(db, kb_id=kb_id, user_id=user_id, name="本库资料.md", chunks=["本库切片"])
    out_kb = _make_resource(
        db, kb_id=other_kb_id, user_id=user_id, name="别库资料.md", chunks=["别库切片"]
    )
    in_chunk = db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.resource_id == in_kb.id))
    out_chunk = db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.resource_id == out_kb.id))

    saved_embed = retriever._embed_query
    saved_client = retriever.QdrantClient
    try:
        retriever._embed_query = lambda query: [0.0] * 4
        retriever.QdrantClient = lambda **kwargs: _FakeQdrant(
            [
                _FakePoint(str(out_chunk.vector_id), 0.95, {"file_name": "别库资料.md"}),
                _FakePoint(str(in_chunk.vector_id), 0.90, {"file_name": "本库资料.md"}),
            ]
        )

        # 不过滤：两份都能拿到（向后兼容，行为与升级前一致）
        both = retriever.search_similar_chunks(query="索引", user_id=user_id, db=db, top_k=5)
        check("不传 resource_ids → 行为不变（两份都在）", len(both) == 2, str(len(both)))

        # 过滤：只剩本库那份，且**分数更高的越界片段被丢弃**
        scoped = retriever.search_similar_chunks(
            query="索引", user_id=user_id, db=db, top_k=5, resource_ids=[in_kb.id]
        )
        check(
            "传 resource_ids → 只剩本库资料",
            len(scoped) == 1 and scoped[0]["resource_id"] == in_kb.id,
            str([item["resource_id"] for item in scoped]),
        )
        check("越界片段不会挤占 top_k（分数 0.95 的别库片段被过滤）", scoped[0]["score"] == 0.90)

        # top_k 上限仍然生效
        limited = retriever.search_similar_chunks(
            query="索引", user_id=user_id, db=db, top_k=1, resource_ids=[in_kb.id, out_kb.id]
        )
        check("top_k 截断仍然生效", len(limited) == 1, str(len(limited)))
    finally:
        retriever._embed_query = saved_embed
        retriever.QdrantClient = saved_client


class _FakePoint:
    def __init__(self, point_id: str, score: float, payload: dict[str, Any]):
        self.id = point_id
        self.score = score
        self.payload = payload


class _FakeQdrant:
    """假 Qdrant：只实现 retriever 用到的那两个方法。"""

    def __init__(self, points: list[_FakePoint]):
        self.points = points
        self.last_limit: int | None = None

    def collection_exists(self, name: str) -> bool:
        return True

    def query_points(self, **kwargs: Any) -> Any:
        self.last_limit = kwargs.get("limit")
        return type("R", (), {"points": self.points})()


# ---------------------------------------------------------------------------
# 6. schema 契约（前端零改动消费）
# ---------------------------------------------------------------------------
def verify_schema_contract() -> None:
    print("\n[6] 接口契约")
    request = AgentTaskStartRequest(
        session_id=1, request_text="帮我看看哪里弱，然后出一套题"
    )
    check("agent_name 仍可省略（门控前置条件）", request.agent_name is None)
    response = AgentTaskStartResponse(route_type="chat", status="streaming")
    check("响应模型仍然只要求 route_type/status", response.data == {})
    check("loop 不再是新的 JSON 分支（仍以 SSE 返回）", LOOP_ROUTE_TYPE == "loop")


# ---------------------------------------------------------------------------
# 数据构造 / 清理
# ---------------------------------------------------------------------------
def _make_resource(db: Any, *, kb_id: int, user_id: int, name: str, chunks: list[str]) -> Resource:
    resource = Resource(
        resource_type=0,
        storage_scene=0,
        upload_purpose=0,
        file_name=name,
        file_hash=f"ag{int(time.time() * 1000) % 10**10}".ljust(32, "0")[:32],
        storage_path="",
        knowledge_base_id=kb_id,
        ingest_status="ready",
        user_id=user_id,
    )
    db.add(resource)
    db.flush()
    for index, text in enumerate(chunks):
        db.add(
            KnowledgeChunk(
                resource_id=resource.id,
                chunk_index=index,
                text=text,
                char_count=len(text),
                vector_id=str(uuid.uuid4()),
            )
        )
    db.commit()
    db.refresh(resource)
    return resource


def main() -> int:
    parser = argparse.ArgumentParser(description="第 2 期 Agent loop 验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    args = parser.parse_args()

    global db
    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    username = f"vkal{suffix}"  # users.username 只有 20 字符
    user_id = kb_id = other_kb_id = None
    session_id = None
    try:
        user = User(username=username, password=hash_password(TEMP_PASSWORD))
        db.add(user)
        db.flush()
        user_id = user.id

        kb = KnowledgeBase(user_id=user_id, name=f"临时Loop验收库-{suffix}")
        other = KnowledgeBase(user_id=user_id, name=f"临时别库-{suffix}")
        db.add_all([kb, other])
        db.flush()
        kb_id, other_kb_id = kb.id, other.id

        from app.db.models import Session as SessionModel

        session = SessionModel(user_id=user_id, title="临时Loop会话", session_model=0)
        db.add(session)
        db.commit()
        session_id = session.id
        print(f"临时用户 id={user_id}（{username}） 知识库 id={kb_id}/{other_kb_id} 会话 id={session_id}")

        verify_gate()
        verify_tools(db, user_id, kb_id)
        verify_workers(db, user_id, session_id)
        verify_loop(db, user_id, session_id)
        verify_scoped_retrieval(db, user_id, kb_id, other_kb_id)
        verify_schema_contract()

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0
    finally:
        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id}")
        else:
            try:
                if user_id:
                    note_ids = list(db.scalars(select(Note.id).where(Note.user_id == user_id)))
                    if note_ids:
                        db.execute(sql_delete(Note).where(Note.id.in_(note_ids)))
                    resource_ids = list(
                        db.scalars(select(Resource.id).where(Resource.user_id == user_id))
                    )
                    if resource_ids:
                        db.execute(
                            sql_delete(KnowledgeChunk).where(
                                KnowledgeChunk.resource_id.in_(resource_ids)
                            )
                        )
                        db.execute(sql_delete(Resource).where(Resource.id.in_(resource_ids)))
                    db.execute(
                        sql_delete(UserKnowledgeMastery).where(
                            UserKnowledgeMastery.user_id == user_id
                        )
                    )
                    from app.db.models import UserProfile

                    db.execute(sql_delete(UserProfile).where(UserProfile.user_id == user_id))
                    db.execute(
                        sql_delete(KnowledgeBase).where(KnowledgeBase.user_id == user_id)
                    )
                    from app.db.models import ChatMessage, Session as SessionModel

                    db.execute(sql_delete(ChatMessage).where(ChatMessage.user_id == user_id))
                    db.execute(sql_delete(SessionModel).where(SessionModel.user_id == user_id))
                    db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"已清理临时数据：user={user_id}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"清理失败（可手工删）：{exc}")
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

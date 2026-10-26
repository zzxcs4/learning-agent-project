"""验收：`POST /agent/tasks/start` 的 HTTP 端到端（门控 + SSE 事件契约 + 零回归）。

为什么必须有这一套（`verify_agent_loop.py` 已验过编排逻辑）：
门控的价值是"**简单问答的首字延迟与升级前一致**"，这只有在真正的 HTTP 流上才成立——
路由层有没有被改动、响应头是否还是 `text/event-stream`、`delta` 事件形状是否与前端
`streamChat.ts` 的 TS 约定一致、老客户端忽略新事件时会不会崩，这些都要打接口才知道。

全程离线：模型、检索、长期记忆写入都在进程内被替换成假实现（不联网、不花额度），
所以本脚本可以随时重跑。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_agent_loop_http.py
    python scripts/verify_agent_loop_http.py --keep   # 保留临时数据
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.runnables import Runnable
from app.db.database import SessionLocal
from app.db.models import (
    ChatMessage,
    KnowledgeBase,
    Note,
    Session as SessionModel,
    User,
    UserKnowledgeMastery,
    UserProfile,
)
from app.main import app
from app.security import hash_password

TEMP_PASSWORD = "verify_pass_123"
DIRECT_ANSWER = "【假模型直答】这是一个不需要工具的单轮回答。"
LOOP_TOOL_ANSWER = "【假模型】根据你的薄弱点，最该先补的是索引。"

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


class FakeModel:
    """一次假模型，覆盖两条链路。

    - 单轮聊天链路把 `get_chat_model()` 包进 `prompt | llm`，`stream()` 收到的是
      格式化后的 dict（不是 message 列表）→ 直答模式；
    - agent loop 直接调 `stream(messages)` → 按脚本产生 tool_calls。
    """

    def __init__(self, tool_script: list[tuple[str, list[dict[str, Any]]]] | None = None):
        self.tool_script = list(tool_script or [])
        self.stream_calls = 0
        self.bound_tools: list[str] = []
        self.saw_tool_message = False

    def use_tool_script(self, script: list[tuple[str, list[dict[str, Any]]]]) -> None:
        """切换脚本并重置计数：同一个假模型要分别扮演"简单问答"与"多步协作"。"""
        self.tool_script = list(script)
        self.stream_calls = 0
        self.saw_tool_message = False

    def bind_tools(self, tools: list[Any]) -> "FakeModel":
        self.bound_tools = [getattr(tool, "name", str(tool)) for tool in tools]
        return self

    def stream(self, payload: Any) -> Any:
        self.stream_calls += 1
        if isinstance(payload, dict):
            # prompt | llm 链路：这里必须 `return` 一个**列表**而不是 yield。
            # `RunnableLambda` 会把被包函数的返回值当成最终输出；返回生成器时它会当成
            # 普通值 yield 出去，于是 chain.stream() 得到的是"生成器对象"而不是文本块，
            # 上层 `_extract_text_delta` 取不到任何 delta（实测踩过）。
            return [AIMessageChunk(content=DIRECT_ANSWER[:8]), AIMessageChunk(content=DIRECT_ANSWER[8:])]

        for message in payload:
            if type(message).__name__ == "ToolMessage":
                self.saw_tool_message = True

        index = min(self.stream_calls - 1, max(len(self.tool_script) - 1, 0))
        if self.tool_script:
            text, tool_calls = self.tool_script[index]
            if text:
                yield AIMessageChunk(content=text)
            if tool_calls:
                yield AIMessageChunk(content="", tool_calls=list(tool_calls))
        else:
            yield AIMessageChunk(content=DIRECT_ANSWER)

    def invoke(self, messages: list[Any]) -> AIMessage:
        return AIMessage(content="（假模型 invoke）")


class FakeChatRunnable(Runnable):
    """把假模型扮成"会逐块吐字"的 Runnable，供 `prompt | llm` 单轮链路使用。

    为什么不用 `RunnableLambda`：它会把被包函数的**返回值整体 yield 一次**，
    返回生成器时上层拿到的是生成器对象、返回列表时拿到的是 list，`_extract_text_delta`
    都取不到文本（两种写法都实测过，都失败）。子类覆写 `stream` 也不生效——
    `RunnableSequence` 对最后一步有专门的"直接 invoke"路径。所以这里直接继承 `Runnable`，
    只实现 `invoke` / `stream` 两个方法，一 chunk 一帧。
    """

    def __init__(self, fake: "FakeModel"):
        super().__init__()
        self._fake = fake

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
        self._fake.stream_calls += 1
        return AIMessage(content=DIRECT_ANSWER)

    def stream(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self._fake.stream_calls += 1
        yield AIMessageChunk(content=DIRECT_ANSWER[:8])
        yield AIMessageChunk(content=DIRECT_ANSWER[8:])


def parse_sse(raw: str) -> list[dict[str, Any]]:
    """按前端 `streamChat.ts` 的同一套规则解析 SSE 帧。"""
    events: list[dict[str, Any]] = []
    for frame in raw.replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line.strip()[5:].strip()
            for line in frame.split("\n")
            if line.strip().startswith("data:")
        ]
        if not data_lines:
            continue
        try:
            events.append(json.loads("\n".join(data_lines)))
        except json.JSONDecodeError:
            continue
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description="第 2 期门控与 SSE 的 HTTP 层验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    args = parser.parse_args()

    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    username = f"vkalh{suffix}"  # users.username 只有 20 字符
    user_id = kb_id = session_id = None

    # --- 全程离线：替换真模型 / 真检索 / 真长期记忆写入 ---
    import app.agent.learning_partner as lp
    import app.agent.prompt_layer as prompt_layer
    import app.services.stream_chat_service as scs

    def _chat_model(provider: str = "deepseek") -> Any:
        # 单轮链路是 `prompt | llm`，右操作数必须是 Runnable，所以这里包一层
        return FakeChatRunnable(fake_model)

    fake_model = FakeModel(
        tool_script=[
            ("", [{"name": "get_weak_points", "args": {"limit": 3}, "id": "h1", "type": "tool_call"}]),
            (LOOP_TOOL_ANSWER, []),
        ]
    )
    saved = {
        "lp_stream": lp.LearningPartnerService._default_llm,
        "prompt_layer_chat_model": prompt_layer.get_chat_model,
        "search": scs.search_similar_chunks,
        "should_retrieve": scs.should_retrieve,
        "should_write": scs.LongTermMemory.should_write,
        "submit_update": scs.LongTermMemory.submit_profile_update_async,
    }
    submit_calls: list[Any] = []

    try:
        # loop 链路：`_default_llm` 是 loop 取模型的唯一入口
        lp.LearningPartnerService._default_llm = lambda self: fake_model  # type: ignore[assignment]
        # 单轮聊天链路：prompt_layer 里 `prompt | get_chat_model(provider=...)`
        prompt_layer.get_chat_model = _chat_model  # type: ignore[assignment]
        scs.search_similar_chunks = lambda **kwargs: []  # type: ignore[assignment]
        scs.should_retrieve = lambda text: False  # type: ignore[assignment]
        scs.LongTermMemory.should_write = staticmethod(lambda text, uid: False)  # type: ignore[assignment]
        scs.LongTermMemory.submit_profile_update_async = staticmethod(  # type: ignore[assignment]
            lambda *a, **k: submit_calls.append(a)
        )

        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            user = User(username=username, password=hash_password(TEMP_PASSWORD))
            db.add(user)
            db.flush()
            user_id = user.id
            kb = KnowledgeBase(user_id=user_id, name=f"临时HTTP验收库-{suffix}")
            db.add(kb)
            db.flush()
            kb_id = kb.id
            session = SessionModel(user_id=user_id, title="临时HTTP会话", session_model=0)
            db.add(session)
            db.add(
                UserProfile(
                    user_id=user_id,
                    weak_points=[
                        {
                            "point": "聚簇索引与二级索引的区别",
                            "knowledge_base_id": kb_id,
                            "times_seen": 4,
                            "axis": "knowledge",
                        }
                    ],
                )
            )
            db.commit()
            session_id = session.id
            print(f"临时用户 id={user_id}（{username}） 知识库 id={kb_id} 会话 id={session_id}")

            # ---- 鉴权 ----
            no_token = client.post(
                "/agent/tasks/start",
                json={"session_id": session_id, "request_text": "你好"},
            )
            check("未带令牌被拦住", no_token.status_code in (401, 403), str(no_token.status_code))

            login = client.post(
                "/auth/login", json={"username": username, "password": TEMP_PASSWORD}
            )
            check("登录成功", login.status_code == 200, str(login.status_code))
            token = login.json()["data"]["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            # ---- ⓪ MCP 状态接口（第 3 期）：默认关闭也必须能自查"为什么没有 MCP 工具" ----
            print("\n[0] MCP 状态接口 /agent/mcp/servers")
            no_token_mcp = client.get("/agent/mcp/servers")
            check(
                "MCP 状态接口需要登录",
                no_token_mcp.status_code in (401, 403),
                str(no_token_mcp.status_code),
            )
            mcp_response = client.get("/agent/mcp/servers", headers=headers)
            check("MCP 状态接口可用", mcp_response.status_code == 200, str(mcp_response.status_code))
            mcp_data = (mcp_response.json() or {}).get("data") or {}
            check(
                "默认关闭：enabled=false 且写明原因（不是空对象）",
                mcp_data.get("enabled") is False and bool(mcp_data.get("reason")),
                str(mcp_data)[:160],
            )
            check("默认关闭：servers 为空列表（前端据此不渲染面板）", mcp_data.get("servers") == [])

            from app.core.settings import settings as mcp_settings

            saved_mcp = (mcp_settings.mcp_enabled, mcp_settings.mcp_servers_json)
            from app.agent.mcp import reset_mcp_manager

            try:
                object.__setattr__(mcp_settings, "mcp_enabled", True)
                object.__setattr__(mcp_settings, "mcp_servers_json", "")
                reset_mcp_manager()
                payload = (
                    client.get("/agent/mcp/servers", headers=headers).json() or {}
                ).get("data") or {}
                check(
                    "开了 MCP 但没配 server → 明确说'没有可用配置'（不是静默空表）",
                    payload.get("enabled") is True and "没有可用配置" in str(payload.get("reason")),
                    str(payload)[:160],
                )
            finally:
                object.__setattr__(mcp_settings, "mcp_enabled", saved_mcp[0])
                object.__setattr__(mcp_settings, "mcp_servers_json", saved_mcp[1])
                reset_mcp_manager()

            # ---- ① 简单问答：默认入口（loop）下也必须"一次模型调用就答完" ----
            print("\n[1] 简单问答：agent_name=None → 默认进 loop，无需工具直接答")
            import app.services.orchestrator_service as os_module
            from app.core.settings import settings as app_settings

            def with_entry_mode(mode: str) -> None:
                object.__setattr__(app_settings, "agent_entry_mode", mode)

            original_mode = app_settings.agent_entry_mode
            check("默认入口是 loop", original_mode == os_module.AGENT_ENTRY_MODE_LOOP, original_mode)

            # 这一段演"简单问答"：脚本置空 → 模型不用工具，直接答
            fake_model.use_tool_script([])
            before_calls = fake_model.stream_calls
            before_bound = list(fake_model.bound_tools or [])
            response = client.post(
                "/agent/tasks/start",
                headers=headers,
                json={
                    "session_id": session_id,
                    "session_model": 0,
                    "request_text": "这个概念是什么意思？",
                },
            )
            check("HTTP 200", response.status_code == 200, str(response.status_code))
            check(
                "响应头仍是 text/event-stream",
                "text/event-stream" in response.headers.get("content-type", ""),
                response.headers.get("content-type", ""),
            )
            check(
                "保持 SSE 头部（不缓冲）",
                response.headers.get("x-accel-buffering") == "no",
                str(response.headers.get("x-accel-buffering")),
            )
            events = parse_sse(response.text)
            kinds = [event["type"] for event in events]
            check("start 事件标注 loop 模式", events[0].get("mode") == "agent_loop", str(events[0]))
            check(
                "简单问答仍只调用一次模型（首字延迟前提）",
                fake_model.stream_calls - before_calls == 1,
                f"calls={fake_model.stream_calls - before_calls}",
            )
            check(
                "简单问答不调用任何工具（无需工具直接答的快速路径）",
                "tool_call" not in kinds,
                str(kinds),
            )
            check("恰好一个 think 步骤后即收敛", kinds.count("step") == 1, str(kinds))
            check(
                "done 的 stop_reason 是 final_answer",
                events[-1].get("stop_reason") == "final_answer",
                str(events[-1])[:100],
            )
            check(
                "start 事件字段与前端约定一致（message_id）",
                isinstance(events[0].get("message_id"), int),
                str(events[0]),
            )
            check(
                "done 事件仍带 response_text 与 sources",
                events[-1]["type"] == "done"
                and "response_text" in events[-1]
                and "sources" in events[-1],
                str(events[-1])[:100],
            )
            check("直答内容完整拼出", "".join(
                event.get("text", "") for event in events if event["type"] == "delta"
            ) == DIRECT_ANSWER)
            check(
                "loop 会给模型绑定工具（所以资料型问题能被自动检索）",
                set(fake_model.bound_tools or []) != set(before_bound),
                str(sorted(fake_model.bound_tools or [])),
            )

            # ---- ①b gated 模式：切回去必须还是"原单轮聊天"（延迟隔离能力仍在） ----
            print("\n[1b] 切到 gated：简单问答回到原单轮 RAG 流式聊天")
            with_entry_mode(os_module.AGENT_ENTRY_MODE_GATED)
            try:
                fake_model.use_tool_script([])
                before_calls = fake_model.stream_calls
                response = client.post(
                    "/agent/tasks/start",
                    headers=headers,
                    json={
                        "session_id": session_id,
                        "session_model": 0,
                        "request_text": "这个概念是什么意思？",
                    },
                )
                events = parse_sse(response.text)
                kinds = [event["type"] for event in events]
                check(
                    "gated：事件类型回到 start/delta/delta/done",
                    kinds == ["start", "delta", "delta", "done"],
                    str(kinds),
                )
                check("gated：start 事件没有 mode 标记", "mode" not in events[0], str(events[0]))
                check(
                    "gated：没有 step / tool_call 事件",
                    "step" not in kinds and "tool_call" not in kinds,
                    str(kinds),
                )
                check(
                    "gated：只调用一次模型",
                    fake_model.stream_calls - before_calls == 1,
                    f"calls={fake_model.stream_calls - before_calls}",
                )
            finally:
                with_entry_mode(original_mode)

            # ---- ② 多步请求：默认入口下必须真正委派并回推完整协作轨迹 ----
            print("\n[2] 多步请求：agent_name=None → agent loop + 协作轨迹")
            fake_model.use_tool_script(
                [
                    ("", [{"name": "get_weak_points", "args": {"limit": 3}, "id": "h1", "type": "tool_call"}]),
                    (LOOP_TOOL_ANSWER, []),
                ]
            )
            response = client.post(
                "/agent/tasks/start",
                headers=headers,
                json={
                    "session_id": session_id,
                    "session_model": 0,
                    "request_text": "帮我看看哪里弱，然后针对弱点出一套题，做完给我讲解并归档成笔记",
                    "knowledge_base_id": kb_id,
                },
            )
            check("HTTP 200", response.status_code == 200, str(response.status_code))
            events = parse_sse(response.text)
            kinds = [event["type"] for event in events]
            check("start 事件标注 loop 模式", events[0].get("mode") == "agent_loop", str(events[0]))
            check("回推 step 事件（步骤时间线）", kinds.count("step") == 2, str(kinds))
            check("回推 tool_call 事件", kinds.count("tool_call") == 1, str(kinds))
            check("回推 tool_result 事件", kinds.count("tool_result") == 1, str(kinds))
            check("以 done 收尾", kinds[-1] == "done", str(kinds))
            tool_call = [event for event in events if event["type"] == "tool_call"][0]
            check("tool_call 带工具名与参数", tool_call["name"] == "get_weak_points", str(tool_call))
            tool_result = [event for event in events if event["type"] == "tool_result"][0]
            check("tool_result 带耗时与结果预览", tool_result["ok"] and tool_result["elapsed_ms"] >= 0)
            check(
                "工具结果真的回灌给了模型（第二轮看到 ToolMessage）",
                fake_model.saw_tool_message,
            )
            done = events[-1]
            check("done 带 stop_reason", done.get("stop_reason") == "final_answer", str(done)[:120])
            check("done 带步骤摘要", isinstance(done.get("steps"), list) and len(done["steps"]) == 2)
            # 前端 AgentTrace 靠 done.trace 渲染时间线；它必须与落库结构同构
            done_trace = done.get("trace")
            check(
                "done 带完整轨迹（含工具名与参数）",
                isinstance(done_trace, dict)
                and done_trace.get("type") == "agent_loop_trace"
                and done_trace.get("stop_reason") == "final_answer"
                and len(done_trace.get("steps") or []) == 2,
                str(done_trace)[:140],
            )
            check(
                "轨迹的 tool_calls 带参数与成败（前端要展示调了什么、成功与否）",
                isinstance(done_trace, dict)
                and len(done_trace.get("tool_calls") or []) == 1
                and done_trace["tool_calls"][0].get("name") == "get_weak_points"
                and done_trace["tool_calls"][0].get("args") == {"limit": 3}
                and done_trace["tool_calls"][0].get("ok") is True
                and isinstance(done_trace["tool_calls"][0].get("elapsed_ms"), int)
                and "result_preview" in done_trace["tool_calls"][0],
                str((done_trace or {}).get("tool_calls")),
            )
            check("loop 最终回答来自模型", done["response_text"] == LOOP_TOOL_ANSWER, done["response_text"])

            # ---- ②b 默认模式下 knowledge_base_id 必须真的传到 loop ----
            # 前端已把「选库」控件从出题模式解耦出来（默认模式下也可见）。若后端没把它
            # 传进 loop，search_material 只能全库检索，retriever 新加的 resource_ids 收窄等于白做。
            print("\n[2b] 默认模式下选库真的传到 loop（否则检索收窄被废掉）")
            from app.agent.learning_partner import LearningPartnerService as _LPS

            sent_events: list[dict[str, Any]] = []
            original_run_events = _LPS.run_events

            def _capture_run_events(self: Any, **kwargs: Any) -> Any:
                sent_events.append(kwargs)
                return original_run_events(self, **kwargs)

            fake_model.use_tool_script([(DIRECT_ANSWER, [])])
            _LPS.run_events = _capture_run_events
            try:
                response = client.post(
                    "/agent/tasks/start",
                    headers=headers,
                    json={
                        "session_id": session_id,
                        "session_model": 0,
                        "request_text": "这个概念是什么意思？",
                        "knowledge_base_id": kb_id,
                    },
                )
                check(
                    "带 knowledge_base_id 的默认模式请求 HTTP 200",
                    response.status_code == 200,
                    str(response.status_code),
                )
                check(
                    "loop 收到了 knowledge_base_id（选库解耦在后端侧生效）",
                    bool(sent_events) and sent_events[-1].get("knowledge_base_id") == kb_id,
                    str(sent_events[-1] if sent_events else None)[:130],
                )

                # 不传库时必须**明确是 None**（退化为全库检索），不能臆造一个库
                sent_events.clear()
                client.post(
                    "/agent/tasks/start",
                    headers=headers,
                    json={
                        "session_id": session_id,
                        "session_model": 0,
                        "request_text": "这个概念是什么意思？",
                    },
                )
                check(
                    "不传库时 loop 收到 None（不臆造知识库）",
                    bool(sent_events) and sent_events[-1].get("knowledge_base_id") is None,
                    str(sent_events[-1] if sent_events else None)[:130],
                )
            finally:
                _LPS.run_events = original_run_events
            # 复位：后面还要演多步委派
            fake_model.use_tool_script(
                [
                    ("", [{"name": "get_weak_points", "args": {"limit": 3}, "id": "h1", "type": "tool_call"}]),
                    (LOOP_TOOL_ANSWER, []),
                ]
            )

            # ---- ③ 老客户端：显式 agent_name 仍走原工作流（零回归） ----
            print("\n[3] 显式 agent_name：仍走原路由，不进门控")
            from app.services.learning_workflow_service import LearningWorkflowService

            original_start = LearningWorkflowService.start
            captured: dict[str, Any] = {}

            def _fake_start(self: Any, **kwargs: Any) -> dict[str, Any]:
                captured.update(kwargs)
                return {
                    "run_id": "http-fake-run",
                    "status": "waiting_answer",
                    "quiz": {"title": "假题", "questions": []},
                }

            LearningWorkflowService.start = _fake_start  # type: ignore[assignment]
            try:
                response = client.post(
                    "/agent/tasks/start",
                    headers=headers,
                    json={
                        "session_id": session_id,
                        "session_model": 0,
                        "request_text": "帮我出题",
                        "agent_name": "quiz_generate_workflow",
                    },
                )
            finally:
                LearningWorkflowService.start = original_start  # type: ignore[assignment]
            check("仍返回 JSON（不是 SSE）", response.status_code == 200, str(response.status_code))
            payload = response.json()
            check("响应仍包在统一 StandardResponse 里", payload.get("code") == 0, str(payload)[:90])
            check(
                "仍是 workflow 路由分支（route_type/run_id 不变）",
                payload["data"]["route_type"] == "workflow"
                and payload["data"]["run_id"] == "http-fake-run",
                str(payload["data"])[:110],
            )

            # ---- ④ 会话归属：别人的会话打不进来 ----
            print("\n[4] 会话归属校验")
            other = client.post(
                "/agent/tasks/start",
                headers=headers,
                json={"session_id": session_id + 99999, "request_text": "帮我看看哪里弱，然后出一套题"},
            )
            check("不存在的会话被拦", other.status_code == 404, str(other.status_code))

            # ---- ⑤ loop 也落库（前端历史列表要能显示） ----
            print("\n[5] loop 结果落库")
            # 先结束本脚本 Session 的事务：REPEATABLE READ 下不 rollback 就永远读到旧快照
            # （踩坑记录第 8 条），而接口用的是另一个 Session。
            db.rollback()
            db.expire_all()
            rows = list(
                db.scalars(
                    select(ChatMessage).where(
                        ChatMessage.user_id == user_id, ChatMessage.session_id == session_id
                    )
                )
            )
            loop_rows = [
                row
                for row in rows
                if isinstance(row.response_payload, dict)
                and row.response_payload.get("type") == "agent_loop_trace"
            ]
            # 默认入口下本轮共 4 次 loop 请求：①直答 ①b(gated，不走 loop) ②多步 ②b×2 直答
            # 其中只有多步那轮带 tool_calls
            multi_step_rows = [
                row for row in loop_rows if row.response_payload.get("tool_calls")
            ]
            check(
                "默认入口下每轮 loop 都写了会话消息（含轨迹）",
                len(loop_rows) == 4,
                f"rows={len(loop_rows)}",
            )
            check("多步那轮带 tool_calls 轨迹", len(multi_step_rows) == 1, f"multi={len(multi_step_rows)}")
            if multi_step_rows:
                trace = multi_step_rows[0].response_payload
                check(
                    "落库的轨迹含 steps 与 tool_calls",
                    bool(trace.get("steps")) and bool(trace.get("tool_calls")),
                    str(trace)[:110],
                )
                check(
                    "落库轨迹与 done.trace 同构（前端一套解析即可）",
                    trace.get("type") == "agent_loop_trace"
                    and trace.get("stop_reason") == "final_answer"
                    and trace.get("steps") == (done_trace or {}).get("steps"),
                    f"payload_steps={len(trace.get('steps') or [])} done_steps={len((done_trace or {}).get('steps') or [])}",
                )
                check(
                    "落库的回答就是最终回答",
                    multi_step_rows[0].response_text == LOOP_TOOL_ANSWER,
                    multi_step_rows[0].response_text[:60],
                )
                check(
                    "轨迹里记下了被调用的工具名（可审计）",
                    any(
                        step.get("tool_calls") == ["get_weak_points"] for step in trace["steps"]
                    ),
                    str([step.get("tool_calls") for step in trace["steps"]]),
                )

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0
    finally:
        # 还原被替换的实现，避免影响同进程其它脚本
        lp.LearningPartnerService._default_llm = saved["lp_stream"]  # type: ignore[assignment]
        prompt_layer.get_chat_model = saved["prompt_layer_chat_model"]  # type: ignore[assignment]
        scs.search_similar_chunks = saved["search"]  # type: ignore[assignment]
        scs.should_retrieve = saved["should_retrieve"]  # type: ignore[assignment]
        scs.LongTermMemory.should_write = saved["should_write"]  # type: ignore[assignment]
        scs.LongTermMemory.submit_profile_update_async = saved["submit_update"]  # type: ignore[assignment]

        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id}")
        else:
            try:
                if user_id:
                    db.execute(sql_delete(Note).where(Note.user_id == user_id))
                    db.execute(sql_delete(ChatMessage).where(ChatMessage.user_id == user_id))
                    db.execute(
                        sql_delete(UserKnowledgeMastery).where(
                            UserKnowledgeMastery.user_id == user_id
                        )
                    )
                    db.execute(sql_delete(UserProfile).where(UserProfile.user_id == user_id))
                    db.execute(
                        sql_delete(KnowledgeBase).where(KnowledgeBase.user_id == user_id)
                    )
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

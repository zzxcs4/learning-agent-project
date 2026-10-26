"""验收：第 3 期可观测（token 用量统计 + 轨迹落 workflow_runs）。

对应 `升级规划.md` §八 第 3 期第 2 条：trace_service + trace 存储（先写
`workflow_runs.state_json`，不急着建表）+ token 统计。零 token 消耗（模型全是假桩），
需要真 MySQL（临时数据自建自清）：

| 段 | 验的是什么 | 手段 |
|---|---|---|
| [1] | `extract_usage` / `UsageCallbackHandler` | 纯函数：合成 LLMResult + 畸形形状 |
| [2] | `UsageTracker` 分步记账与汇总 | 纯函数：含"拿不到 usage"的步 |
| [3] | loop 集成（分步 usage 进 steps/trace，事件形状不变） | 假模型 + 真 MySQL |
| [4] | `persist_loop_trace` 落库（completed / failed 两态） | 真 MySQL workflow_runs |
| [5] | **stream() 全链路**：SSE 消费 → workflow_runs 真实落行 | 假模型 + 真 MySQL |

用法（在 agent-backend-project 目录下）：

    python scripts/verify_observability.py
    python scripts/verify_observability.py --keep      # 保留临时数据
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, LLMResult
from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from app.agent.learning_partner import LearningPartnerService
from app.db.database import SessionLocal
from app.db.models import ChatMessage, Session as SessionModel, User, WorkflowRun
from app.security import hash_password
from app.services.trace_service import (
    LOOP_WORKFLOW_TYPE,
    UsageCallbackHandler,
    UsageTracker,
    extract_usage,
    persist_loop_trace,
)

TEMP_PASSWORD = "verify_obs_123"
USAGE_A = {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}

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


def _llm_result(usage: dict[str, int] | None) -> LLMResult:
    """合成 on_llm_end 收到的 LLMResult（聚合消息带 usage_metadata）。"""
    message = AIMessage(content="", usage_metadata=usage)
    return LLMResult(generations=[[ChatGeneration(message=message)]])


def drive_events(service: LearningPartnerService, **kwargs: Any) -> tuple[Any, list[dict]]:
    generator = service.run_events(**kwargs)
    events: list[dict] = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stop:
            return stop.value, events


# ---------------------------------------------------------------------------
# [1] extract_usage / UsageCallbackHandler（纯函数）
# ---------------------------------------------------------------------------
def verify_extract() -> None:
    print("\n[1] extract_usage / UsageCallbackHandler（纯函数）")

    handler = UsageCallbackHandler()
    handler.on_llm_end(_llm_result(USAGE_A))
    check("on_llm_end 取到聚合 usage", handler.usage == USAGE_A, str(handler.usage))

    handler_none = UsageCallbackHandler()
    handler_none.on_llm_end(_llm_result(None))
    check("usage_metadata 缺失 → None（不是 0）", handler_none.usage is None)

    check("畸形形状（空 generations）→ None", extract_usage(LLMResult(generations=[])) is None)
    check("畸形形状（非 LLMResult）→ None", extract_usage(object()) is None)

    class _Boom:
        generations = property(lambda self: (_ for _ in ()).throw(RuntimeError("炸了")))

    check("generations 抛异常 → handler 兜住（绝不打断模型流）", _handler_survives(_Boom()))


def _handler_survives(response: Any) -> bool:
    """handler 必须把提取阶段的任何异常吞掉——它跑在模型流内部。"""
    handler = UsageCallbackHandler()
    try:
        handler.on_llm_end(response)
    except Exception:  # noqa: BLE001
        return False
    return handler.usage is None


# ---------------------------------------------------------------------------
# [2] UsageTracker（纯函数）
# ---------------------------------------------------------------------------
def verify_tracker() -> None:
    print("\n[2] UsageTracker 分步记账与汇总")

    tracker = UsageTracker()
    step1 = tracker.new_handler()
    step1.on_llm_end(_llm_result(USAGE_A))
    tracker.record_step(1, step1)

    step2 = tracker.new_handler()  # 这一步拿不到 usage（假模型 / 中转不回传）
    tracker.record_step(2, step2)

    step3 = tracker.new_handler()
    step3.on_llm_end(_llm_result({"input_tokens": 7, "output_tokens": 6, "total_tokens": 13}))
    tracker.record_step(3, step3)

    summary = tracker.summary()
    check("汇总只累计已知值（10+7 / 4+6）", summary["input_tokens"] == 17 and summary["output_tokens"] == 10, str(summary))
    check("total_tokens = input + output 的独立累计", summary["total_tokens"] == 27, str(summary["total_tokens"]))
    check("分步记录与步号对齐", [row["step"] for row in summary["steps"]] == [1, 2, 3], str([r["step"] for r in summary["steps"]]))
    check("拿不到 usage 的步记 None", summary["steps"][1]["input_tokens"] is None, str(summary["steps"][1]))
    check("step_tokens 按步号取回", tracker.step_tokens(3)["total_tokens"] == 13, str(tracker.step_tokens(3)))
    check("step_tokens 查无此步 → 全 None", tracker.step_tokens(99) == {"input_tokens": None, "output_tokens": None, "total_tokens": None})


# ---------------------------------------------------------------------------
# [3] loop 集成（假模型 + 真 MySQL：usage 进 steps / trace，事件形状不变）
# ---------------------------------------------------------------------------
class UsageFakeModel:
    """带用量回执的假模型：一次 stream = 一步，流结束按真实链路回调 on_llm_end。"""

    def __init__(self, usage: dict[str, int] | None):
        self.usage = usage
        self.calls = 0
        self.handlers: list[Any] = []

    def bind_tools(self, tools: list[Any]) -> "UsageFakeModel":
        return self

    def stream(self, messages: list[Any], config: Any = None) -> Any:
        self.calls += 1
        callbacks = (config or {}).get("callbacks") or []
        self.handlers.append(callbacks[0] if callbacks else None)
        yield AIMessageChunk(content="直接回答。")
        # 真链路时序：on_llm_end 在流结束时携带聚合 usage（stream_usage=True）
        if self.handlers[-1] is not None and self.usage is not None:
            self.handlers[-1].on_llm_end(_llm_result(self.usage))


class FakeWorkerGateway:
    def delegate(self, worker_name: str, payload: dict, *, user_id: int, session_id: int) -> dict:
        return {"worker": worker_name, "ok": True, "status": "dispatched", "data": {}}


def verify_loop_integration(db: Any, user_id: int, session_id: int) -> None:
    print("\n[3] loop 集成（假模型带 usage 回执 + 真 MySQL）")

    model = UsageFakeModel(USAGE_A)
    service = LearningPartnerService(
        db, llm=model, worker_gateway=FakeWorkerGateway()
    )
    result, events = drive_events(
        service,
        user_id=user_id,
        session_id=session_id,
        request_text="你好",
        load_history=False,
    )
    check("事件形状不变（step → delta，多出的只有数据字段）", [e["type"] for e in events] == ["step", "delta"], str([e["type"] for e in events]))
    check("分步 usage 进了 result.steps", result.steps[0]["input_tokens"] == 10 and result.steps[0]["total_tokens"] == 14, str(result.steps[0]))
    check("汇总 usage 挂上 LoopResult", result.usage["total_tokens"] == 14 and result.usage["input_tokens"] == 10, str(result.usage))
    trace = LearningPartnerService._trace_payload(result)
    check("trace 载荷带同一份 usage（response_payload / done / state_json 同形）", trace["usage"] == result.usage)
    check("假模型确实经过了回调接线（handler 被传入）", model.handlers and model.handlers[0] is not None)

    # 拿不到 usage 的模型：全 None、不抛异常，loop 行为不变
    bare = UsageFakeModel(None)
    bare_service = LearningPartnerService(
        db, llm=bare, worker_gateway=FakeWorkerGateway()
    )
    bare_result, _ = drive_events(
        bare_service,
        user_id=user_id,
        session_id=session_id,
        request_text="你好",
        load_history=False,
    )
    check("无 usage 模型：分步字段为 None、汇总为 0", bare_result.steps[0]["input_tokens"] is None and bare_result.usage["total_tokens"] == 0, str(bare_result.usage))
    check("无 usage 模型：stop_reason 正常收敛", bare_result.stop_reason == "final_answer")


# ---------------------------------------------------------------------------
# [4] persist_loop_trace 落库（真 MySQL）
# ---------------------------------------------------------------------------
def verify_persist(db: Any, user_id: int, session_id: int, result: Any) -> list[str]:
    print("\n[4] persist_loop_trace 落库（completed / failed 两态）")

    run_id = persist_loop_trace(
        db,
        user_id=user_id,
        session_id=session_id,
        message_id=12345,
        request_text="帮我看看哪里弱",
        result=result,
        usage=result.usage,
    )
    check("返回 run_id", bool(run_id), str(run_id))
    row = db.scalar(select(WorkflowRun).where(WorkflowRun.run_id == run_id)) if run_id else None
    check(
        "workflow_runs 落行：type=agent_loop / status=completed",
        row is not None and row.workflow_type == LOOP_WORKFLOW_TYPE and row.status == "completed",
        str(row.workflow_type if row else None),
    )
    state = (row.state_json or {}) if row else {}
    check(
        "state_json 含 message_id / 轨迹 / usage",
        state.get("message_id") == 12345
        and state.get("trace", {}).get("steps")
        and state.get("usage", {}).get("total_tokens") == 14,
        json.dumps(state, ensure_ascii=False)[:160],
    )

    failed_id = persist_loop_trace(
        db,
        user_id=user_id,
        session_id=session_id,
        message_id=12346,
        request_text="会失败的一次",
        result=result,
        usage=result.usage,
        error_message="模拟失败",
    )
    failed_row = db.scalar(select(WorkflowRun).where(WorkflowRun.run_id == failed_id)) if failed_id else None
    check("error_message → status=failed", failed_row is not None and failed_row.status == "failed", str(failed_row.status if failed_row else None))

    return [rid for rid in (run_id, failed_id) if rid]


# ---------------------------------------------------------------------------
# [5] stream() 全链路：SSE 消费 → workflow_runs 真实落行
# ---------------------------------------------------------------------------
def verify_stream_e2e(db: Any, user_id: int, session_id: int) -> tuple[int, list[str]]:
    print("\n[5] stream() 全链路（SSE 消费 → 轨迹与 usage 真实落库）")

    service = LearningPartnerService(
        db, llm=UsageFakeModel(USAGE_A), worker_gateway=FakeWorkerGateway()
    )
    frames = list(
        service.stream(
            user_id=user_id,
            session_id=session_id,
            session_model=0,
            request_text="第 3 期可观测验收",
            select_model=0,
        )
    )
    events = [json.loads(frame.removeprefix("data: ")) for frame in frames if frame.startswith("data: ")]
    start = next((e for e in events if e["type"] == "start"), {})
    done = next((e for e in events if e["type"] == "done"), {})
    check("SSE 含 start 与 done 帧", bool(start) and bool(done), str([e["type"] for e in events]))
    check("done 事件带 usage", done.get("usage", {}).get("total_tokens") == 14, str(done.get("usage"))[:120])

    message_id = start.get("message_id")
    row = db.get(ChatMessage, message_id) if message_id else None
    check(
        "response_payload.trace 带同一份 usage（展示职责不变）",
        row is not None and (row.response_payload or {}).get("usage", {}).get("total_tokens") == 14,
        str((row.response_payload or {}).get("usage"))[:120] if row else "无行",
    )

    run = db.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.user_id == user_id, WorkflowRun.workflow_type == LOOP_WORKFLOW_TYPE)
        .order_by(WorkflowRun.id.desc())
    )
    state = (run.state_json or {}) if run else {}
    check(
        "stream() 真实落了 workflow_runs（type=agent_loop）",
        run is not None and run.status == "completed" and state.get("message_id") == message_id,
        f"run_id={run.run_id if run else None}",
    )
    check(
        "state_json 的 usage 与 done 事件一致",
        state.get("usage", {}).get("total_tokens") == 14 and state.get("request_preview") == "第 3 期可观测验收",
        json.dumps(state, ensure_ascii=False)[:160],
    )
    return int(message_id or 0), [run.run_id] if run else []


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="第 3 期可观测验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    args = parser.parse_args()

    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    username = f"vobs{suffix}"
    user_id = session_id = None
    trace_run_ids: list[str] = []
    message_id = 0
    try:
        user = User(username=username, password=hash_password(TEMP_PASSWORD))
        db.add(user)
        db.flush()
        user_id = user.id

        session = SessionModel(user_id=user_id, title="临时可观测会话", session_model=0)
        db.add(session)
        db.commit()
        session_id = session.id
        print(f"临时数据：user={user_id}（{username}） session={session_id}")

        verify_extract()
        verify_tracker()
        verify_loop_integration(db, user_id, session_id)

        # [4]/[5] 复用同一条 loop 结果：先跑一次拿到真实 LoopResult
        result, _ = drive_events(
            LearningPartnerService(
                db, llm=UsageFakeModel(USAGE_A), worker_gateway=FakeWorkerGateway()
            ),
            user_id=user_id,
            session_id=session_id,
            request_text="帮我看看哪里弱",
            load_history=False,
        )
        trace_run_ids = verify_persist(db, user_id, session_id, result)
        message_id, stream_run_ids = verify_stream_e2e(db, user_id, session_id)
        trace_run_ids.extend(stream_run_ids)

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0
    finally:
        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id} message_id={message_id}")
        else:
            try:
                if user_id:
                    # 先删子表（workflow_runs / chat_messages 引用 users+sessions）
                    db.execute(sql_delete(WorkflowRun).where(WorkflowRun.user_id == user_id))
                    db.execute(sql_delete(ChatMessage).where(ChatMessage.user_id == user_id))
                    if session_id:
                        db.execute(sql_delete(SessionModel).where(SessionModel.id == session_id))
                    db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"已清理临时数据：user={user_id}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"清理失败（可手工删）：{exc}")
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

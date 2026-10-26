"""出题参数卡验收（可复跑，**不需要后端服务**，用假出题模型，只写真库）。

验证第 23 轮的核心改动：出题参数（知识库 / 每轮题目数 / 发散度 / 难度）从"发送前表单"
改成"**对话里先问**"之后，到底问谁、问什么、怎么续跑。

钉的是这几件事：

1. 参数没齐 → **不出题**：run 状态 `awaiting_params`、会话里写出一条参数卡消息
   （`request_id = "{run_id}:quiz"`、payload 里带 `params` 规格）；
2. 参数卡规格：题数 5–20 默认 10、发散度五档带说明、难度三档、知识库候选带文档数；
3. "只问缺的"：调用方传了的字段 `resolved=True`，不传的才进 `missing`；
4. 知识库自动使用：用户只有一个库时不再问，直接用它（出题能带上考纲）；
5. 难度默认取画像 `target_level`（中级 → 进阶）；
6. 提交参数 → 出题：**原地更新**那条参数卡消息（同一条消息从"卡"变"题"），
   run 变 `waiting_answer`，练习落库并绑定知识库；
7. 校验：题数 4/21 → 400、发散度 6 → 400、别人的知识库 → 404、
   重复确认 → 400、不存在的 run → 404；
8. 参数齐了 → 不出现参数卡（老调用方零回归）；
9. 四个知识库工具真的能读/能建（含越权拒绝与空名拒绝）。

假模型是有意的：本切片要钉的是**流程与落库口径**，不是模型出题质量。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_quiz_params.py
    python scripts/verify_quiz_params.py --cleanup     # 跑完删掉本次产生的数据
"""

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import delete, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.learning_partner import LearningPartnerService
from app.core.exceptions import AppException
from app.db.database import SessionLocal
from app.db.models import (
    ChatMessage,
    KnowledgeBase,
    PracticeSet,
    Question,
    Session,
    User,
    UserKnowledgeMastery,
    UserProfile,
    WorkflowRun,
)
from app.services.learning_workflow_service import LearningWorkflowService
from app.services.quiz_params_service import (
    DIFFICULTY_LEVELS,
    DIVERGENCE_OPTIONS,
    NO_KNOWLEDGE_BASE_OPTION,
    QUESTION_COUNT_DEFAULT,
    build_params_spec,
    normalize_knowledge_base_id,
    resolve_submitted_params,
)

# GBK 控制台下 ✅/❌ 会抛 UnicodeEncodeError——"语法全过却 exit 1"（交接文档坑 47）
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

FAILED: list[str] = []
PROMPTS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """打印一条断言结果；失败时追加到 FAILED。"""
    print(("✅ " if ok else "❌ ") + label + (f"  —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


class FakeQuizLLM:
    """假出题模型：回放固定题目 JSON，并把提示词留下来给断言检查。"""
    def __init__(self, count: int) -> None:
        questions = [
            {
                "id": f"q{index}",
                "type": "radio",
                "stem": f"第 {index} 题：GIL 的作用是什么？",
                "options": [{"value": "A", "label": "同一时刻只有一个线程执行字节码"}],
                "required": True,
                "points": round(100 / count, 2),
                "difficulty": 3,
            }
            for index in range(1, count + 1)
        ]
        self.payload = {
            "title": "GIL 专项练习",
            "description": "参数卡验收用题",
            "question_count": count,
            "questions": questions,
        }

    def invoke(self, prompt: str) -> SimpleNamespace:
        PROMPTS.append(str(prompt))
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


class FakeLoopModel:
    """假 loop 模型：按脚本"流"出 `(文本, tool_calls)`，用来驱动默认模式的 ReAct loop。

    与 `verify_agent_loop.py` 里的同名假模型同构（那边是 loop 自己的验收，这边只需要
    "委派一次就收工"这一种脚本），所以只留最小实现。
    """

    def __init__(self, script: list[tuple[str, list[dict]]]) -> None:
        self.script = list(script)
        self.calls: list[list] = []
        self.bound_tools: list[str] = []

    def bind_tools(self, tools: list) -> "FakeLoopModel":
        self.bound_tools = [getattr(tool, "name", str(tool)) for tool in tools]
        return self

    def stream(self, messages: list, config=None):
        from langchain_core.messages import AIMessageChunk

        self.calls.append(list(messages))
        index = min(len(self.calls) - 1, len(self.script) - 1)
        text, tool_calls = self.script[index]
        if text:
            yield AIMessageChunk(content=text)
        if tool_calls:
            yield AIMessageChunk(content="", tool_calls=list(tool_calls))


def _loop_call(name: str, args: dict, call_id: str = "call-1") -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _drive_loop(service: LearningPartnerService, **kwargs) -> tuple:
    """把 `run_events` 跑到底，返回（LoopResult, 事件列表）。"""
    generator = service.run_events(**kwargs)
    events: list[dict] = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stop:
            return stop.value, events


def _make_user(db, name: str) -> int:
    user = User(username=name, password="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return int(user.id)


def _make_session(db, user_id: int, stamp: int) -> int:
    session = Session(user_id=user_id, session_model=0, title=f"参数卡验收_{stamp}")
    db.add(session)
    db.commit()
    db.refresh(session)
    return int(session.id)


def _make_kb(db, user_id: int, name: str) -> int:
    kb = KnowledgeBase(user_id=user_id, name=name)
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return int(kb.id)


def _card_message(db, user_id: int, run_id: str) -> ChatMessage | None:
    return db.scalar(
        select(ChatMessage).where(
            ChatMessage.user_id == user_id,
            ChatMessage.request_id == f"{run_id}:quiz",
        )
    )


def _raises_not_found(call) -> bool:
    """跑一段会抛异常的调用，判断它是不是 404（归属校验用的最小断言工具）。"""
    try:
        call()
    except AppException as exc:
        return exc.code == 404
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="出题参数卡验收")
    parser.add_argument("--cleanup", action="store_true", help="跑完删除本次产生的数据")
    args = parser.parse_args()

    stamp = int(time.time())
    db = SessionLocal()
    user_id = stranger_id = 0
    kb_ids: list[int] = []
    try:
        # ---------------- 准备：一个用户 + 两个知识库（所以"选哪个库"是要问的） ----------------
        user_id = _make_user(db, f"vanq{stamp}")
        session_id = _make_session(db, user_id, stamp)
        kb_a = _make_kb(db, user_id, f"参数卡验收库A_{stamp}")
        kb_b = _make_kb(db, user_id, f"参数卡验收库B_{stamp}")
        kb_ids = [kb_a, kb_b]
        profile = UserProfile(user_id=user_id, target_level="中级")
        db.add(profile)
        db.commit()
        print(f"[0] 临时用户 user_id={user_id} 会话={session_id} 知识库={kb_ids} 画像等级=中级")

        service = LearningWorkflowService(db, llm=FakeQuizLLM(7))

        # ---------------- 场景 1：什么都不传 → 出参数卡，不出题 ----------------
        state = service.start(user_id=user_id, session_id=session_id, request_text="给我出点题")
        run_id = str(state["run_id"])
        spec = state.get("params") or {}
        check("参数没齐时不出题（quiz 为空）", not (state.get("quiz") or {}).get("questions"))
        check("run 状态是 awaiting_params", state.get("status") == "awaiting_params", str(state.get("status")))
        run = db.scalar(select(WorkflowRun).where(WorkflowRun.run_id == run_id))
        check("workflow_runs.status 也是 awaiting_params", run.status == "awaiting_params", str(run.status))
        check("没有落练习（awaiting_params 阶段不该写 practice_sets）",
              db.scalar(select(PracticeSet.id).where(PracticeSet.user_id == user_id)) is None)

        card = _card_message(db, user_id, run_id)
        check("会话里写出了参数卡消息（request_id={run_id}:quiz）", card is not None)
        payload = (card.response_payload or {}) if card else {}
        check("参数卡 payload.status = awaiting_params", payload.get("status") == "awaiting_params",
              str(payload.get("status")))
        check("参数卡 payload.params 带规格", bool(payload.get("params")))

        # ---------------- 场景 2：规格内容（题数/发散度/难度/知识库） ----------------
        fields = {str(field.get("key")): field for field in spec.get("fields") or []}
        check("规格四个字段齐全",
              set(fields) == {"knowledge_base_id", "question_count", "divergence", "difficulty"},
              str(sorted(fields)))
        check(f"missing = 题数/发散度/知识库（难度有画像默认但也要问）",
              set(spec.get("missing") or []) == {"knowledge_base_id", "question_count", "divergence", "difficulty"},
              str(spec.get("missing")))
        count_field = fields.get("question_count") or {}
        check(f"题数范围 5–20、默认 {QUESTION_COUNT_DEFAULT}",
              (count_field.get("min"), count_field.get("max"), count_field.get("default")) == (5, 20, 10),
              str(count_field))
        div_opts = (fields.get("divergence") or {}).get("options") or []
        check("发散度五档带说明（含 聚焦薄弱 / 全面探索）",
              len(div_opts) == len(DIVERGENCE_OPTIONS)
              and [item.get("label") for item in div_opts][:1] == ["聚焦薄弱"]
              and [item.get("label") for item in div_opts][-1:] == ["全面探索"]
              and all(item.get("description") for item in div_opts),
              str([item.get("label") for item in div_opts]))
        diff_opts = (fields.get("difficulty") or {}).get("options") or []
        check("难度三档 = 入门/进阶/高级",
              [item.get("value") for item in diff_opts] == list(DIFFICULTY_LEVELS),
              str([item.get("value") for item in diff_opts]))
        check("难度默认值取画像 target_level（中级 → 进阶）",
              (fields.get("difficulty") or {}).get("default") == "进阶"
              and (fields.get("difficulty") or {}).get("value") == "进阶",
              str(fields.get("difficulty")))
        kb_field = fields.get("knowledge_base_id") or {}
        kb_options = kb_field.get("options") or []
        check("知识库候选第一项恒是「不使用知识库」（value=0）",
              kb_options[:1] == [NO_KNOWLEDGE_BASE_OPTION],
              str(kb_options[:1]))
        check("知识库候选 = 「不使用」+ 自己的两个库（带文档数）",
              sorted(item.get("value") for item in kb_options[1:]) == sorted(kb_ids),
              str([item.get("value") for item in kb_options]))
        check("知识库字段未 resolved（有两个库所以要问）", kb_field.get("resolved") is False)

        # ---------------- 场景 3：提交参数 → 出题 + 原地更新那张卡 ----------------
        card_id = int(card.id)
        final = service.configure(
            user_id=user_id,
            run_id=run_id,
            question_count=7,
            knowledge_base_id=kb_a,
            divergence=1,
            difficulty="高级",
        )
        questions = (final.get("quiz") or {}).get("questions") or []
        check("提交参数后真的出题了（7 题）", len(questions) == 7, f"题目数={len(questions)}")
        check("run 变 waiting_answer", final.get("status") == "waiting_answer", str(final.get("status")))
        check("参数写回状态（7 / kb_a / 1 / 高级）",
              (final.get("question_count"), final.get("knowledge_base_id"),
               final.get("divergence"), final.get("difficulty")) == (7, kb_a, 1, "高级"),
              f"{final.get('question_count')}/{final.get('knowledge_base_id')}/"
              f"{final.get('divergence')}/{final.get('difficulty')}")
        db.refresh(card)
        check("参数卡消息**原地**更新（同一条 id）",
              card.id == card_id
              and (card.response_payload or {}).get("status") == "waiting_answer"
              and len((card.response_payload or {}).get("quiz", {}).get("questions") or []) == 7,
              f"id={card.id} status={(card.response_payload or {}).get('status')}")
        check("会话里没有多出第二条消息（一次练习只占一条）",
              len(list(db.scalars(select(ChatMessage.id).where(
                  ChatMessage.user_id == user_id,
                  ChatMessage.session_id == session_id,
                  ChatMessage.request_id == f"{run_id}:quiz",
              )))) == 1)
        practice = db.scalar(select(PracticeSet).where(PracticeSet.user_id == user_id))
        check("练习落库并绑定知识库 A",
              practice is not None and practice.knowledge_base_id == kb_a,
              f"kb={getattr(practice, 'knowledge_base_id', None)}")
        check("练习题目数与参数一致", int(practice.question_count or 0) == 7, str(practice.question_count))
        prompt = PROMPTS[-1] if PROMPTS else ""
        check("提示词里出现本次难度块（高级）", "## 本次难度：高级" in prompt)
        check(
            "提示词里的发散度块是「聚焦薄弱」档",
            "## 本轮发散度：1/5" in prompt and "聚焦薄弱" in prompt,
        )

        # ---------------- 场景 4：校验 ----------------
        def expect_error(label: str, code: int, **kwargs) -> None:
            try:
                service.configure(user_id=user_id, run_id=run_id, **kwargs)
                check(label, False, "没有抛异常")
            except AppException as exc:
                check(label, exc.code == code, f"code={exc.code}")

        expect_error("重复确认参数 → 400", 400, question_count=7)
        expect_error("题数 4（低于范围）→ 400", 400, question_count=4)
        expect_error("题数 21（高于范围）→ 400", 400, question_count=21)
        expect_error("发散度 6（超出五档）→ 400", 400, divergence=6)
        try:
            service.configure(user_id=stranger_id or 999999, run_id=run_id, question_count=7)
            check("别人的 run 不能确认参数（应抛 404）", False, "没有抛异常")
        except AppException as exc:
            check("别人的 run 不能确认参数（404）", exc.code == 404, f"code={exc.code}")

        # ---------------- 场景 5：参数齐了就不出现参数卡（老调用方零回归） ----------------
        direct = LearningWorkflowService(db, llm=FakeQuizLLM(5)).start(
            user_id=user_id,
            session_id=session_id,
            request_text="直接出 5 道题",
            question_count=5,
            knowledge_base_id=kb_a,
            divergence=3,
            difficulty="进阶",
        )
        check("参数齐了直接出题（无参数卡）",
              direct.get("status") == "waiting_answer"
              and len((direct.get("quiz") or {}).get("questions") or []) == 5,
              str(direct.get("status")))
        check("参数齐了时不返回 params 规格", not direct.get("params"))

        # ---------------- 场景 6：只有一个库 → 自动使用，不再问 ----------------
        single_user = _make_user(db, f"vanq1{stamp}")
        single_session = _make_session(db, single_user, stamp)
        single_kb = _make_kb(db, single_user, f"参数卡验收唯一库_{stamp}")
        kb_ids.append(single_kb)
        spec_single = build_params_spec(
            db, user_id=single_user, question_count=None,
            knowledge_base_id=None, divergence=None, difficulty=None,
        )
        kb_only = {f["key"]: f for f in spec_single["fields"]}["knowledge_base_id"]
        check("唯一知识库自动使用（resolved=True 且 value=那个库）",
              kb_only["resolved"] is True and kb_only["value"] == single_kb,
              f"resolved={kb_only['resolved']} value={kb_only['value']}")
        check("唯一库时不把知识库算进 missing",
              "knowledge_base_id" not in spec_single["missing"], str(spec_single["missing"]))
        check("自动使用时给出说明文案", "自动使用" in str(kb_only.get("note")), str(kb_only.get("note")))

        # ---------------- 场景 7：知识库工具（读 + 写 + 越权/空名） ----------------
        from app.agent.tools import DEFAULT_TOOL_NAMES, TOOL_REGISTRY
        from app.agent.tools.tool_context import ToolContext
        from app.agent.tools.runtime import ToolRuntime

        runtime = ToolRuntime(
            context=ToolContext(db=db, user_id=user_id, session_id=session_id, knowledge_base_id=kb_a),
            allowed_tools=DEFAULT_TOOL_NAMES,
            tools=TOOL_REGISTRY,
        )
        bases = json.loads(runtime.execute("list_knowledge_bases", {}))
        check("list_knowledge_bases 列出自己的库（含 id/名称/文档数）",
              bases.get("count") == 2
              and all(item.get("knowledge_base_id") and "document_count" in item
                      for item in bases.get("knowledge_bases") or []),
              json.dumps(bases, ensure_ascii=False)[:120])
        docs = json.loads(runtime.execute("list_documents", {"knowledge_base_id": kb_a}))
        check("list_documents 空库返回 count=0 + note",
              docs.get("count") == 0 and "note" in docs, json.dumps(docs, ensure_ascii=False)[:120])
        summary = json.loads(runtime.execute("get_document_summary", {"knowledge_base_id": kb_a}))
        check("get_document_summary 给出总量与分类计数",
              summary.get("document_count") == 0 and "by_kind" in summary and "by_month" in summary,
              json.dumps(summary, ensure_ascii=False)[:120])
        stranger_id = _make_user(db, f"vanq2{stamp}")
        stranger_kb = _make_kb(db, stranger_id, f"别人的库_{stamp}")
        kb_ids.append(stranger_kb)
        denied = json.loads(runtime.execute("list_documents", {"knowledge_base_id": stranger_kb}))
        check("不能读别人的库（返回可读错误）", "error" in denied, json.dumps(denied, ensure_ascii=False)[:120])
        created = json.loads(runtime.execute("create_knowledge_base", {"name": f"工具建的库_{stamp}"}))
        check("create_knowledge_base 真的建库并返回 id",
              bool(created.get("knowledge_base_id")) and "note" in created,
              json.dumps(created, ensure_ascii=False)[:120])
        if created.get("knowledge_base_id"):
            kb_ids.append(int(created["knowledge_base_id"]))
            check("建出来的库归属当前用户",
                  db.scalar(select(KnowledgeBase.user_id).where(
                      KnowledgeBase.id == int(created["knowledge_base_id"]))) == user_id)
        blank = json.loads(runtime.execute("create_knowledge_base", {"name": "   "}))
        check("空名建库被拒绝", "error" in blank, json.dumps(blank, ensure_ascii=False)[:80])

        # ---------------- 场景 8：默认模式"全委派"（loop 的 delegate_quiz 路径） ----------------
        #
        # 这一条钉的是"全委派"能否成立：委派出去后 worker 只落一张参数卡就返回，
        # **不调模型**（参数没定就出题 = 白花额度），且返回体里必须有 run_id——
        # 前端靠 `tool_result.run_id` 认领那条参数卡消息（`_delegated_quiz_run_id`）。
        from app.agent.workers import WorkerGateway

        gateway = WorkerGateway(db)
        delegated = gateway.delegate(
            "delegate_quiz",
            {
                "session_id": session_id,
                "session_model": 0,
                "request_text": "针对薄弱点出一套题",
            },
            user_id=user_id,
            session_id=session_id,
        )
        delegated_data = delegated.get("data") or {}
        delegated_run = str(delegated_data.get("run_id") or "")
        check(
            "委派成功且状态是 awaiting_params（loop 拿到结果就收工）",
            delegated.get("ok") is True
            and delegated_data.get("status") == "awaiting_params",
            f"ok={delegated.get('ok')} status={delegated_data.get('status')}",
        )
        check("委派结果里带 run_id（前端靠它认领参数卡消息）", bool(delegated_run), delegated_run)
        check(
            "委派回来的结果明确告诉模型「别再委派」",
            "不要再委派" in str(delegated_data.get("note") or ""),
            str(delegated_data.get("note"))[:80],
        )
        card_by_delegate = _card_message(db, user_id, delegated_run)
        delegate_status = (
            (card_by_delegate.response_payload or {}).get("status") if card_by_delegate else "无消息"
        )
        check(
            "委派路径真的落了参数卡消息",
            card_by_delegate is not None and delegate_status == "awaiting_params",
            str(delegate_status),
        )
        check("参数没定时不调模型（委派路径零额度）", len(PROMPTS) == 2, f"模型调用次数={len(PROMPTS)}")

        # ---------------- 场景 9：真的走一遍 loop（默认模式 + 真网关） ----------------
        #
        # 场景 8 只验了 worker 那一半。这一条把 loop 也接上：假模型（只委派一次就收工）
        # + **真** WorkerGateway，端到端证明"默认模式委派 → 参数卡 → tool_result 带 run_id"
        # 这条链是通的（前端就是靠这个 run_id 回刷出参数卡的）。
        loop_model = FakeLoopModel(
            [
                ("", [_loop_call("delegate_quiz", {"request_text": "针对我的弱点出一套题"})]),
                ("参数卡已经给你了，先确认再出题。", []),
            ]
        )
        loop_service = LearningPartnerService(db, llm=loop_model, worker_gateway=WorkerGateway(db))
        loop_result, loop_events = _drive_loop(
            loop_service,
            user_id=user_id,
            session_id=session_id,
            request_text="针对我的弱点出一套题",
            load_history=False,
        )
        tool_results = [event for event in loop_events if event.get("type") == "tool_result"]
        loop_quiz_event = next(
            (event for event in tool_results if event.get("name") == "delegate_quiz"), {}
        )
        loop_run_id = str(loop_quiz_event.get("run_id") or "")
        check(
            "loop 委派后 tool_result 事件带回真 run_id（前端据此回刷）",
            bool(loop_run_id) and loop_run_id != "fake-run-1",
            loop_run_id,
        )
        loop_run = (
            db.scalar(select(WorkflowRun).where(WorkflowRun.run_id == loop_run_id))
            if loop_run_id
            else None
        )
        check(
            "loop 委派出的运行停在 awaiting_params（没有直接出题）",
            loop_run is not None and loop_run.status == "awaiting_params",
            getattr(loop_run, "status", "无运行"),
        )
        loop_card = _card_message(db, user_id, loop_run_id) if loop_run_id else None
        loop_card_status = (
            (loop_card.response_payload or {}).get("status") if loop_card else "无消息"
        )
        check(
            "loop 委派真的在会话里落了参数卡",
            loop_card is not None and loop_card_status == "awaiting_params",
            str(loop_card_status),
        )
        check(
            "loop 收工后模型给出最终回答（全委派：不等出题结果、不二次委派）",
            bool(loop_result.answer) and len(tool_results) == 1,
            f"answer={loop_result.answer[:24]!r} tool_result={len(tool_results)}",
        )
        check("loop 路径同样零出题额度", len(PROMPTS) == 2, f"模型调用次数={len(PROMPTS)}")

        # ---------------- 场景 10：知识库挂载 / 「不使用知识库」（第 24 轮） ----------------
        #
        # 第 24 轮把输入区的知识库控件改成一个数据库图标 + 点开的选择器，并把挂载做成
        # **会话级**；选择器里第一项恒是「不使用知识库」。这里验三件事：
        #   (a) 哨兵值 0 = "用户明确不用库"：出题**不再追问**，且练习不归属任何库；
        #   (b) 会话里挂了库时，loop 的 `delegate_quiz` 自动带上这个库（出题不再问库）；
        #   (c) 提示词里明确告诉模型"本会话挂载了哪个库"（否则它会反问用户用哪个库）。
        check("哨兵 0/负数/空 都收敛成 None（绝不去过滤 knowledge_base_id=0）",
              [normalize_knowledge_base_id(value) for value in (0, None, "", -3, "x")] == [None] * 5,
              str([normalize_knowledge_base_id(value) for value in (0, None, "", -3, "x")]))
        check("有效 id 原样返回", normalize_knowledge_base_id(7) == 7 and normalize_knowledge_base_id("9") == 9)

        none_spec = build_params_spec(
            db, user_id=user_id, question_count=None, knowledge_base_id=0,
            divergence=None, difficulty=None,
        )
        none_field = next(item for item in none_spec["fields"] if item["key"] == "knowledge_base_id")
        check("明确不用库时知识库字段已定（value=0、不再问）",
              none_field.get("resolved") is True and none_field.get("value") == 0,
              f"resolved={none_field.get('resolved')} value={none_field.get('value')}")
        check("「不使用知识库」的说明写清了是用户的选择",
              "不使用知识库" in str(none_field.get("note") or ""),
              str(none_field.get("note")))
        check("缺的字段里不再有知识库（只问题数/发散度/难度）",
              "knowledge_base_id" not in (none_spec.get("missing") or []),
              str(none_spec.get("missing")))

        # (a) 明确不用库 → 直接出题，不落参数卡
        none_state = service.start(
            user_id=user_id, session_id=session_id, request_text="不用资料，随便出几道题",
            knowledge_base_id=0, question_count=5, divergence=3, difficulty="进阶",
        )
        none_run_id = str(none_state["run_id"])
        check("不用库时直接出题（不出参数卡）",
              len((none_state.get("quiz") or {}).get("questions") or []) > 0
              and none_state.get("question_count") == 5,
              f"题目数={len((none_state.get('quiz') or {}).get('questions') or [])} "
              f"题量参数={none_state.get('question_count')}")
        check("不用库时状态是 waiting_answer", none_state.get("status") == "waiting_answer",
              str(none_state.get("status")))
        none_practice_id = int(none_state.get("practice_set_id") or 0)
        check("不用库时落库知识库为 NULL（不是 0）",
              none_state.get("knowledge_base_id") is None
              and none_practice_id > 0
              and db.scalar(select(PracticeSet.knowledge_base_id).where(
                  PracticeSet.id == none_practice_id
              )) is None,
              f"state={none_state.get('knowledge_base_id')} practice={none_practice_id}")
        none_messages = list(db.scalars(select(ChatMessage).where(
            ChatMessage.user_id == user_id,
            ChatMessage.request_id == f"{none_run_id}:quiz",
        )))
        check("不用库时只有一条出题消息，且是题目不是参数卡",
              len(none_messages) == 1
              and (none_messages[0].response_payload or {}).get("status") == "waiting_answer",
              f"消息数={len(none_messages)} status="
              f"{(none_messages[0].response_payload or {}).get('status') if none_messages else '无'}")

        # (b)(c) 会话挂载：loop 委派自动带上挂载的库 + 提示词告诉模型挂了哪个库
        mounted_model = FakeLoopModel(
            [
                ("", [_loop_call("delegate_quiz", {"request_text": "给我出点题"}, "m1")]),
                ("已按你挂载的知识库出题。", []),
            ]
        )
        mounted_service = LearningPartnerService(
            db, llm=mounted_model, worker_gateway=WorkerGateway(db)
        )
        _mounted_result, mounted_events = _drive_loop(
            mounted_service,
            user_id=user_id,
            session_id=session_id,
            request_text="给我出点题",
            knowledge_base_id=kb_a,
            load_history=False,
        )
        mounted_event = next(
            (event for event in mounted_events
             if event.get("type") == "tool_result" and event.get("name") == "delegate_quiz"),
            {},
        )
        mounted_run_id = str(mounted_event.get("run_id") or "")
        mounted_run = (
            db.scalar(select(WorkflowRun).where(WorkflowRun.run_id == mounted_run_id))
            if mounted_run_id else None
        )
        check("挂了库时 loop 委派自动带上该库（出题不再问知识库）",
              mounted_run is not None
              and int((mounted_run.state_json or {}).get("knowledge_base_id") or 0) == kb_a,
              str((mounted_run.state_json or {}).get("knowledge_base_id")) if mounted_run else "无运行")
        # 挂载的库让"知识库"这一项**已定**：参数卡仍然会出现（题数/发散度/难度还没定），
        # 但它只问那三项，绝不再问知识库——用户已经在输入区挂好了。
        mounted_card = _card_message(db, user_id, mounted_run_id) if mounted_run_id else None
        mounted_spec = (
            (mounted_card.response_payload or {}).get("params") or {} if mounted_card else {}
        )
        mounted_kb_field = next(
            (item for item in mounted_spec.get("fields") or []
             if item.get("key") == "knowledge_base_id"),
            {},
        )
        check("挂了库时参数卡不再问知识库（该项已定 = 挂载的库）",
              mounted_kb_field.get("resolved") is True
              and int(mounted_kb_field.get("value") or 0) == kb_a
              and set(mounted_spec.get("missing") or [])
              == {"question_count", "divergence", "difficulty"},
              f"resolved={mounted_kb_field.get('resolved')} value={mounted_kb_field.get('value')} "
              f"missing={mounted_spec.get('missing')}")
        # 确认参数时不带知识库 key（前端参数卡本来就不会提交已定的字段）也要沿用挂载的库：
        # `resolve_submitted_params` 回落到字段 default，这正是"挂载 = 已有信息"的落地。
        mounted_final = LearningWorkflowService(db, llm=FakeQuizLLM(6)).configure(
            user_id=user_id, run_id=mounted_run_id,
            question_count=6, divergence=2, difficulty="进阶",
        )
        check("参数卡确认后出题仍归属挂载的库（提交体没带这个 key）",
              int(mounted_final.get("knowledge_base_id") or 0) == kb_a,
              str(mounted_final.get("knowledge_base_id")))

        # (d)(e) 第 25 轮：**出题要求一律用用户原话**（模型爱把"出题"扩写成一段自编知识范围，
        # 那段字会压过知识库考纲 → 出题偏离资料）；考纲块里还要写明优先级。
        fabricated = "围绕「大模型的核心原理」，侧重 Transformer/自注意力、Embedding 向量化"
        captured_payloads: list[dict[str, Any]] = []

        class _SpyGateway(WorkerGateway):
            """记下真正发给 worker 的载荷（`tool_result` 事件里只有预览，看不到载荷全貌）。"""

            def delegate(self, worker_name: str, payload: dict[str, Any], **kwargs: Any):
                captured_payloads.append({"name": worker_name, "payload": dict(payload)})
                return super().delegate(worker_name, payload, **kwargs)

        brief_model = FakeLoopModel(
            [
                ("", [_loop_call("delegate_quiz", {"request_text": fabricated}, "m2")]),
                ("已按你的要求出题。", []),
            ]
        )
        brief_service = LearningPartnerService(
            db, llm=brief_model, worker_gateway=_SpyGateway(db)
        )
        _brief_result, brief_events = _drive_loop(
            brief_service,
            user_id=user_id,
            session_id=session_id,
            request_text="出题",
            knowledge_base_id=kb_a,
            load_history=False,
        )
        brief_event = next(
            (event for event in brief_events
             if event.get("type") == "tool_result" and event.get("name") == "delegate_quiz"),
            {},
        )
        brief_run_id = str(brief_event.get("run_id") or "")
        brief_run = (
            db.scalar(select(WorkflowRun).where(WorkflowRun.run_id == brief_run_id))
            if brief_run_id else None
        )
        brief_state = dict((brief_run.state_json or {})) if brief_run else {}
        brief_payload = captured_payloads[-1]["payload"] if captured_payloads else {}
        check("委派载荷里的出题要求就是用户原话（模型自编那段被顶掉）",
              brief_payload.get("request_text") == "出题",
              str(brief_payload.get("request_text")))
        check("模型自编那段不丢：进载荷 model_brief（trace 里可追溯）",
              brief_payload.get("model_brief") == fabricated,
              str(brief_payload.get("model_brief"))[:80])
        check("运行状态里的 request_text 也是用户原话（会话气泡不会再冒充用户说话）",
              brief_state.get("request_text") == "出题",
              str(brief_state.get("request_text")))

        # 出题提示词：范围以考纲为准 + 忽略考纲外的自拟知识点（要真的带考纲才验得到）
        kb_outline = KnowledgeBase(
            user_id=user_id,
            name="有考纲的库",
            quiz_core={
                "sections": [
                    {"heading": "Transformer 基础", "topics": ["自注意力"], "asked_count": 0},
                    {"heading": "训练与优化", "topics": ["梯度下降"], "asked_count": 0},
                ],
                "frequently_tested": ["自注意力的计算复杂度"],
                "followup_paths": [],
            },
        )
        db.add(kb_outline)
        db.commit()
        db.refresh(kb_outline)
        PROMPTS.clear()
        LearningWorkflowService(db, llm=FakeQuizLLM(5)).start(
            user_id=user_id, session_id=session_id,
            request_text="出题",  # 用户原话就这么短，模拟"只说了一个出题"
            question_count=5, divergence=3, difficulty="进阶",
            knowledge_base_id=kb_outline.id,
        )
        brief_prompt = PROMPTS[-1] if PROMPTS else ""
        check("出题提示词带上了考纲（有考纲的库才验得到优先级那句话）",
              "Transformer 基础" in brief_prompt, brief_prompt[-200:].replace("\n", " "))
        check("出题提示词写明考纲优先、忽略用户要求里考纲外的知识点",
              "范围以上面的考纲为准" in brief_prompt
              and "绝对不要按它去编造考纲里没有的内容" in brief_prompt,
              "缺优先级块")
        check("出题提示词的「用户要求」就是用户原话（没有自编知识范围）",
              "用户要求：出题" in brief_prompt and fabricated not in brief_prompt,
              "用户要求：出题" in brief_prompt)

        # (f) 第 25 轮异步出题：`begin_configure` 立即返回 + `generate_for_run` 后台跑完
        async_state = service.start(user_id=user_id, session_id=session_id, request_text="出题")
        async_run_id = str(async_state["run_id"])
        async_spec = _card_message(db, user_id, async_run_id)
        async_service = LearningWorkflowService(db, llm=FakeQuizLLM(5))
        prepared = async_service.begin_configure(
            user_id=user_id, run_id=async_run_id,
            question_count=5, divergence=2, difficulty="入门",
        )
        check("异步前半段立即返回 generating_quiz", prepared.get("status") == "generating_quiz",
              str(prepared.get("status")))
        async_card = _card_message(db, user_id, async_run_id)
        check("参数卡消息就地变成「正在出题」（刷新回来不会又是一张确认卡）",
              (async_card.response_payload or {}).get("status") == "generating_quiz"
              if async_card else False,
              str((async_card.response_payload or {}).get("status")) if async_card else "无消息")
        async_final = async_service.generate_for_run(
            user_id=user_id, run_id=async_run_id, state=prepared["state"]
        )
        check("后台出题跑完 → waiting_answer 且有题",
              async_final.get("status") == "waiting_answer"
              and len((async_final.get("quiz") or {}).get("questions") or []) == 5,
              f"status={async_final.get('status')}")
        async_card_done = _card_message(db, user_id, async_run_id)
        check("后台出题把那条消息就地换成题目（不新增气泡）",
              (async_card_done.response_payload or {}).get("status") == "waiting_answer"
              and len(((async_card_done.response_payload or {}).get("quiz") or {})
                      .get("questions") or []) == 5
              if async_card_done else False,
              str((async_card_done.response_payload or {}).get("status")) if async_card_done else "无消息")

        # (g) 后台出题失败：运行标 failed，**消息要写出原因**（否则用户看到一张永远转圈的卡）
        failed_state = service.start(user_id=user_id, session_id=session_id, request_text="再出一套")
        failed_run_id = str(failed_state["run_id"])

        class _BoomLLM:
            def invoke(self, prompt: str) -> SimpleNamespace:  # noqa: ARG002 - 故意炸
                raise RuntimeError("模型挂了")

        failed_service = LearningWorkflowService(db, llm=_BoomLLM())
        failed_prepared = failed_service.begin_configure(
            user_id=user_id, run_id=failed_run_id,
            question_count=5, divergence=2, difficulty="入门",
        )
        try:
            failed_service.generate_for_run(
                user_id=user_id, run_id=failed_run_id, state=failed_prepared["state"]
            )
            raised = False
        except Exception:  # noqa: BLE001 - 后台任务会吞掉它，这里只看状态
            raised = True
        failed_row = db.scalar(select(WorkflowRun).where(WorkflowRun.run_id == failed_run_id))
        failed_card = _card_message(db, user_id, failed_run_id)
        check("后台出题失败 → 运行标 failed", bool(raised) and failed_row.status == "failed",
              f"raised={raised} status={getattr(failed_row, 'status', '无')}")
        check("失败时消息写出原因（不会留一张永远转圈的卡）",
              (failed_card.response_payload or {}).get("status") == "failed"
              and "出题失败" in str(failed_card.response_text or "")
              if failed_card else False,
              str(failed_card.response_text)[:60] if failed_card else "无消息")
        check("挂了库的那次出题真的落到了该库",
              db.scalar(select(PracticeSet.knowledge_base_id).where(
                  PracticeSet.id == int(mounted_final.get("practice_set_id") or 0)
              )) == kb_a,
              str(mounted_final.get("practice_set_id")))
        mounted_prompt_text = str(
            mounted_model.calls[0][0].content
            if mounted_model.calls and mounted_model.calls[0] else ""
        )
        check("提示词告诉模型本会话挂载了哪个库（它就不会反问用户）",
              "本会话已挂载知识库" in mounted_prompt_text
              and f"参数卡验收库A_{stamp}" in mounted_prompt_text,
              mounted_prompt_text[-140:] if mounted_prompt_text else "提示词为空")

        # 挂载 0（明确不用库）：loop 侧必须收敛成 None，否则检索会变成
        # `WHERE knowledge_base_id = 0` —— 用户看到的是"什么都搜不到"。
        zero_model = FakeLoopModel([("好，这次不用你的资料。", [])])
        zero_service = LearningPartnerService(db, llm=zero_model, worker_gateway=WorkerGateway(db))
        _zero_result, _zero_events = _drive_loop(
            zero_service,
            user_id=user_id,
            session_id=session_id,
            request_text="不用资料，随便聊聊",
            knowledge_base_id=0,
            load_history=False,
        )
        check("挂载 0（不用库）时 loop 侧收敛成 None（检索不会按 0 过滤）",
              zero_service._knowledge_base_id is None
              and zero_service._knowledge_base_id != 0,
              str(zero_service._knowledge_base_id))
        check("挂载 0 时提示词里没有知识库挂载块（不误导模型）",
              "本会话已挂载知识库" not in str(
                  zero_model.calls[0][0].content if zero_model.calls and zero_model.calls[0] else ""
              ),
              "")
        # 编排入口：默认模式（loop 路径）拿到的必须是收敛后的 None，
        # 否则 `search_material` 会按 `knowledge_base_id = 0` 过滤——一条资料都搜不到。
        from app.services.orchestrator_service import LOOP_PATH, OrchestratorService

        captured: dict[str, Any] = {}
        orchestrator = OrchestratorService(db)
        orchestrator.classify_entry_path = lambda *_a, **_k: {"path": LOOP_PATH, "reason": "验收"}  # type: ignore[method-assign]
        orchestrator._start_loop = (  # type: ignore[method-assign]
            lambda **kwargs: (captured.update(kwargs), {"ok": True})[1]
        )
        orchestrator.start_task(
            user_id=user_id, session_id=session_id, request_text="随便聊聊",
            select_model="", knowledge_base_id=0,
        )
        check("默认模式入口把 0 收敛成 None（检索不会按 0 过滤）",
              captured.get("knowledge_base_id") is None and captured != {},
              str(captured.get("knowledge_base_id")))
        orchestrator.start_task(
            user_id=user_id, session_id=session_id, request_text="随便聊聊",
            select_model="", knowledge_base_id=kb_a,
        )
        check("默认模式入口保留有效知识库 id", captured.get("knowledge_base_id") == kb_a,
              str(captured.get("knowledge_base_id")))
        check("参数卡提交 0 = 落库 NULL 且不追问",
              resolve_submitted_params(
                  db, user_id=user_id,
                  spec=build_params_spec(db, user_id=user_id, question_count=None,
                                         knowledge_base_id=0, divergence=None, difficulty=None),
                  submitted={"knowledge_base_id": 0},
              ).get("knowledge_base_id") is None,
              "")
        # 「没提供」= 键不存在 **或值为 None**：两者必须等价，否则服务层/HTTP 层摆一个
        # `knowledge_base_id=None` 就会把已定的库清成 NULL（第 23 轮唯一库自动使用确认后丢库的根因）
        resolved_spec = build_params_spec(
            db, user_id=user_id, question_count=7, knowledge_base_id=kb_a,
            divergence=2, difficulty="进阶",
        )
        check("显式 None 提交不覆盖已定值（等价于没提供）",
              resolve_submitted_params(
                  db, user_id=user_id, spec=resolved_spec,
                  submitted={"knowledge_base_id": None, "question_count": None},
              ).get("knowledge_base_id") == kb_a,
              "")
        check("唯一库自动使用时确认参数不会丢库",
              resolve_submitted_params(
                  db, user_id=single_user, spec=build_params_spec(
                      db, user_id=single_user, question_count=None,
                      knowledge_base_id=None, divergence=None, difficulty=None),
                  submitted={"knowledge_base_id": None, "question_count": 6,
                             "divergence": 3, "difficulty": "进阶"},
              ).get("knowledge_base_id") == single_kb,
              "")
        check("参数卡提交别人的库仍然 404（0 的放行没有放宽归属校验）",
              _raises_not_found(lambda: resolve_submitted_params(
                  db, user_id=user_id,
                  spec=build_params_spec(db, user_id=user_id, question_count=None,
                                         knowledge_base_id=None, divergence=None, difficulty=None),
                  submitted={"knowledge_base_id": stranger_kb},
              )),
              "")

        if args.cleanup:
            _cleanup(db, user_ids=[user_id, single_user, stranger_id], knowledge_base_ids=kb_ids)
            print("[9] 已清理本次产生的数据")
    finally:
        db.close()

    print(f"\n出题参数卡验收：{'全部通过' if not FAILED else '失败 ' + str(len(FAILED)) + ' 条'}")
    for item in FAILED:
        print(f"  - {item}")
    return 1 if FAILED else 0


def _cleanup(db, *, user_ids: list[int], knowledge_base_ids: list[int]) -> None:
    """删本次产生的行（顺序：消息 → 运行 → 题目/练习 → 掌握度 → 库 → 会话 → 用户）。

    先 `rollback()` 结束可能还开着的读事务，否则 REPEATABLE READ 的旧快照会让
    下面的 `select` 看不到新写入的行（实测会漏删 practice_sets → 删库撞 1451）。

    库**按 `user_ids` 现查现删**：调用方手里那份 `knowledge_base_ids` 是在用例开头
    收集的，脚本中段新建的库（例如本轮为验考纲优先级建的 kb_outline）不在里面，
    漏掉就会在删用户时撞 `knowledge_bases_ibfk_1`（第 25 轮实测踩到）。
    """
    db.rollback()
    ids = [item for item in user_ids if item]
    if ids:
        db.execute(delete(ChatMessage).where(ChatMessage.user_id.in_(ids)))
        db.execute(delete(WorkflowRun).where(WorkflowRun.user_id.in_(ids)))
    practice_ids = list(db.scalars(select(PracticeSet.id).where(PracticeSet.user_id.in_(ids))))
    question_ids = (
        list(db.scalars(select(Question.id).where(Question.practice_set_id.in_(practice_ids))))
        if practice_ids
        else []
    )
    if question_ids:
        from app.db.models import Attempt

        db.execute(delete(Attempt).where(Attempt.question_id.in_(question_ids)))
        db.execute(delete(Question).where(Question.id.in_(question_ids)))
    if practice_ids:
        db.execute(delete(PracticeSet).where(PracticeSet.id.in_(practice_ids)))
    db.execute(delete(UserKnowledgeMastery).where(UserKnowledgeMastery.user_id.in_(ids)))
    db.execute(delete(UserProfile).where(UserProfile.user_id.in_(ids)))
    owned_kb_ids = set(int(item) for item in knowledge_base_ids)
    if ids:
        owned_kb_ids |= set(
            int(item) for item in db.scalars(
                select(KnowledgeBase.id).where(KnowledgeBase.user_id.in_(ids))
            )
        )
    if owned_kb_ids:
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(sorted(owned_kb_ids))))
    db.execute(delete(Session).where(Session.user_id.in_(ids)))
    db.execute(delete(User).where(User.id.in_(ids)))
    db.commit()


if __name__ == "__main__":
    sys.exit(main())

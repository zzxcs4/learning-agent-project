"""难点讲解一期验收：知识卡结构化 → 出图 Prompt → 落库消息 → 读回现签 URL。

设计口径（与 `verify_kb_documents.py` 一致）：

- **不调真模型、不调真 Seedream**：`DifficultyLearnerAgent` 的 `llm.invoke` 与
  `generate_image_bytes` 都被替换成假实现。验收要证明的是**我们这条链路的编排**，
  而不是"第三方服务今天通不通"（那件事该由 `probe_*` 脚本去测）。
- **不 mock 数据库**：真建用户/会话/练习/错题，真跑 SQL。踩坑记录第 1 条（MySQL
  REPEATABLE READ）要求长事务里读不到后台新状态，所以每次读状态前先 `commit()`。
- 跑完自清理（`--cleanup` 默认开，`--keep` 保留现场排查）。

覆盖的断言（按链路顺序）：

1. 素材：最近一次完成练习的错题 + 薄弱点会被带进模型素材（提示词里能看到）；
2. 卡结构：模型输出被归一成 `title/topic/summary/points(≤3)/pitfall/correct/visual_hint`；
3. 出图 Prompt：含标题、难点、误区、正确理解与"不要水印/气泡对话"那段收尾；
4. 出图成功路径：图片进 COS（假上传）→ 段落落库 → 载荷带 `degraded=False`；
5. 降级路径：没配 `SEEDREAM_API_KEY` 时**只出卡、不出图**，讲解照常成功，
   且载荷带**可行动的原因**（`degraded_reason`，不含 SDK 原文/Request id）；
6. 读回：`ChatService.get_session_messages` 返回的图片 URL 是**现签**的（与落库 URL 不同）；
7. 前端认领口径：`WORKFLOW_ROUTE_RULES["difficulty_learner"]` 存在且带 `讲解` 关键词；
8. 路由兜底：**显式选了难点讲解 + 只写一个主题（无动词）也要进讲解工作流**
   （真实踩坑：`"大模型参数的演化"` 曾掉进普通聊天），同时钉住出题侧零回归。

用法（在 `agent-backend-project` 下）：

    python scripts/verify_difficulty_card.py            # 全跑 + 自清理
    python scripts/verify_difficulty_card.py --keep      # 保留测试数据
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):  # pragma: no cover
        pass

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


class _FakeMessage:
    """假的模型返回：只需要 `content` 能被 `_message_text` 读出来。"""

    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """假模型：把预设 JSON 当回答返回，并记下收到的提示词供断言。"""

    def __init__(self, payload: str):
        self.payload = payload
        self.prompts: list[str] = []

    def invoke(self, prompt):
        self.prompts.append(prompt if isinstance(prompt, str) else str(prompt))
        return _FakeMessage(self.payload)


CARD_JSON = """
```json
{
  "title": "GIL 与并发",
  "topic": "Python 并发",
  "summary": "GIL 让同一时刻只有一个线程执行字节码，所以多线程不适合 CPU 密集任务。",
  "points": [
    {"title": "本质", "tip": "解释器级的互斥锁，不是语言规范"},
    {"title": "影响", "tip": "CPU 密集任务无法靠多线程并行"},
    {"title": "例外", "tip": "IO 阻塞时会释放 GIL"},
    {"title": "这条会被丢掉", "tip": "超过 3 条的要被截断"}
  ],
  "pitfall": ["以为多线程一定更快", "把 GIL 当成语法限制"],
  "correct": "CPU 密集用多进程，IO 密集用多线程或协程。",
  "visual_hint": "左右对比：多线程串行 vs 多进程并行"
}
```
"""


def _fake_image_bytes(prompt: str = "") -> tuple[bytes, str]:
    """伪造一张 PNG（内容不重要，但要是**确定性**的，方便断言去重与哈希）。

    签名必须与真实现一致（`generate_image_bytes(prompt)`）：被替换的是**服务模块里**
    那个名字，服务是按位置传 prompt 调的。
    """
    payload = b"\x89PNG\r\n\x1a\n" + b"difficulty-card-verify" * 4
    return payload, ".png"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="难点讲解一期验收")
    parser.add_argument("--keep", action="store_true", help="保留测试数据（默认自清理）")
    args = parser.parse_args(argv[1:])

    from sqlalchemy import select

    from app.agent.difficulty_learner import (
        DifficultyLearnerAgent,
        build_material,
        normalize_card,
    )
    from app.core.settings import settings
    from app.db import SessionLocal
    from app.db.models import (
        Attempt,
        ChatMessage,
        KnowledgeBase,
        PracticeSet,
        Question,
        Resource,
        Session as SessionModel,
        User,
        WorkflowRun,
    )
    from app.services.chat_service import ChatService
    from app.services.difficulty_learner_service import (
        DIFFICULTY_WORKFLOW_TYPE,
        MESSAGE_SUFFIX,
        DifficultyLearnerService,
    )
    from app.services import difficulty_learner_service as dls
    from app.services import seedream_image_service as sis

    db = SessionLocal()
    suffix = uuid.uuid4().hex[:8]
    username = f"vdif{suffix}"  # users.username 是 varchar(20)，临时名必须短
    created: dict[str, int] = {}
    failures: list[str] = []

    # ---------------------------------------------------------------- 假模型接线
    fake_llm = _FakeLLM(CARD_JSON)
    agent = DifficultyLearnerAgent(llm=fake_llm)

    real_generate = sis.generate_image_bytes
    real_setting_key = settings.seedream_api_key

    try:
        # ------------------------------------------------------------ 夹具
        print("\n[0] 建立夹具（用户 / 会话 / 一次已完成练习 + 错题）")
        user = User(username=username, password="verify-not-a-real-hash")
        # 兼容 users 表可能存在的必填列：用反射结果只填必要字段
        db.add(user)
        db.commit()
        db.refresh(user)
        created["user"] = user.id

        session = SessionModel(user_id=user.id, session_model=0, title="难点讲解验收")
        db.add(session)
        db.commit()
        db.refresh(session)
        created["session"] = session.id

        practice = PracticeSet(
            user_id=user.id,
            knowledge_base_id=None,
            run_id=f"verify-{suffix}",
            title="GIL 专项练习",
            status="completed",
            total_score=50.0,
            question_count=2,
        )
        db.add(practice)
        db.commit()
        db.refresh(practice)
        created["practice"] = practice.id

        wrong = Question(
            practice_set_id=practice.id,
            question_code="q1",
            focus_area="GIL",
            question_type="textarea",
            stem="为什么 CPU 密集任务用多线程反而更慢？",
            points=50,
            difficulty=3.0,
        )
        right = Question(
            practice_set_id=practice.id,
            question_code="q2",
            focus_area="GIL",
            question_type="textarea",
            stem="IO 密集任务在 GIL 下表现如何？",
            points=50,
            difficulty=2.0,
        )
        db.add_all([wrong, right])
        db.commit()
        db.refresh(wrong)
        db.refresh(right)

        db.add_all(
            [
                Attempt(
                    question_id=wrong.id,
                    user_id=user.id,
                    answer={"value": "因为线程切换开销大"},
                    score=20.0,
                    is_correct=False,
                    analysis="没抓住 GIL 是解释器级互斥锁这一点。",
                ),
                Attempt(
                    question_id=right.id,
                    user_id=user.id,
                    answer={"value": "基本不受影响"},
                    score=80.0,
                    is_correct=True,
                    analysis="正确。",
                ),
            ]
        )
        db.commit()
        db.commit()  # 结束读事务：后台/后续读要看到上面写入的状态
        check("夹具就绪", bool(user.id and session.id and practice.id))

        # ------------------------------------------------------------ 1. 素材
        print("\n[1] 素材组装（用户这句话为主 + 最近练习错题为辅）")
        material = build_material(
            db,
            user_id=user.id,
            session_id=session.id,
            request_text="讲讲 GIL 为什么让多线程变慢",
            knowledge_base_id=None,
        )
        check("带上了最近一次完成的练习", (material.get("practice") or {}).get("id") == practice.id
              or (material.get("practice") or {}).get("practice_set_id") == practice.id)
        wrong_stems = [item.get("stem") for item in material.get("wrong_questions") or []]
        check(
            "只带做错的题（做对的那道不进素材）",
            wrong_stems == ["为什么 CPU 密集任务用多线程反而更慢？"],
            str(wrong_stems),
        )
        check(
            "错题带上了点评",
            (material.get("wrong_questions") or [{}])[0].get("analysis", "").startswith("没抓住"),
        )

        # ------------------------------------------------------------ 2. 卡结构
        print("\n[2] 知识卡结构化（含 4 条 points 的截断）")
        card = agent.prepare_card(material)
        check("标题来自模型", card.get("title") == "GIL 与并发", str(card.get("title")))
        check("points 截断到 3 条", len(card.get("points") or []) == 3, str(len(card.get("points") or [])))
        check(
            "pitfall 归一成 list（多条误区不丢）",
            isinstance(card.get("pitfall"), list) and len(card["pitfall"]) == 2,
            str(card.get("pitfall")),
        )
        check("summary 非空", bool(card.get("summary")))
        check("提示词里带上了错题（素材进了模型）", "多线程反而更慢" in (fake_llm.prompts[-1] if fake_llm.prompts else ""))

        # ------------------------------------------------------------ 3. 出图 Prompt
        print("\n[3] 图像 Prompt 拼装")
        prompt = DifficultyLearnerAgent.build_image_prompt(card)
        for needle in ("竖版中文学习知识卡", "GIL 与并发", "0.本质", "误区", "正确"):
            check(f"Prompt 含「{needle}」", needle in prompt)
        for needle in ("不要漫画分镜", "水印", "少字大字"):
            check(f"Prompt 含收尾约束「{needle}」", needle in prompt)

        # 字段缺失也不能拼出空块 / 抛异常
        empty_prompt = DifficultyLearnerAgent.build_image_prompt({"points": [{"title": "只有标题"}]})
        check("字段全缺时 Prompt 仍可拼出", "知识卡" in empty_prompt and "只有标题" in empty_prompt)
        check("normalize_card 容忍非 dict 入参", normalize_card(None) == {})  # type: ignore[arg-type]

        # ------------------------------------------------------------ 4. 出图成功路径
        print("\n[4] 出图成功路径（假 Seedream + 假 COS 上传）")
        uploaded: dict[str, object] = {}

        # 假上传：参数名必须与真实现一字不差（服务是按关键字调的）
        def fake_upload(
            db,
            user,
            file_bytes,
            file_name,
            content_type,
            storage_scene=3,
            knowledge_base_id=None,
        ):
            uploaded["bytes"] = len(file_bytes)
            uploaded["file_name"] = file_name
            uploaded["content_type"] = content_type
            resource = Resource(
                resource_type=1,
                storage_scene=storage_scene,
                upload_purpose=0,
                file_name=file_name,
                file_hash=hashlib.md5(file_bytes).hexdigest(),
                storage_path=f"cos://{settings.cos_bucket}/user_{user.id}/images/{file_name}",
                knowledge_base_id=knowledge_base_id,
                user_id=user.id,
            )
            db.add(resource)
            db.commit()
            db.refresh(resource)
            return resource, False

        from app.services.upload_service_cos import UploadService

        real_save_bytes = UploadService.save_bytes
        real_presign = sis.settings
        UploadService.save_bytes = staticmethod(fake_upload)  # type: ignore[assignment]
        sis.generate_image_bytes = _fake_image_bytes  # type: ignore[assignment]
        dls.generate_image_bytes = _fake_image_bytes  # type: ignore[assignment]

        # 现签 URL 需要真 COS 客户端；这里替换成"可预测的假签名"，
        # 断言的是"读消息时**重新签了一次**"，不是签名算法本身。
        signed_calls: list[str] = []
        sign_seq = {"n": 0}

        def fake_presign_url(storage_path, expires_seconds=3600):
            signed_calls.append(storage_path)
            # 每次调用都换一个签名串（**不能**用 len(signed_calls) 当序号：断言前会把
            # 调用记录清空，序号一归零签名就重复了，这条断言会永远失败）。
            sign_seq["n"] += 1
            digest = hashlib.md5(f"{storage_path}:{sign_seq['n']}".encode()).hexdigest()[:8]
            return f"https://cos.example.com/{Path(storage_path).name}?sign={digest}"

        from app.services.cos_storage_service import CosStorageService

        real_cos_presign = CosStorageService.get_presigned_url
        CosStorageService.get_presigned_url = staticmethod(fake_presign_url)  # type: ignore[assignment]
        dls.CosStorageService.get_presigned_url = staticmethod(fake_presign_url)  # type: ignore[assignment]

        try:
            service = DifficultyLearnerService(db, agent=agent)
            result = service.start(
                user_id=user.id,
                session_id=session.id,
                request_text="讲讲 GIL 为什么让多线程变慢",
            )
            data = result.get("data") or {}
            check("工作流状态 completed", result.get("status") == "completed", str(result.get("status")))
            check("载荷类型 difficulty_card", data.get("type") == "difficulty_card")
            check("本次有配图（degraded=False）", data.get("degraded") is False, str(data.get("degraded")))
            check("图片字节数与假图一致", uploaded.get("bytes") == len(_fake_image_bytes()[0]),
                  str(uploaded.get("bytes")))
            check("落库文件名带 knowledge_card 前缀",
                  str(uploaded.get("file_name", "")).startswith("knowledge_card_"))
            check("资源 id 回传", bool(data.get("resource_id")), str(data.get("resource_id")))

            db.commit()  # 结束读事务再查，避免 REPEATABLE READ 读不到新行
            row = db.scalar(
                select(ChatMessage).where(
                    ChatMessage.session_id == session.id,
                    ChatMessage.request_id == f"{result['run_id']}{MESSAGE_SUFFIX}",
                )
            )
            check("知识卡消息已落库", row is not None)
            check("request_id 后缀是 :difficulty（前端按它认领）",
                  bool(row) and row.request_id.endswith(":difficulty"))
            segments = (row.response_segments if row else None) or []
            check("附件段是 image 类型", bool(segments) and segments[0].get("type") == "image")
            check("附件段落库保留 cos:// 路径（供现签）",
                  bool(segments) and str(segments[0].get("storage_path", "")).startswith("cos://"),
                  str(segments[0].get("storage_path") if segments else ""))
            check("附件段带 resource_id（删会话时能回收对象）",
                  bool(segments) and isinstance(segments[0].get("resource_id"), int))

            run = db.scalar(select(WorkflowRun).where(WorkflowRun.run_id == result["run_id"]))
            check("workflow_runs 记录 completed 且类型正确",
                  bool(run) and run.status == "completed" and run.workflow_type == DIFFICULTY_WORKFLOW_TYPE,
                  f"{getattr(run, 'status', None)}/{getattr(run, 'workflow_type', None)}")

            # -------------------------------------------------------- 5. 读回现签
            print("\n[5] 历史读回：图片 URL 必须是**现签**的")
            signed_calls.clear()
            messages, _ = ChatService.get_session_messages(
                db, user_id=user.id, session_id=session.id, page=1, page_size=20
            )
            card_message = next((m for m in messages if m.request_id.endswith(":difficulty")), None)
            check("历史消息里有知识卡那条", card_message is not None)
            urls = [seg.url for seg in (card_message.response_segments if card_message else [])]
            check("读回时重新签了一次 URL", len(signed_calls) == 1, str(signed_calls))
            check("返回的 URL 是新的签名地址",
                  bool(urls) and urls[0] != segments[0].get("url"),
                  f"{urls[0] if urls else None} vs {segments[0].get('url') if segments else None}")
            check("storage_path 随消息一起回传（幂等重签需要它）",
                  bool(card_message and card_message.response_segments)
                  and bool(card_message.response_segments[0].storage_path))
            check("载荷里的卡结构完整",
                  bool(card_message and card_message.response_payload)
                  and (card_message.response_payload or {}).get("card", {}).get("title") == "GIL 与并发")

            # -------------------------------------------------------- 6. 降级路径
            print("\n[6] 降级路径：没配 SEEDREAM_API_KEY 时只出卡不出图")
            sis.generate_image_bytes = real_generate  # 恢复真实现（它内部会因缺 Key 抛 Unavailable）
            dls.generate_image_bytes = real_generate  # type: ignore[assignment]
            object.__setattr__(settings, "seedream_api_key", "")
            degraded = service.start(
                user_id=user.id,
                session_id=session.id,
                request_text="再讲讲协程和 GIL 的关系",
            )
            ddata = degraded.get("data") or {}
            check("降级后依然 completed", degraded.get("status") == "completed")
            check("降级标记为 True", ddata.get("degraded") is True, str(ddata.get("degraded")))
            check("降级时没有 resource_id", ddata.get("resource_id") in (None, ""))
            db.commit()
            degraded_row = db.scalar(
                select(ChatMessage).where(
                    ChatMessage.session_id == session.id,
                    ChatMessage.request_id == f"{degraded['run_id']}{MESSAGE_SUFFIX}",
                )
            )
            check("降级时消息照常落库（卡内容不丢）", degraded_row is not None)
            check("降级时附件段为空",
                  not (degraded_row.response_segments if degraded_row else None))
            check("降级时载荷仍带完整卡",
                  bool(degraded_row and degraded_row.response_payload)
                  and bool((degraded_row.response_payload or {}).get("card", {}).get("summary")))
            # 静默降级是踩过的坑：没图时必须能告诉用户"为什么"（缺 Key / 额度耗尽 /
            # 模型 ID 错，三者要做的事完全不同），所以这个字段必须有值且不是原始报错串。
            degraded_reason = str(
                (degraded_row.response_payload or {}).get("degraded_reason") or ""
            )
            check("降级时载荷带可行动的原因", bool(degraded_reason), degraded_reason)
            check("原因里不含 SDK 原文/Request id",
                  "Error code" not in degraded_reason and "Request id" not in degraded_reason,
                  degraded_reason)
        finally:
            UploadService.save_bytes = real_save_bytes  # type: ignore[assignment]
            CosStorageService.get_presigned_url = real_cos_presign  # type: ignore[assignment]
            dls.CosStorageService.get_presigned_url = real_cos_presign  # type: ignore[assignment]
            object.__setattr__(settings, "seedream_api_key", real_setting_key)

        # ------------------------------------------------------------ 7. 路由口径
        print("\n[7] 路由口径：前端选「难点讲解」必须命中工作流")
        from app.services.orchestrator_service import (
            WORKFLOW_ROUTE_RULES,
            OrchestratorService,
        )

        rule = WORKFLOW_ROUTE_RULES.get("difficulty_learner")
        check("difficulty_learner 配了路由规则", rule is not None)
        check("工作流类型是 difficulty_card",
              bool(rule) and rule.workflow_type == DIFFICULTY_WORKFLOW_TYPE,
              getattr(rule, "workflow_type", None))
        check("关键词里有「讲解」", bool(rule) and "讲解" in rule.required_keywords)

        # ---- 用户显式选了「难点讲解」但输入框里只有一个主题（没有动词） ----
        #
        # 真实踩坑：`agent_name=difficulty_learner` + `request_text="大模型参数的演化"`。
        # 这句话一个关键词都不含，语义兜底又因为嵌入模型加载失败而恒返回 chat，
        # 于是请求被当成普通聊天 —— 用户点了「难点讲解」却拿不到知识卡。
        # 所以必须断言：**显式选择 + 纯主题输入**也要进 difficulty_card 工作流。
        orch = OrchestratorService.__new__(OrchestratorService)  # route 不用 db
        for text in ("大模型参数的演化", "什么是向量数据库"):
            r = orch.route("difficulty_learner", text)
            check(
                f"纯主题输入也进讲解工作流：{text}",
                r.get("workflow_type") == DIFFICULTY_WORKFLOW_TYPE,
                f"route_type={r.get('route_type')} reason={r.get('reason')}",
            )

        # 出题侧必须零回归：它没有 force_workflow，没有出题意图时仍走普通聊天
        r_quiz = orch.route("quiz_generate_workflow", "什么是向量数据库")
        check("出题侧零回归（无出题意图仍走聊天）",
              r_quiz.get("route_type") == "chat",
              f"{r_quiz.get('route_type')} reason={r_quiz.get('reason')}")
        r_quiz2 = orch.route("quiz_generate_workflow", "帮我出题")
        check("出题侧关键词仍命中工作流",
              r_quiz2.get("route_type") == "workflow",
              f"{r_quiz2.get('route_type')} reason={r_quiz2.get('reason')}")

        check("讲解侧开了 force_workflow 兜底", bool(rule) and rule.force_workflow is True)
        check("出题侧**没有**开 force_workflow",
              WORKFLOW_ROUTE_RULES["quiz_generate_workflow"].force_workflow is False)

        # ------------------------------------------------------------ 8. 知识库
        #
        # 难点讲解的知识库上下文由 `_knowledge_context` 组装：**检索层就按 resource_ids
        # 收窄到本库**，再拼上考纲。这里钉住三件事：
        # ① 不选库时一个字都不取（"不选就按模型知识"）；
        # ② 选了库时，检索调用**必须**带 resource_ids（曾经是全局取 top-3 再在 Python 里
        #    丢掉别库命中——本库明明有切片也会被别库高分挤掉名额，静默少给资料）；
        # ③ 检索结果真的进了送模型的素材文本。
        print("\n[8] 知识库：选了库要按本库检索，不选库则完全不用")
        from app.agent.difficulty_learner import _knowledge_context, render_material
        from app.db.models import KnowledgeBase
        from app.rag import retriever as retriever_mod

        kb = KnowledgeBase(user_id=user.id, name=f"验收库-{suffix}", description="")
        db.add(kb)
        db.commit()
        db.refresh(kb)
        created["knowledge_base"] = kb.id

        in_kb = Resource(
            resource_type=1,
            storage_scene=3,
            upload_purpose=0,
            file_name=f"in_kb_{suffix}.md",
            file_hash=hashlib.md5(f"in-{suffix}".encode()).hexdigest(),
            storage_path=f"cos://{settings.cos_bucket}/user_{user.id}/kb/in_kb.md",
            knowledge_base_id=kb.id,
            user_id=user.id,
        )
        other_kb_resource = Resource(
            resource_type=1,
            storage_scene=3,
            upload_purpose=0,
            file_name=f"other_{suffix}.md",
            file_hash=hashlib.md5(f"other-{suffix}".encode()).hexdigest(),
            storage_path=f"cos://{settings.cos_bucket}/user_{user.id}/kb/other.md",
            knowledge_base_id=None,
            user_id=user.id,
        )
        db.add_all([in_kb, other_kb_resource])
        db.commit()
        db.refresh(in_kb)
        db.refresh(other_kb_resource)

        # ① 不选库：一个字都不取，且检索**根本不该被调用**
        calls: list[dict] = []

        def fake_search_similar_chunks(query, user_id, db, top_k=5, resource_ids=None):
            calls.append({"query": query, "top_k": top_k, "resource_ids": resource_ids})
            hits = [
                {"resource_id": in_kb.id, "text": f"本库原文-{suffix}", "score": 0.9},
                {"resource_id": other_kb_resource.id, "text": f"别处原文-{suffix}", "score": 0.95},
            ]
            # 真实检索器会按 resource_ids 在 MySQL 侧过滤；假实现照这个语义来
            if resource_ids is not None:
                allowed = set(resource_ids)
                hits = [h for h in hits if h.get("resource_id") in allowed]
            return hits[:top_k]

        real_search = retriever_mod.search_similar_chunks
        retriever_mod.search_similar_chunks = fake_search_similar_chunks  # type: ignore[assignment]
        try:
            no_kb_context = _knowledge_context(
                db, knowledge_base_id=None, request_text="大模型参数的演化"
            )
            check("不选库时不取任何资料", no_kb_context == "", repr(no_kb_context)[:60])
            check("不选库时检索根本没被调用", not calls, f"被调用了 {len(calls)} 次")

            # ② + ③ 选了库：带 resource_ids 检索，且结果进上下文
            calls.clear()
            kb_context = _knowledge_context(
                db, knowledge_base_id=kb.id, request_text="大模型参数的演化"
            )
            check("选库后确实调用了检索", len(calls) == 1, f"{len(calls)} 次")
            check(
                "检索层就收窄到本库（带 resource_ids）",
                calls and calls[0]["resource_ids"] == [in_kb.id],
                f"resource_ids={calls[0]['resource_ids'] if calls else None}",
            )
            check("本库原文进了上下文", f"本库原文-{suffix}" in kb_context)
            check(
                "别库原文**没有**混进上下文",
                f"别处原文-{suffix}" not in kb_context,
                "post-filter 之外还必须靠检索层过滤",
            )
            check("检索用的就是用户这句话", calls and calls[0]["query"] == "大模型参数的演化")

            # 素材 → 提示词文本：知识库上下文要真的出现在渲染结果里
            material = build_material(
                db,
                user_id=user.id,
                session_id=session.id,
                request_text="大模型参数的演化",
                knowledge_base_id=kb.id,
            )
            rendered = render_material(material)
            check("素材里带上了知识库上下文", f"本库原文-{suffix}" in rendered)
        finally:
            retriever_mod.search_similar_chunks = real_search  # type: ignore[assignment]

    except Exception as exc:  # noqa: BLE001 - 验收脚本要把异常变成可读的失败
        import traceback

        traceback.print_exc()
        failures.append(f"未捕获异常: {type(exc).__name__}: {exc}")
    finally:
        # ------------------------------------------------------------ 清理
        if not args.keep and created:
            print("\n[9] 清理测试数据")
            try:
                # 踩坑记录第 2/3 条：本项目没有 ORM relationship，删除顺序必须自己排
                _cleanup(db, created)
                print("  已清理")
            except Exception as exc:  # noqa: BLE001
                print(f"  清理失败（不影响验收结论）: {exc}")
        db.close()

    # ---------------------------------------------------------------- 汇总
    failed = [item for item in CHECKS if not item[1]]
    print(f"\n{'=' * 60}")
    print(f"断言总数 {len(CHECKS)}，通过 {len(CHECKS) - len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  FAIL  {name}  [{detail}]")
    for item in failures:
        print(f"  异常  {item}")
    print("OK" if not failed and not failures else "NG")
    return 0 if not failed and not failures else 1


def _cleanup(db, created: dict[str, int]) -> None:
    """删掉本脚本造的测试数据（先子表后父表）。"""
    from sqlalchemy import delete, select

    from app.db.models import (
        Attempt,
        ChatMessage,
        KnowledgeBase,
        PracticeSet,
        Question,
        Resource,
        Session as SessionModel,
        User,
        WorkflowRun,
    )

    user_id = created.get("user")
    session_id = created.get("session")
    practice_id = created.get("practice")
    if not user_id:
        return

    if practice_id:
        question_ids = select(Question.id).where(Question.practice_set_id == practice_id)
        db.execute(delete(Attempt).where(Attempt.question_id.in_(question_ids)))
        db.execute(delete(Question).where(Question.practice_set_id == practice_id))
        db.execute(delete(PracticeSet).where(PracticeSet.id == practice_id))
    if session_id:
        db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
        db.execute(delete(WorkflowRun).where(WorkflowRun.session_id == session_id))
        db.execute(delete(SessionModel).where(SessionModel.id == session_id))
    # 资源：本脚本生成的图（storage_path 指向 COS，但假上传并没有真对象，直接删行即可）
    db.execute(delete(Resource).where(Resource.user_id == user_id, Resource.resource_type == 1))
    db.commit()
    # 知识库在第 8 段才建，所以按 created 里的记录删（不能无条件删用户的所有库）
    knowledge_base_id = created.get("knowledge_base")
    if knowledge_base_id:
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == knowledge_base_id))
        db.commit()
    db.execute(delete(User).where(User.id == user_id))
    db.commit()


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""难点讲解服务（第一期：一张知识卡 + 文字讲解，不再做 TTS 音频）。

链路（每一步都可单独验收）：

```
用户这句话 + 最近一次完成练习的错题/薄弱点 + 知识库资料
   → DifficultyLearnerAgent.prepare_card      结构化知识卡（JSON）
   → DifficultyLearnerAgent.build_image_prompt 拼图像 Prompt
   → seedream_image_service.generate_image_bytes  出图（**没配 Key 就跳过，不报错**）
   → UploadService.save_bytes                存 COS + 落 resources
   → CosStorageService.get_presigned_url      拼可访问地址
   → chat_messages（response_segments 挂图 + response_payload 挂结构化卡）
```

为什么出图失败不算失败：讲解的**内容价值**在文字与卡结构里，图只是表现形式。
没配 `SEEDREAM_API_KEY`、出图超时、COS 上传失败，都只降级成"这次没有图"，
文字讲解与结构化卡照常落库——用户不会因为一个可选的渲染步骤拿不到讲解。

为什么消息的 `response_segments[].storage_path` 要存 `cos://` 路径：预签名 URL 默认
1 小时过期，只存 URL 的话历史消息里的知识卡过一会儿就变占位图。读消息时由
`ChatService._build_segments` 现签一个新鲜地址（见那里）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.agent.difficulty_learner import (
    DifficultyLearnerAgent,
    build_material,
    to_payload,
)
from app.core import raise_error
from app.core.logging_config import main_logger
from app.db.models import ChatMessage, User
from app.services.cos_storage_service import CosStorageService
from app.services.retry_service import RetryExecutor
from app.services.seedream_image_service import (
    ImageGenerationUnavailable,
    describe_failure,
    generate_image_bytes,
)
from app.services.session_service import SessionService
from app.services.upload_service_cos import UploadService
from app.services.workflow_run_service import WorkflowRunService

# 前端智能体名与工作流类型（`AGENT_CONFIG` 里的键必须完全一致）
DIFFICULTY_LEARNER_AGENT = "difficulty_learner"
DIFFICULTY_WORKFLOW_TYPE = "difficulty_card"

# 消息归属：与出题消息（`{run_id}:quiz`）同一套认领口径——前端靠 `{run_id}:difficulty`
# 在前端认领"这一轮新出的知识卡"，所以后缀不要改。
MESSAGE_SUFFIX = ":difficulty"
# 学习会话（`session_model=0`）是本项目所有工作流消息的落点；笔记会话不接受讲解
LEARNING_SESSION_MODEL = 0
# 出图后落库的文件名前缀（前端附件显示名也用这个）
IMAGE_FILE_STEM = "knowledge_card"

# 提示词"尚未查询"的哨兵：与 None（查过但没有模板）必须区分开，否则每个实例都会重查
_UNSET = object()


class DifficultyLearnerService:
    """编排难点讲解一期：出一张知识卡（+ 可选配图）并写进会话。"""

    def __init__(
        self,
        db: Session,
        agent: DifficultyLearnerAgent | None = None,
        *,
        provider: str = "deepseek",
    ):
        self.db = db
        self.provider = provider
        self.agent = agent or DifficultyLearnerAgent(provider=provider)
        # 角色提示词按**实例**缓存：一次讲解只查一次模板。
        # 用实例而不是模块级全局，是为了让"改了模板立刻生效"这件事在长驻进程里也可预期
        # （全局缓存会把第一次的模板钉死到进程结束，运营改完看不到效果）。
        self._card_prompt: Any = _UNSET

    # ------------------------------------------------------------------ 主入口
    def start(
        self,
        *,
        user_id: int,
        session_id: int,
        request_text: str,
        session_model: int = LEARNING_SESSION_MODEL,
        knowledge_base_id: int | None = None,
        provider: str = "deepseek",
        references: str = "",
    ) -> dict[str, Any]:
        """跑完"取素材 → 结构化 → 出图 → 落库"，返回给前端可直接用的结果。

        返回 `{"run_id", "status", "data": {type, card, image_url, resource_id, degraded}}`：
        `degraded=True` 表示这次没有图（没配 Key / 出图失败），前端照常展示文字与卡结构。

        `references` 是**额外资料**（agent loop 里工具检索到的原文）：它进模型素材，
        但**不进 `request_text`**——落库的 `request_text` 必须还是用户原话，否则会话里
        会出现一条又长又不像人话的"用户消息"。
        """
        clean_text = (request_text or "").strip()
        if not clean_text:
            raise_error(code=400, message="请先说明要讲解的难点")

        # 会话归属校验放在最前：讲解消息写进别人的会话是最坏的一类 bug
        session = SessionService.get_session_or_404(
            db=self.db,
            user_id=user_id,
            session_id=session_id,
            session_model=session_model,
        )
        user = self.db.get(User, user_id)
        if user is None:
            raise_error(code=404, message="用户不存在")

        run_id = uuid.uuid4().hex
        state: dict[str, Any] = {
            "run_id": run_id,
            "user_id": user_id,
            "session_id": session.id,
            "request_text": clean_text,
            "knowledge_base_id": knowledge_base_id,
            "provider": provider or self.provider,
            "status": "created",
        }
        run = WorkflowRunService.create_run(
            db=self.db,
            user_id=user_id,
            session_id=session.id,
            workflow_type=DIFFICULTY_WORKFLOW_TYPE,
            status="created",
            state=state,
            run_id=run_id,
        )

        try:
            material = build_material(
                self.db,
                user_id=user_id,
                session_id=session.id,
                request_text=clean_text,
                knowledge_base_id=knowledge_base_id,
                references=references,
            )
            card = RetryExecutor.run(
                state=state,
                node_name="prepare_card",
                operation=lambda: self.agent.prepare_card(
                    material, system_prompt=self._resolve_card_prompt()
                ),
            )
            state["card"] = card
            state["has_material"] = bool(
                material.get("wrong_questions") or material.get("weak_points")
            )

            image = self._render_and_store_image(
                state=state, user=user, run_id=run_id, card=card
            )
            payload = to_payload(card)
            state["resource_id"] = image.get("resource_id")
            state["degraded"] = image.get("degraded")
            state["image_reason"] = image.get("reason")
            state["status"] = "completed"

            self._create_message(
                user_id=user_id,
                session_id=session.id,
                request_id=f"{run_id}{MESSAGE_SUFFIX}",
                request_text=clean_text,
                response_text=str(payload.get("summary") or payload.get("title") or "知识卡"),
                payload={
                    "type": "difficulty_card",
                    "version": 1,
                    "run_id": run_id,
                    "status": "completed",
                    "card": payload,
                    "image_url": image.get("image_url"),
                    "resource_id": image.get("resource_id"),
                    "degraded": bool(image.get("degraded")),
                    # 没出图时把**可行动的原因**一并落库：出图失败是静默降级，
                    # 不落这个字段用户只能看到"没有图"，然后来问人（这正是踩过的坑）。
                    "degraded_reason": _degraded_reason(image),
                },
                segments=image.get("segments") or [],
            )
            WorkflowRunService.update_run(self.db, run, "completed", state)
        except Exception as exc:  # noqa: BLE001 - 统一转成工作流失败状态再抛
            main_logger.warning(f"[难点讲解] 失败: run_id={run_id}, error={exc}")
            WorkflowRunService.mark_failed(self.db, run, state, "difficulty_card", str(exc))
            raise

        print(
            f"[难点讲解] 完成: run={run_id} resource={image.get('resource_id')} "
            f"degraded={image.get('degraded')}"
        )
        return {
            "run_id": run_id,
            "status": "completed",
            "data": {
                "type": "difficulty_card",
                "card": payload,
                "image_url": image.get("image_url"),
                "resource_id": image.get("resource_id"),
                "degraded": bool(image.get("degraded")),
                "degraded_reason": _degraded_reason(image),
            },
        }

    # ------------------------------------------------------------------ 出图 + 落库
    def _render_and_store_image(
        self, *, state: dict[str, Any], user: User, run_id: str, card: dict[str, Any]
    ) -> dict[str, Any]:
        """出图并存 COS；**任何一步失败都返回 degraded 结果，不抛异常**。

        出图是整条链路里唯一"贵且可能不可用"的一步（网络 + 第三方服务 + 数十秒），
        所以它的失败必须被隔离在这里：调用方拿到的是"这次没有图"，而不是整条讲解失败。
        """
        prompt = DifficultyLearnerAgent.build_image_prompt(card)
        state["image_prompt"] = prompt

        try:
            image_bytes, suffix = RetryExecutor.run(
                state=state,
                node_name="generate_image",
                operation=lambda: generate_image_bytes(prompt),
            )
        except ImageGenerationUnavailable as exc:
            main_logger.warning(f"[难点讲解] 出图不可用，降级为纯文字: {exc}")
            return {"degraded": True, "reason": str(exc), "segments": []}
        except Exception as exc:  # noqa: BLE001 - 非"能力不可用"的异常同样不阻断讲解
            main_logger.warning(f"[难点讲解] 出图异常，降级为纯文字: {exc}")
            return {"degraded": True, "reason": str(exc), "segments": []}

        file_ext = suffix if suffix.startswith(".") else f".{suffix}"
        try:
            resource, _ = UploadService.save_bytes(
                db=self.db,
                user=user,
                file_bytes=image_bytes,
                file_name=f"{IMAGE_FILE_STEM}_{run_id}{file_ext}",
                content_type=_content_type_for(file_ext),
            )
        except Exception as exc:  # noqa: BLE001 - 存不下来也不该让讲解失败
            main_logger.warning(f"[难点讲解] 知识卡图片入库失败，降级为纯文字: {exc}")
            return {"degraded": True, "reason": f"图片入库失败: {exc}", "segments": []}

        image_url = CosStorageService.get_presigned_url(resource.storage_path)
        return {
            "degraded": False,
            "reason": "",
            "resource_id": resource.id,
            "image_url": image_url,
            "segments": [
                {
                    "type": "image",
                    "name": resource.file_name,
                    "url": image_url,
                    "resource_id": resource.id,
                    # 落库保留 cos:// 路径：读消息时靠它现签新鲜 URL（URL 1 小时就过期）
                    "storage_path": resource.storage_path,
                }
            ],
        }

    # ------------------------------------------------------------------ 落库
    def _create_message(
        self,
        *,
        user_id: int,
        session_id: int,
        request_id: str,
        request_text: str,
        response_text: str,
        payload: dict[str, Any],
        segments: list[dict[str, Any]],
    ) -> ChatMessage:
        """写一条知识卡消息。

        `select_model=1`（知识精讲）与既有的 0/2 区分开；`response_text` 只放**一句话**
        （卡片正文在 payload、图在 segments），避免同一份内容在气泡里重复两遍。
        """
        row = ChatMessage(
            user_id=user_id,
            session_id=session_id,
            select_model=1,
            request_id=request_id,
            request_text=request_text,
            response_text=response_text,
            request_segments=[],
            response_segments=segments,
            response_payload=payload,
            status=0,
            file_extracted_text="",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    # ------------------------------------------------------------------ 提示词
    def _resolve_card_prompt(self) -> str | None:
        """取 `prompt_templates` 里 `difficulty_learner:study` 的生效版本；没有返回 None。

        与出题/归档共用同一套模板机制（Redis 缓存 + DB 兜底）。取不到**不报错**：
        内置提示词就是兜底，运营没配模板时功能照常。
        """
        if self._card_prompt is not _UNSET:
            return self._card_prompt
        try:
            from app.services.prompt_template_service import prompt_template_manager

            prompt = prompt_template_manager.get_full_prompt(
                DIFFICULTY_LEARNER_AGENT, "study", self.db
            )
        except Exception as exc:  # noqa: BLE001 - 模板层异常不该让讲解失败
            main_logger.warning(f"[难点讲解] 取角色提示词失败（用内置）: {exc}")
            prompt = None
        self._card_prompt = str(prompt).strip() if prompt else None
        return self._card_prompt

    # ------------------------------------------------------------------ 供 agent loop 复用
    def explain_for_loop(
        self,
        *,
        user_id: int,
        session_id: int,
        topic: str,
        references: str = "",
        knowledge_base_id: int | None = None,
    ) -> dict[str, Any]:
        """agent loop 里 `delegate_explain` 的入口：与前端直连走**同一条**链路。

        为什么不让 loop 自己再写一套：讲解的产出（卡 + 图 + 消息）必须只有一份实现，
        否则"从难点讲解入口进"和"让学习搭档顺带讲一下"会得到两种不同的东西。
        loop 额外带进来的 `references`（工具检索到的原文）在这里当知识库上下文用，
        但**不落库**——它只影响这一次的卡内容。
        """
        text = (topic or "").strip()
        supplied = (references or "").strip()
        result = self.start(
            user_id=user_id,
            session_id=session_id,
            request_text=text,
            provider=self.provider,
            references=supplied,
            # 已经带了补充资料就不再检索（见 build_material 的口径）；
            # 没带资料时用本会话挂载的库检索，与出题侧口径一致。
            knowledge_base_id=None if supplied else knowledge_base_id,
        )
        data = result.get("data") or {}
        card = data.get("card") or {}
        # 回给 loop 的观察**刻意小**：模型会把观察文本原样转述给用户，把整张卡（含
        # visual_hint 这类内部字段）塞进去，用户就会在气泡里看到一坨 JSON。
        # 这里只回"讲的是什么、有几个要点、有没有图"，正文由那条知识卡消息承载。
        return {
            "run_id": result.get("run_id"),
            "topic": topic,
            "title": card.get("title"),
            "summary": card.get("summary"),
            "point_count": len(card.get("points") or []),
            "has_image": not bool(data.get("degraded")),
            "degraded": bool(data.get("degraded")),
            "note": (
                "知识卡已生成并写入本会话（含配图）" if not data.get("degraded")
                else "知识卡已生成并写入本会话（本次未配图）"
            ),
        }


def _degraded_reason(image: dict[str, Any]) -> str:
    """把 `_render_and_store_image` 的内部 reason 变成**给用户看**的一句话。

    没降级就返回空串。有图却还带一句话会让前端没法判断该不该提示。

    这里**再筛一遍** `describe_failure`：内部 reason 可能带着 `（服务端原文: …）`
    （含方舟错误码与 Request id）。那串东西对运维有用、已经进日志了，但甩到用户气泡里
    只是噪音，所以只保留翻译后的那句。
    """
    if not image.get("degraded"):
        return ""
    raw = str(image.get("reason") or "").strip()
    if not raw:
        return "本次未生成配图"
    return describe_failure(raw)


def _content_type_for(file_ext: str) -> str:
    """按后缀给 MIME；不在白名单内按 png 处理（出图只会有 png/jpeg）。"""
    from app.services.upload_service_cos import ALLOWED_EXTENSIONS

    return ALLOWED_EXTENSIONS.get(file_ext.lower(), "image/png")


__all__ = [
    "DIFFICULTY_LEARNER_AGENT",
    "DIFFICULTY_WORKFLOW_TYPE",
    "MESSAGE_SUFFIX",
    "DifficultyLearnerService",
]

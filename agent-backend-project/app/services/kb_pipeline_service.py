"""知识库构建流水线：向量入库 + 考纲提炼（上传后异步执行，状态可查）。

上传接口只做三件事：**存文件、建记录、丢任务**。两个重活都在这里：

1. **向量入库**（解析 → 切片 → embedding → Qdrant + `knowledge_chunks`）——几十秒级；
2. **考纲提炼**（每 ~5 万字 1 次 LLM，按资源幂等替换）——十几秒级。

为什么异步：同步做会让上传请求卡住甚至超时（30 万字以上必然超时），而这两件事
**不阻塞用户使用**——出题对知识库是"可选依赖"，没就绪就退化为只用 `request_text`。

状态机（都写库，前端可轮询）：

```
resources.ingest_status        pending → ingesting → ready / failed(+ingest_error)
knowledge_bases.quiz_status    pending → extracting → ready / empty / failed
```

> 只有 `quiz_status = ready` 的库才有考纲可用；只要有一份资料 `ingest_status = ready`
> 就能检索到原文。
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_config import main_logger
from app.db.database import SessionLocal
from app.db.models import KnowledgeBase, KnowledgeChunk, Resource
from app.services.quiz_core_service import QuizCoreService
from app.services.rag_service import RagService

# 入库状态
INGEST_PENDING = "pending"
INGEST_INGESTING = "ingesting"
INGEST_READY = "ready"
INGEST_FAILED = "failed"

# 考纲状态
QUIZ_PENDING = "pending"
QUIZ_EXTRACTING = "extracting"
QUIZ_READY = "ready"
QUIZ_EMPTY = "empty"
QUIZ_FAILED = "failed"


class KbPipelineService:
    """知识库构建流水线（后台任务入口）。"""

    # ------------------------------------------------------------------ 后台入口
    @staticmethod
    def process_resource(
        resource_id: int, file_bytes: bytes | None = None, *, skip_ingest: bool = False
    ) -> dict[str, Any]:
        """后台任务：入库 + 提炼。自己开/关 session，异常只记日志（绝不抛给请求）。"""
        db = SessionLocal()
        try:
            resource = db.get(Resource, resource_id)
            if resource is None:
                return {"status": "skipped", "reason": "资源不存在"}

            if not skip_ingest:
                ingested = KbPipelineService._ingest(db, resource, file_bytes)
                if not ingested:
                    return {"status": "failed", "stage": "ingest", "resource_id": resource_id}
            else:
                KbPipelineService._mark_quiz_pending(db, resource.knowledge_base_id)

            if not resource.knowledge_base_id:
                return {"status": "ok", "stage": "ingest", "quiz": "skipped: 未归档到知识库"}

            quiz = KbPipelineService._extract_outline(
                db, knowledge_base_id=resource.knowledge_base_id, resource_id=resource_id
            )
            return {"status": "ok", "resource_id": resource_id, "quiz": quiz}
        finally:
            db.close()

    # ------------------------------------------------------------------ 阶段 1：向量入库
    @staticmethod
    def _ingest(db: Session, resource: Resource, file_bytes: bytes | None) -> bool:
        """写状态 → 入库 → 写结果。失败只把状态置 failed，不抛异常。"""
        resource.ingest_status = INGEST_INGESTING
        resource.ingest_error = None
        db.commit()

        try:
            if file_bytes is None:
                raise ValueError("后台任务没有拿到文件内容")
            RagService.ingest_from_bytes(
                db=db,
                file_bytes=file_bytes,
                file_name=resource.file_name,
                user_id=resource.user_id,
                resource_id=resource.id,
            )
        except Exception as exc:  # noqa: BLE001 - 入库失败不能让后台任务崩掉
            db.rollback()
            fresh = db.get(Resource, resource.id)
            if fresh is not None:
                fresh.ingest_status = INGEST_FAILED
                fresh.ingest_error = str(exc)[:500]
                db.commit()
            main_logger.warning(f"向量入库失败 resource_id={resource.id}: {exc}")
            return False

        fresh = db.get(Resource, resource.id)
        if fresh is not None:
            fresh.ingest_status = INGEST_READY
            fresh.ingest_error = None
            db.commit()
        main_logger.info(f"向量入库完成 resource_id={resource.id}")
        return True

    # ------------------------------------------------------------------ 阶段 2：考纲提炼
    @staticmethod
    def _extract_outline(
        db: Session, *, knowledge_base_id: int, resource_id: int
    ) -> str:
        """提炼考纲并把结果映射成 `quiz_status`。"""
        knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
        if knowledge_base is None:
            return QUIZ_FAILED

        knowledge_base.quiz_status = QUIZ_EXTRACTING
        db.commit()

        try:
            result = QuizCoreService.extract_resource(db, resource_id=resource_id)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            fresh = db.get(KnowledgeBase, knowledge_base_id)
            if fresh is not None:
                fresh.quiz_status = QUIZ_FAILED
                db.commit()
            main_logger.warning(f"考纲提炼失败 knowledge_base_id={knowledge_base_id}: {exc}")
            return QUIZ_FAILED

        status = str(result.get("status") or "")
        fresh = db.get(KnowledgeBase, knowledge_base_id)
        if fresh is None:
            return QUIZ_FAILED
        if status == "ok":
            fresh.quiz_status = QUIZ_READY
            fresh.quiz_extracted_at = int(time.time())
        elif status == "empty":
            # 模型没给出可用考纲（例如资料是无结构清单），不算失败
            fresh.quiz_status = QUIZ_EMPTY
            fresh.quiz_extracted_at = int(time.time())
        else:
            fresh.quiz_status = QUIZ_FAILED
        db.commit()
        main_logger.info(f"考纲提炼结束 knowledge_base_id={knowledge_base_id} status={status}")
        return fresh.quiz_status

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def _mark_quiz_pending(db: Session, knowledge_base_id: int | None) -> None:
        if not knowledge_base_id:
            return
        knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
        if knowledge_base is not None:
            knowledge_base.quiz_status = QUIZ_PENDING
            db.commit()

    @staticmethod
    def mark_pending(
        db: Session, *, resource_id: int, previous_knowledge_base_id: int | None = None
    ) -> None:
        """重传 / 移动知识库时重置状态：本资料与受影响的库都要重新构建。

        `previous_knowledge_base_id` 是移动前所属的库——它的考纲里还留着这份资料的章节，
        但资料已经搬走了，所以只能标记为待重新提炼（下一份资料上传或手动重建时修正）。
        """
        resource = db.get(Resource, resource_id)
        if resource is not None:
            resource.ingest_status = INGEST_PENDING
            resource.ingest_error = None
        for knowledge_base_id in (resource.knowledge_base_id if resource else None, previous_knowledge_base_id):
            if knowledge_base_id:
                knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
                if knowledge_base is not None:
                    knowledge_base.quiz_status = QUIZ_PENDING
        db.commit()

    @staticmethod
    def summarize(db: Session, *, knowledge_base_id: int) -> dict[str, Any]:
        """库的状态聚合（给知识库列表/详情接口用）。

        `can_quiz` 决定出题时能否注入考纲；`can_retrieve` 决定能否取到原文。
        """
        knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
        if knowledge_base is None:
            return {}
        statuses = list(
            db.scalars(
                select(Resource.ingest_status).where(
                    Resource.knowledge_base_id == knowledge_base_id
                )
            )
        )
        ready_count = sum(1 for status in statuses if status == INGEST_READY)
        failed_count = sum(1 for status in statuses if status == INGEST_FAILED)
        pending_count = len(statuses) - ready_count - failed_count
        return {
            "resource_count": len(statuses),
            "ready_count": ready_count,
            "failed_count": failed_count,
            "pending_count": pending_count,
            "quiz_status": knowledge_base.quiz_status,
            "quiz_extracted_at": knowledge_base.quiz_extracted_at,
            "can_quiz": knowledge_base.quiz_status == QUIZ_READY,
            "can_retrieve": ready_count > 0,
        }

    @staticmethod
    def chunk_count(db: Session, *, knowledge_base_id: int) -> int:
        """库内切片总数（详情页展示"资料多少字"的近似）。"""
        return int(
            db.scalar(
                select(KnowledgeChunk.id)
                .join(Resource, Resource.id == KnowledgeChunk.resource_id)
                .where(Resource.knowledge_base_id == knowledge_base_id)
                .limit(1)
            )
            or 0
        )

"""知识库服务：增删改查 + 构建状态聚合 + 库内文档阅读。

产品口径（2026-10-17）：**只暴露知识库，不暴露"资料"这一层**。资料是库的内部组成，
用户看到的是"这个库有几份资料、准备到哪一步、能不能开始练"。

产品口径（2026-10-18 补充）：知识库要能**当文档库看**——库里每份资料都能列出来、
打开读正文、必要时跳到原文件预览。因此本服务多了一组**只读**的文档方法
（`list_resources` / `get_resource_detail` / `get_chunk`），它们不改变上面的口径：
文档仍然属于库，只是从"看不见的内部组成"变成"看得见的库内容"。

删除策略（用户未单独拍板，按最保守的实现）：**资料与练习解绑、掌握度记录删除、库行删除**——
不删用户的文件（COS 对象与 `knowledge_chunks` 都保留），因为资料可能还要用于答疑。
"""

from __future__ import annotations

import time
from typing import Any

from qcloud_cos.cos_exception import CosClientError, CosServiceError
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import raise_error
from app.core.logging_config import main_logger
from app.db.models import (
    KnowledgeBase,
    KnowledgeChunk,
    PracticeSet,
    Resource,
    UserKnowledgeMastery,
    UserProfile,
)
from app.rag import delete_vectors_by_ids
from app.rag.merge_text import join_chunks
from app.services.cos_storage_service import CosStorageService
from app.services.kb_pipeline_service import KbPipelineService
from app.services.upload_service_cos import (
    KNOWLEDGE_BASE_NON_DOCUMENT_MESSAGE,
    RESOURCE_TYPE_FILE,
)


SOURCE_STATUS_FIELD = "source_status"
"""考纲章节在**详情响应**里被补上的字段名：原文资料还在不在。

**只加在响应里，不写回库**——考纲是提炼产物，删资料后它一定指向不存在的
`resource_id`；标记成"这份资料已删"比静默失效好，但没必要为此改动库里的 JSON。
"""

DELETE_RETENTION_POLICY = "resources_unbound; practice_history_kept; weak_points_generalized; mastery_removed"
"""删库的保留策略，**随响应返回**，让调用方不必去读源码猜"什么被留、什么被删"。

逐项对应 `KnowledgeBaseService.delete` 的实际行为：
- `resources_unbound`：库内资料**解绑**（切片、Qdrant 向量、原文件全留，答疑仍检索得到）；
  随后可从「未归档文件」列表归档回别的库，或彻底删除；
- `practice_history_kept`：练习记录解绑保留，历史仍可查；
- `weak_points_generalized`：画像里指向本库的弱点改成"通用弱点"，**继续参与注入**
  （对应 `TechSpar-领域删除行为-设计参照.md` §3 的口径）；
- `mastery_removed`：**掌握度是硬删**（掌握度的键就是 `knowledge_base_id`，库没了键就没了）。
  这与参照文档相反（它保留 `topic_mastery`），是本项目刻意选的口径：
  宁可丢掉分数，也不要"重建同名库时历史掌握度静默复活接管新库"。

改这段字符串等于改对外口径，**必须同步** `scripts/verify_kb_delete.py` 的断言。
"""


def _with_source_status(db: Session, quiz_core: Any) -> dict[str, Any] | None:
    """给考纲每个章节补 `source_status`，让前端能给出**可操作**的降级提示。

    取值（只看"这份资料还在不在"，不每次都去数切片——那要按章节逐条查库）：
    - `ok`      —— 该章声明的原文资料还在；
    - `missing` —— 原文资料已被删除（章节本身留着，点了跳原文只会扑空）；
    - `unknown` —— 历史数据没带可用的 `resource_id`。

    为什么不顺手把无效章节从库里删掉：那是**数据迁移**，而这里只需要一个只读标注；
    真要清理也得单独一次显式迁移，不能在读接口里悄悄改库。
    """
    if not isinstance(quiz_core, dict):
        return None
    sections = [item for item in (quiz_core.get("sections") or []) if isinstance(item, dict)]
    if not sections:
        return quiz_core

    wanted: set[int] = set()
    for section in sections:
        resource_id = _section_resource_id(section)
        if resource_id is not None:
            wanted.add(resource_id)

    alive: set[int] = set()
    if wanted:
        alive = {
            int(row)
            for row in db.scalars(select(Resource.id).where(Resource.id.in_(wanted)))
        }

    annotated: list[dict[str, Any]] = []
    for section in sections:
        item = dict(section)
        resource_id = _section_resource_id(section)
        if resource_id is None:
            item[SOURCE_STATUS_FIELD] = "unknown"
        elif resource_id in alive:
            item[SOURCE_STATUS_FIELD] = "ok"
        else:
            item[SOURCE_STATUS_FIELD] = "missing"
        annotated.append(item)

    updated = dict(quiz_core)
    updated["sections"] = annotated
    return updated


def _section_resource_id(section: dict[str, Any]) -> int | None:
    """取章节 `source_ref.resource_id`；缺失或非法一律当"没有"。"""
    source_ref = section.get("source_ref")
    if not isinstance(source_ref, dict):
        return None
    raw = source_ref.get("resource_id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def prune_resource_sections(quiz_core: Any, *, resource_id: int) -> dict[str, Any] | None:
    """从考纲里摘掉**以这份资料为原文锚点**的章节；返回新对象，没改动返回 `None`。

    为什么删资料必须摘章节：章节是"从这份资料提炼、并以它为锚"的派生数据，
    `source_ref.resource_id` 指向它、正文靠 `chunk_from/to` 回查。资料一删这两样都断了，
    而**后续任何一次提炼都修不回来**——`_merge_quiz_core` 的幂等替换只针对"当前这次提炼的
    那份资料"，重传同名文件拿到的是**新的 resource_id**，只会再新增一批章节，
    于是考纲里会留下同名章节的两份。所以只能在这里清。

    留着还有隐性代价：`_pick_sections` 挑出题上下文时**不筛 source_status**，
    这些取不到原文的章节照样占轮换名额、照样被 `bump_asked_count` 记账。

    同章合并的取舍：`_merge_quiz_core` 会把**不同资料的同名章节合并**，而 `source_ref`
    只保留**首次创建**该章节的那份资料。所以按 `resource_id` 摘 = 连它合并进来的、
    来自其它资料的考点名词一起丢。这是**刻意的**：合并后无法区分"哪个考点名词来自哪份资料"
    （数据结构里没这个信息），而留着一个跳不过去的章节比丢几个名词更糟——用户还能
    重新提炼那份还在的资料补回来。
    """
    if not isinstance(quiz_core, dict):
        return None
    sections = [item for item in (quiz_core.get("sections") or []) if isinstance(item, dict)]
    if not sections:
        return None
    kept = [item for item in sections if _section_resource_id(item) != resource_id]
    if len(kept) == len(sections):
        return None
    updated = dict(quiz_core)
    updated["sections"] = kept
    return updated


class KnowledgeBaseService:
    """知识库 CRUD。"""

    @staticmethod
    def prune_resource_sections(quiz_core: Any, *, resource_id: int) -> dict[str, Any] | None:
        """删资料时摘掉它的考纲章节（见模块级 `prune_resource_sections` 的完整取舍说明）。"""
        return prune_resource_sections(quiz_core, resource_id=resource_id)

    @staticmethod
    def list_knowledge_bases(db: Session, *, user_id: int) -> list[dict[str, Any]]:
        """按创建时间倒序返回，并附上每库的构建状态。"""
        rows = list(
            db.scalars(
                select(KnowledgeBase)
                .where(KnowledgeBase.user_id == user_id)
                .order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.id.desc())
            )
        )
        return [KnowledgeBaseService._to_item(db, row) for row in rows]

    @staticmethod
    def get_or_404(db: Session, *, user_id: int, knowledge_base_id: int) -> KnowledgeBase:
        """按 user_id + id 取库，不存在抛 404（避免越权读到别人的库）。"""
        row = db.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.user_id == user_id,
            )
        )
        if row is None:
            raise_error(code=404, message="知识库不存在")
        return row

    @staticmethod
    def get_detail(
        db: Session, *, user_id: int, knowledge_base_id: int
    ) -> dict[str, Any]:
        """单库详情。**只有这里带 `quiz_core`**——阅读器要用它画"考纲 → 跳原文"，
        列表接口不带（每库约 8,000 字的考纲会让列表响应翻好几倍）。"""
        return KnowledgeBaseService._to_item(
            db,
            KnowledgeBaseService.get_or_404(
                db, user_id=user_id, knowledge_base_id=knowledge_base_id
            ),
            with_quiz_core=True,
        )

    @staticmethod
    def create(
        db: Session, *, user_id: int, name: str, description: str | None = None
    ) -> dict[str, Any]:
        """建库。空库的 `quiz_status` 是 pending（还没有资料可提炼）。"""
        knowledge_base = KnowledgeBase(
            user_id=user_id,
            name=name.strip(),
            description=(description or "").strip() or None,
        )
        db.add(knowledge_base)
        db.commit()
        db.refresh(knowledge_base)
        return KnowledgeBaseService._to_item(db, knowledge_base)

    @staticmethod
    def update(
        db: Session, *, user_id: int, knowledge_base_id: int, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """只改白名单字段（名字 / 说明），避免请求体顺手改掉状态或 quiz_core。"""
        knowledge_base = KnowledgeBaseService.get_or_404(
            db, user_id=user_id, knowledge_base_id=knowledge_base_id
        )
        if "name" in changes and changes["name"] is not None:
            knowledge_base.name = str(changes["name"]).strip()
        if "description" in changes:
            value = changes["description"]
            knowledge_base.description = str(value).strip() if value else None
        db.commit()
        db.refresh(knowledge_base)
        return KnowledgeBaseService._to_item(db, knowledge_base)

    @staticmethod
    def delete(
        db: Session, *, user_id: int, knowledge_base_id: int
    ) -> dict[str, Any]:
        """删库：资料与练习解绑、掌握度删除、弱点改回"通用"，最后删库行。

        不删文件与切片：它们仍可被答疑检索用到；也不清 Qdrant 向量（同上）。

        ⚠️ **顺序必须显式**：本项目没有定义 ORM `relationship`，SQLAlchemy 不会自动把
        "先删子表、再删父表"排好——实测直接 `db.delete(kb)` 会被
        `user_knowledge_mastery_ibfk_1` 挡下（1451）。所以这里先 flush 解绑、再 Core 删子表、
        最后删父表。
        """
        knowledge_base = KnowledgeBaseService.get_or_404(
            db, user_id=user_id, knowledge_base_id=knowledge_base_id
        )

        resources = list(
            db.scalars(
                select(Resource).where(Resource.knowledge_base_id == knowledge_base_id)
            )
        )
        for resource in resources:
            resource.knowledge_base_id = None

        practices = list(
            db.scalars(
                select(PracticeSet).where(PracticeSet.knowledge_base_id == knowledge_base_id)
            )
        )
        for practice in practices:
            practice.knowledge_base_id = None

        # 先把"解绑"落盘，否则后面的 DELETE 可能先于 UPDATE 执行，仍被外键挡住
        db.flush()

        removed_mastery = db.execute(
            sql_delete(UserKnowledgeMastery).where(
                UserKnowledgeMastery.knowledge_base_id == knowledge_base_id
            )
        ).rowcount or 0

        # 弱点里指向这个库的条目改成"通用弱点"（知识本身还在，只是不再归属这个库）
        profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
        if profile is not None and profile.weak_points:
            updated: list[dict[str, Any]] = []
            for item in profile.weak_points:
                if isinstance(item, dict) and item.get("knowledge_base_id") == knowledge_base_id:
                    item = {**item, "knowledge_base_id": None}
                updated.append(item)
            profile.weak_points = updated

        try:
            db.delete(knowledge_base)
            db.commit()
        except IntegrityError:
            # 并发场景：删库的同时有练习/构建任务在写这个库（外键会挡住 DELETE）。
            # 不抛 500，给前端一个可重试的明确信号。
            db.rollback()
            raise_error(code=409, message="该知识库还有正在进行的练习或构建任务，请稍后重试")
        return {
            "knowledge_base_id": knowledge_base_id,
            "released_resource_count": len(resources),
            "released_practice_count": len(practices),
            "removed_mastery_count": int(removed_mastery),
            "retention_policy": DELETE_RETENTION_POLICY,
        }

    # ------------------------------------------------------------------ 库内文档（阅读器）
    @staticmethod
    def list_resources(
        db: Session,
        *,
        user_id: int,
        knowledge_base_id: int,
        keyword: str | None = None,
        group_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """列出库内文档（不含正文）：带切片数与可读字数，供列表页展示。

        - `keyword` 非空时做**库内搜索**：文件名 **或** 任意切片正文命中即算命中。
          为什么连正文一起搜——用户记得的多半是"哪份资料里讲过这个"，而不是文件名。
        - `group_name` 非空时只返回该分组的文档。

        统计用一次 `LEFT JOIN + GROUP BY` 拿齐，避免每份资料再查一次切片表（N+1）。
        """
        KnowledgeBaseService.get_or_404(
            db, user_id=user_id, knowledge_base_id=knowledge_base_id
        )
        needle = (keyword or "").strip()
        wanted_group = (group_name or "").strip()

        conditions = [Resource.knowledge_base_id == knowledge_base_id]
        if wanted_group:
            conditions.append(Resource.group_name == wanted_group)

        chunk_count = func.count(KnowledgeChunk.id)
        chunk_chars = func.coalesce(func.sum(KnowledgeChunk.char_count), 0)
        # 只用**一个** LEFT JOIN：搜索时把"正文命中"写进 ON，非搜索时按 resource_id 关联。
        # 曾经写成"先 outerjoin 再 join"——SQLAlchemy 会生成两个同名 JOIN，MySQL 直接报
        # `Not unique table/alias: 'knowledge_chunks'`。
        on_clause = KnowledgeChunk.resource_id == Resource.id
        if needle:
            on_clause = on_clause & KnowledgeChunk.text.ilike(
                KnowledgeBaseService._like_pattern(needle), escape="\\"
            )
        stmt = (
            select(
                Resource,
                chunk_count.label("chunk_count"),
                chunk_chars.label("chunk_chars"),
                func.unix_timestamp(Resource.create_time).label("create_ts"),
            )
            .join(KnowledgeChunk, on_clause, isouter=True)
            .where(*conditions)
            .group_by(Resource.id)
            .order_by(Resource.create_time.desc(), Resource.id.desc())
        )
        if needle:
            # 命中条件：文件名命中，**或** 至少有一个切片命中（后者由 JOIN 上来的行体现）
            stmt = stmt.where(
                or_(
                    Resource.file_name.ilike(
                        KnowledgeBaseService._like_pattern(needle), escape="\\"
                    ),
                    KnowledgeChunk.id.is_not(None),
                )
            )

        rows = db.execute(stmt).all()
        items: list[dict[str, Any]] = []
        for resource, count, chars, create_ts in rows:
            matched_chunks = int(count or 0)
            name_hit = bool(needle) and needle.lower() in (resource.file_name or "").lower()
            item = KnowledgeBaseService._to_resource_item(
                resource,
                chunk_count=matched_chunks if needle else int(count or 0),
                chunk_chars=int(chars or 0),
                create_ts=create_ts,
            )
            if needle:
                item["matched_chunk_count"] = matched_chunks
                if name_hit and matched_chunks:
                    item["match_field"] = "both"
                elif name_hit:
                    item["match_field"] = "name"
                else:
                    item["match_field"] = "content"
                item["match_snippets"] = KnowledgeBaseService._match_snippets(
                    db, resource_id=resource.id, needle=needle
                )
            items.append(item)
        return items

    @staticmethod
    def list_groups(
        db: Session, *, user_id: int, knowledge_base_id: int
    ) -> list[dict[str, Any]]:
        """库内分组（含"未分组"）：`group_name` 是纯字符串列，分组由数据反推，没有分组表。"""
        KnowledgeBaseService.get_or_404(
            db, user_id=user_id, knowledge_base_id=knowledge_base_id
        )
        rows = db.execute(
            select(Resource.group_name, func.count(Resource.id))
            .where(Resource.knowledge_base_id == knowledge_base_id)
            .group_by(Resource.group_name)
        ).all()
        items = [{"name": (name or None), "resource_count": int(count or 0)} for name, count in rows]
        # 未分组排最后；其余按文档数倒序、同数按名字
        return sorted(
            items,
            key=lambda row: (row["name"] is None, -row["resource_count"], row["name"] or ""),
        )

    @staticmethod
    def update_resource(
        db: Session, *, user_id: int, resource_id: int, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """改名 / 移动分组 / **重新归档到某个知识库**：只改白名单字段。

        改文件名**不必**同步 Qdrant：payload 里的 `file_name` 只服务于"同一份文件重传时先删后写"，
        而删向量一律按 `vector_id`（权威来源是 `knowledge_chunks`），所以改名不会留下脏数据。

        重新归档（`knowledge_base_id`）的意义：删库会把库内资料**解绑**而不是删掉（切片与向量都留），
        解绑后 `resources` 列表接口（必须传库 ID）就再也列不出它——本参数是**把孤儿收回来的唯一入口**。
        重新归档不会重算切片或向量：它们本来就没被删过，所以是纯元数据变更。

        ⚠️ **只有文档能归档**：图片/音频（知识卡 png、语音）是消息附件，进库会污染文档列表与考纲；
        带库上传在 `UploadService` 已被 400 拦住，这里是**归档接口**（`PATCH .../unbound-resources/{id}`）
        这一条独立入口的闸——它是唯一能把已有资源塞进库的路，闸门必须与上传口径一致。
        """
        resource = KnowledgeBaseService.get_resource_or_404(
            db, user_id=user_id, resource_id=resource_id
        )
        if "file_name" in changes and changes["file_name"] is not None:
            new_name = str(changes["file_name"]).strip()
            if not new_name:
                raise_error(code=400, message="文件名不能为空")
            resource.file_name = new_name[:255]
        if "group_name" in changes:
            # 传空字符串 = 移出分组；"没传"与"传 null"的区别由 `exclude_unset` 保证
            value = changes["group_name"]
            resource.group_name = (str(value).strip()[:64] or None) if value else None
        if "knowledge_base_id" in changes and changes["knowledge_base_id"] is not None:
            if resource.resource_type != RESOURCE_TYPE_FILE:
                raise_error(code=400, message=KNOWLEDGE_BASE_NON_DOCUMENT_MESSAGE)
            resource.knowledge_base_id = KnowledgeBaseService._require_own_knowledge_base(
                db, user_id=user_id, knowledge_base_id=int(changes["knowledge_base_id"])
            )
        db.commit()
        db.refresh(resource)
        return KnowledgeBaseService.get_resource_detail(
            db, user_id=user_id, resource_id=resource_id
        )

    @staticmethod
    def _require_own_knowledge_base(db: Session, *, user_id: int, knowledge_base_id: int) -> int:
        """校验这个库是本人的，返回库 ID（只用来挡住"把资料归档进别人的库"）。"""
        if db.scalar(
            select(KnowledgeBase.id).where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.user_id == user_id,
            )
        ) is None:
            raise_error(code=404, message="知识库不存在")
        return knowledge_base_id

    @staticmethod
    def list_unbound_resources(db: Session, *, user_id: int) -> list[dict[str, Any]]:
        """列出**未归档**（`knowledge_base_id` 为空）的**文档型**资料。

        为什么需要这个列表：删库时资料是**解绑**而不是删除（切片、向量、原文件全留，
        答疑仍检索得到），但库内文档列表接口必须传 `knowledge_base_id`——
        于是这些资料在实际使用中仍然生效，用户却再也没有地方能看到或处理它们。
        本方法与 `reassign_resource` 一起补上这两个缺口（可见性 + 回收）。

        ⚠️ **必须筛掉图片/音频**（2026-10-20 用户反馈）：`knowledge_base_id IS NULL`
        不止"删库留下的孤儿资料"这一种，**聊天里的每一张图、每一个知识卡 png、每个头像/语音
        天生就是 NULL**。那条列表的口径是"孤儿文档"，把这些附件一起列出来，用户看到的就是
        "'已删除的知识库留下的'里躺着一张 knowledge_card_xxx.png"——既误导（库根本没删过），
        又给了"归档回库"的入口（点一下 png 就成了知识库资料）。
        文档与附件的分界就看 `resource_type`（`RESOURCE_TYPE_FILE` = 文档）。
        """
        today = int(time.time()) // 86400
        rows = list(
            db.scalars(
                select(Resource)
                .where(
                    Resource.user_id == user_id,
                    Resource.knowledge_base_id.is_(None),
                    Resource.resource_type == RESOURCE_TYPE_FILE,
                )
                .order_by(Resource.create_time.desc(), Resource.id.desc())
            )
        )
        items: list[dict[str, Any]] = []
        for resource in rows:
            stats = db.execute(
                select(
                    func.count(KnowledgeChunk.id),
                    func.coalesce(func.sum(KnowledgeChunk.char_count), 0),
                ).where(KnowledgeChunk.resource_id == resource.id)
            ).one()
            chunk_count = int(stats[0] or 0)
            create_time = KnowledgeBaseService._resource_create_ts(resource)
            items.append(
                {
                    "id": resource.id,
                    "file_name": resource.file_name,
                    "group_name": None,  # 未归档资料没有库内分组
                    "knowledge_base_id": None,
                    "ingest_status": resource.ingest_status or "pending",
                    "ingest_error": resource.ingest_error,
                    "chunk_count": chunk_count,
                    "char_count": int(resource.content_chars or stats[1] or 0),
                    "has_text": bool(resource.content_text) or chunk_count > 0,
                    "can_preview": KnowledgeBaseService.can_preview_file(resource),
                    "create_time": create_time,
                    "inactive_days": (
                        max(0, today - create_time // 86400) if create_time else None
                    ),
                }
            )
        return items

    @staticmethod
    def reassign_resource(
        db: Session, *, user_id: int, resource_id: int, knowledge_base_id: int
    ) -> dict[str, Any]:
        """把一份**未归档**资料重新归档到某个知识库（回收入口）。"""
        return KnowledgeBaseService.update_resource(
            db,
            user_id=user_id,
            resource_id=resource_id,
            changes={"knowledge_base_id": knowledge_base_id},
        )

    @staticmethod
    def delete_resource(
        db: Session, *, user_id: int, resource_id: int, delete_file: bool = True
    ) -> dict[str, Any]:
        """删除单份文档：**先取向量 id → 删切片 → 删资料行 + 摘考纲章节 → 删向量 → 删原文件**。

        为什么是这个顺序：
        - `knowledge_chunks.resource_id` 有外键，必须**先删切片**再删资料行（否则 1451 被挡）；
        - 向量点的 id 就是 `knowledge_chunks.vector_id`，所以必须在删切片**之前**把它们读出来；
        - **考纲里以这份资料为锚的章节必须同事务摘掉**（见 `prune_resource_sections`）：
          它是派生数据，晚一步就再也没有信息能把它认出来，只能永久留在考纲里；
        - Qdrant / COS 都在事务**之外**做：它们是外部系统，失败不该回滚"资料已删除"这个事实。
        """
        resource = KnowledgeBaseService.get_resource_or_404(
            db, user_id=user_id, resource_id=resource_id
        )
        knowledge_base_id = resource.knowledge_base_id
        file_name = resource.file_name
        storage_path = resource.storage_path

        vector_ids = list(
            db.scalars(
                select(KnowledgeChunk.vector_id).where(
                    KnowledgeChunk.resource_id == resource_id
                )
            )
        )
        deleted_chunks = (
            db.execute(
                sql_delete(KnowledgeChunk).where(KnowledgeChunk.resource_id == resource_id)
            ).rowcount
            or 0
        )

        # 摘考纲章节：与"删资料行"同一个事务，避免出现"资料没了、章节还指着它"
        removed_sections = 0
        knowledge_base = db.get(KnowledgeBase, knowledge_base_id) if knowledge_base_id else None
        if knowledge_base is not None:
            pruned = prune_resource_sections(knowledge_base.quiz_core, resource_id=resource_id)
            if pruned is not None:
                removed_sections = len(knowledge_base.quiz_core.get("sections") or []) - len(
                    pruned.get("sections") or []
                )
                knowledge_base.quiz_core = pruned

        db.delete(resource)
        try:
            db.commit()
        except IntegrityError:
            # 目前只有 knowledge_chunks 引用 resources，且上面已先删干净，所以正常路径走不到这里；
            # 万一以后加了 RESTRICT 外键（例如练习记录直接引用资料），这里必须给出**可操作**的提示，
            # 而不是"请稍后重试"——重试多少次都不会成功。
            db.rollback()
            raise_error(
                code=409,
                message="这份资料还被其它记录引用（例如已生成的练习），请先处理那些记录再删除",
            )

        deleted_vectors = delete_vectors_by_ids(vector_ids)
        deleted_file = False
        if delete_file and CosStorageService.is_cos_storage_path(storage_path or ""):
            try:
                client = CosStorageService.get_client()
                deleted_file = CosStorageService.delete_object_by_storage_path(
                    client, storage_path
                )
            except Exception as exc:  # noqa: BLE001 - 对象存储失败不影响"资料已删除"
                main_logger.warning(f"删除原文件失败 resource_id={resource_id}: {exc}")

        return {
            "resource_id": resource_id,
            "file_name": file_name,
            "knowledge_base_id": knowledge_base_id,
            "deleted_chunk_count": int(deleted_chunks),
            "deleted_vector_count": int(deleted_vectors),
            "deleted_file": bool(deleted_file),
            "removed_quiz_sections": int(removed_sections),
        }

    @staticmethod
    def get_resource_detail(
        db: Session, *, user_id: int, resource_id: int
    ) -> dict[str, Any]:
        """文档详情：正文（全文优先，切片拼回兜底）+ 切片索引 + 原文件临时预览链接。"""
        resource = KnowledgeBaseService.get_resource_or_404(
            db, user_id=user_id, resource_id=resource_id
        )
        chunks = list(
            db.scalars(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.resource_id == resource_id)
                .order_by(KnowledgeChunk.chunk_index.asc())
            )
        )

        if resource.content_text:
            text = str(resource.content_text)
            text_source = "original"
        elif chunks:
            # 老数据没有 content_text：用切片拼回（块间重叠会被消掉，排版仍有损失）
            text = join_chunks([str(chunk.text or "") for chunk in chunks])
            text_source = "chunks"
        else:
            text = ""
            text_source = "none"

        preview_available = KnowledgeBaseService.can_preview_file(resource)
        return {
            "id": resource.id,
            "knowledge_base_id": resource.knowledge_base_id,
            "file_name": resource.file_name,
            "group_name": resource.group_name,
            "ingest_status": resource.ingest_status,
            "ingest_error": resource.ingest_error,
            "create_time": KnowledgeBaseService._resource_create_ts(resource),
            "text": text,
            "text_chars": len(text),
            "text_source": text_source,
            "chunk_count": len(chunks),
            "chunks": [
                {
                    "chunk_index": chunk.chunk_index,
                    "char_count": chunk.char_count,
                    "text": str(chunk.text or ""),
                }
                for chunk in chunks
            ],
            "preview_path": (
                f"/knowledge-bases/{resource.knowledge_base_id}"
                f"/resources/{resource.id}/file"
                if preview_available
                else None
            ),
            "can_preview": preview_available,
        }

    @staticmethod
    def get_chunk(
        db: Session, *, user_id: int, resource_id: int, chunk_index: int
    ) -> dict[str, Any]:
        """取某个顺序号的切片：考纲里的 `source_ref.chunk_from` 直接喂进来即可定位。"""
        resource = KnowledgeBaseService.get_resource_or_404(
            db, user_id=user_id, resource_id=resource_id
        )
        chunk = db.scalar(
            select(KnowledgeChunk).where(
                KnowledgeChunk.resource_id == resource_id,
                KnowledgeChunk.chunk_index == chunk_index,
            )
        )
        if chunk is None:
            raise_error(code=404, message="片段不存在（该资料可能已重新入库）")
        return {
            "resource_id": resource.id,
            "resource_name": resource.file_name,
            "chunk": {
                "chunk_index": chunk.chunk_index,
                "char_count": chunk.char_count,
                "text": str(chunk.text or ""),
            },
        }

    @staticmethod
    def get_resource_or_404(db: Session, *, user_id: int, resource_id: int) -> Resource:
        """按 user_id + id 取资料，不存在抛 404（库内文档只能由本人读）。"""
        row = db.scalar(
            select(Resource).where(
                Resource.id == resource_id, Resource.user_id == user_id
            )
        )
        if row is None:
            raise_error(code=404, message="资料不存在")
        return row

    @staticmethod
    def _like_pattern(needle: str) -> str:
        """把用户输入转成安全的 LIKE 模式。

        必须转义 `%` `_` `\\`：否则用户搜一个 `%` 就会命中全库，
        搜 `_` 会变成"任意单字符"，看起来像 bug。
        """
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    @staticmethod
    def _match_snippets(
        db: Session, *, resource_id: int, needle: str, limit: int = 3, span: int = 120
    ) -> list[str]:
        """取几段命中正文用于列表预览（关键词前后各留一段，超出部分用省略号）。

        只在搜索时调用；切片可能很长，所以截 `span` 字而不是整片返回。
        """
        rows = list(
            db.scalars(
                select(KnowledgeChunk.text)
                .where(
                    KnowledgeChunk.resource_id == resource_id,
                    KnowledgeChunk.text.ilike(KnowledgeBaseService._like_pattern(needle), escape="\\"),
                )
                .order_by(KnowledgeChunk.chunk_index.asc())
                .limit(limit)
            )
        )
        snippets: list[str] = []
        lowered = needle.lower()
        for text in rows:
            body = str(text or "")
            pos = body.lower().find(lowered)
            if pos < 0:
                snippets.append(body[:span])
                continue
            start = max(0, pos - span // 3)
            end = min(len(body), pos + len(needle) + span)
            snippet = body[start:end].replace("\n", " ").strip()
            if start > 0:
                snippet = "…" + snippet
            if end < len(body):
                snippet = snippet + "…"
            snippets.append(snippet)
        return snippets

    @staticmethod
    def can_preview_file(resource: Resource) -> bool:
        """原文件能不能取到：文件类型 + COS 路径都在才行。"""
        if resource.resource_type != 0:
            return False
        if not resource.file_name:
            return False
        return CosStorageService.is_cos_storage_path(resource.storage_path or "")

    @staticmethod
    def get_file_stream(resource: Resource, *, offset: int = 0, length: int = 0):
        """从 COS 取原文件（可选 Range），交给路由流式转发给浏览器。

        为什么不直接把 COS 预签名链接给前端：**2024-01-01 后创建的桶用默认域名访问
        一律强制下载**（实测 `Content-Disposition: attachment`），浏览器无法内联预览 PDF。
        由后端转发还有两个附带好处：COS 链接不外泄；前端拿到的是同源地址，不用配跨域。
        """
        if not KnowledgeBaseService.can_preview_file(resource):
            raise_error(code=404, message="该资料没有可预览的原文件")
        bucket_name, object_name = CosStorageService.parse_storage_path(resource.storage_path)
        client = CosStorageService.get_client()
        kwargs: dict[str, Any] = {"Bucket": bucket_name, "Key": object_name}
        if length > 0:
            kwargs["Range"] = f"bytes={offset}-{offset + length - 1}"
        try:
            return client.get_object(**kwargs)
        except (CosServiceError, CosClientError) as exc:
            main_logger.warning(f"原文件读取失败 resource_id={resource.id}: {exc}")
            raise_error(code=502, message="对象存储暂时不可用，无法读取原文件")

    @staticmethod
    def _to_resource_item(
        resource: Resource,
        *,
        chunk_count: int,
        chunk_chars: int,
        create_ts: Any,
    ) -> dict[str, Any]:
        # 有 content_text 就用它的字符数（权威）；否则用切片字数和作为近似。
        char_count = int(resource.content_chars or 0) or int(chunk_chars or 0)
        return {
            "id": resource.id,
            "file_name": resource.file_name,
            "group_name": resource.group_name,
            "ingest_status": resource.ingest_status,
            "ingest_error": resource.ingest_error,
            "chunk_count": chunk_count,
            "char_count": char_count,
            "has_text": bool(resource.content_text) or chunk_count > 0,
            "can_preview": KnowledgeBaseService.can_preview_file(resource),
            "create_time": int(create_ts) if create_ts is not None else None,
            "matched_chunk_count": 0,
            "match_field": None,
            "match_snippets": [],
        }

    @staticmethod
    def _resource_create_ts(resource: Resource) -> int | None:
        created = resource.create_time
        return int(created.timestamp()) if created is not None else None

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _to_item(
        db: Session, knowledge_base: KnowledgeBase, *, with_quiz_core: bool = False
    ) -> dict[str, Any]:
        status = KbPipelineService.summarize(db, knowledge_base_id=knowledge_base.id)
        item: dict[str, Any] = {
            "id": knowledge_base.id,
            "name": knowledge_base.name,
            "description": knowledge_base.description,
            "resource_count": status.get("resource_count", 0),
            "ready_count": status.get("ready_count", 0),
            "failed_count": status.get("failed_count", 0),
            "pending_count": status.get("pending_count", 0),
            "quiz_status": status.get("quiz_status") or knowledge_base.quiz_status,
            "quiz_extracted_at": status.get("quiz_extracted_at"),
            "can_quiz": bool(status.get("can_quiz")),
            "can_retrieve": bool(status.get("can_retrieve")),
            "created_at": int(knowledge_base.created_at or 0),
            "quiz_core": None,
        }
        if with_quiz_core:
            core = knowledge_base.quiz_core
            item["quiz_core"] = _with_source_status(db, core) if isinstance(core, dict) else None
        return item

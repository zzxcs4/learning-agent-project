"""出题知识库（`quiz_core`）提炼服务：产出一份**考纲式的知识边界**。

⚠️ 它不是"内容摘要"——这是本服务最容易做错的定位：

- **不做什么**：不写知识点讲解、不抽取"可判定断言"。具体事实由**原始资料**（`knowledge_chunks` /
  RAG 检索）在出题时提供。
- **做什么**：告诉出题模型三件事——① 这套资料能考哪些章节与考点（**边界**）② 哪些最常考（**优先级**）
  ③ 面试官会怎么往下追问（**追问路径**）。三块合起来就是一份"考纲"。

形态参考 TechSpar 的领域 `README.md`（`REFERENCE_PROJECTS.md` A5 第 143–144 行）::

    Java
    最常考什么
    集合框架、泛型、并发、JVM、异常处理、IO/NIO、反射、注解。
    面试通常不会只问 API 用法，而是追到实现机制、线程安全和性能影响。
    常见追问路径
    HashMap、ConcurrentHashMap、ArrayList、LinkedList 的差异和适用场景。
    ...
    核心知识边界
    1. 语言基础与面向对象
       值传递、重载/重写、接口与抽象类、访问控制、异常体系。
    2. 集合与泛型
       常见集合底层结构、扩容机制、遍历方式、泛型擦除。
    ...

存储形状（`knowledge_bases.quiz_core`，JSON **对象**，不是数组）::

    {"title": "MySQL 索引与事务",
     "frequently_tested": ["最左前缀与索引失效：几乎每轮都会问"],
     "followup_paths": ["从“这个索引为什么没用上”追问到执行计划与回表代价"],
     "sections": [
       {"heading": "索引入门",
        "topics": ["聚簇索引与回表", "联合索引与最左前缀", "索引失效的常见写法"],
        "source_ref": {"resource_id": 2, "chunk_from": 0, "chunk_to": 1},
        "asked_count": 0, "last_asked_at": null}
     ]}

`asked_count` 落在**章节**上（轮换单位 = 一章 → 该章可出多道题），语义是「**这一章被选作出题
上下文的次数**」——**不是**"考过几次"（题实际考了哪章无从得知，模型可能考没给它原文的章）。
所以它只用于轮换（下次优先配没轮到的章节），**不能当覆盖率**（知识覆盖率本项目不做，见交接 §0.5 第 13 条）。
TechSpar 没有这个字段，它的覆盖面靠"高频题库 + `recent_questions` + `divergence` + `question_graph`"
四件套；本项目只有这一个机制，所以**形态照抄、计数必须自己留**。

成本：**每 ~5 万字 1 次 LLM 调用**。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.agent.workflows.common import ensure_list, invoke_json
from app.core.logging_config import main_logger
from app.db import SessionLocal
from app.db.models import KnowledgeBase, KnowledgeChunk, Resource
from app.rag.chunk_store import fetch_chunks_by_resource

# 每 ~5 万字提炼 1 次（≈ 100 个 500 字切片）
CHARS_PER_EXTRACTION = 50_000
# 单批产出多少个章节。考纲比内容摘要紧凑得多（每章只列考点名词），4 章 ≈ 5 万字资料的真实章节数；
# 输出约 900 字符，在 `DEEPSEEK_MAX_TOKENS=1024`（≈1400 汉字）下安全。
MAX_SECTIONS_PER_BATCH = 4
# 每章最多列多少个考点名词
MAX_TOPICS_PER_SECTION = 6
# 章节标题 / 考点名词 / 元信息的字数与条数上限。
# ⚠️ `MAX_TOPIC_CHARS` 不能太小：考点名词会拿去做"关键词定位"，**被截断的词在原文里根本不存在**，
# 会导致该考点永远匹配不到（实测 24 字把"ConcurrentHashMap 桶锁与协助扩容"截成"…协助扩"，命中率 95.5%）。
MAX_HEADING_CHARS = 30
MAX_TOPIC_CHARS = 40
MAX_META_ITEMS = 8
# 注入预算（§5.2 用）：整份考纲渲染成 Markdown 后的字符预算
CHAR_BUDGET = 8_000


class QuizCoreService:
    """提炼（资料 → 考纲）与渲染（考纲 → 出题提示词上下文）。"""

    # ------------------------------------------------------------------ 对外入口
    @staticmethod
    def extract_for_resource(resource_id: int) -> dict[str, Any]:
        """异步提炼入口（给 `BackgroundTasks` 用）：自己开/关 session，异常只记日志。

        上传接口不感知提炼结果 —— 提炼失败不能让一次成功的上传变成失败。
        """
        db = SessionLocal()
        try:
            result = QuizCoreService.extract_resource(db, resource_id=resource_id)
            main_logger.info(f"quiz_core 提炼结束: {result}")
            return result
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛给请求
            db.rollback()
            main_logger.warning(f"quiz_core 提炼失败 resource_id={resource_id}: {exc}")
            return {"status": "failed", "resource_id": resource_id, "error": str(exc)}
        finally:
            db.close()

    @staticmethod
    def extract_resource(db: Session, *, resource_id: int, llm: Any = None) -> dict[str, Any]:
        """读切片 → 每 ~5 万字 1 次 LLM → 合并成考纲 → 幂等写回 `quiz_core`。"""
        resource = db.get(Resource, resource_id)
        if resource is None:
            return {"status": "skipped", "reason": "资源不存在"}
        if not resource.knowledge_base_id:
            return {"status": "skipped", "reason": "资料未归档到知识库，没有可写的 quiz_core"}

        knowledge_base = db.get(KnowledgeBase, resource.knowledge_base_id)
        if knowledge_base is None:
            return {"status": "skipped", "reason": "资料指向的知识库不存在"}

        chunks = fetch_chunks_by_resource(db, resource_id=resource_id)
        if not chunks:
            return {"status": "skipped", "reason": "没有切片（图片/音频，或还没入库）"}

        batches = QuizCoreService._group_chunks(chunks)
        incoming = [
            QuizCoreService._extract_batch(llm, batch, resource_id=resource_id) for batch in batches
        ]

        # ⚠️ 守卫必须在合并**之前**：合并的第一步是"按资源幂等替换"，如果模型什么有用的都没产出，
        # 替换会把该资源的旧章节删掉却补不上新的 —— 等于把已有考纲删成空。
        if not any(_has_content(batch) for batch in incoming):
            return {
                "status": "empty",
                "resource_id": resource_id,
                "knowledge_base_id": knowledge_base.id,
                "reason": "模型没给出可用考纲（已有 quiz_core 原样保留）",
            }

        merged = _merge_quiz_core(
            knowledge_base.quiz_core,
            incoming,
            resource_id=resource_id,
            title=knowledge_base.name,
        )
        # 与已有考纲合并后仍为空（例如旧数据也是空壳）→ 同样不写，避免留下只有标题的空块
        if not _has_content(merged):
            return {
                "status": "empty",
                "resource_id": resource_id,
                "knowledge_base_id": knowledge_base.id,
                "reason": "模型没给出可用考纲（已有 quiz_core 原样保留）",
            }
        # 必须赋新对象，否则 SQLAlchemy 检测不到 JSON 列的原地修改
        knowledge_base.quiz_core = merged
        db.commit()

        sections = merged.get("sections") or []
        return {
            "status": "ok",
            "resource_id": resource_id,
            "knowledge_base_id": knowledge_base.id,
            "chunks": len(chunks),
            "batches": len(batches),
            "sections": len(sections),
            "topics": sum(len(section.get("topics") or []) for section in sections),
            "rendered_chars": len(QuizCoreService.render_quiz_core(merged)),
        }

    # ------------------------------------------------------------------ 渲染
    @staticmethod
    def render_quiz_core(quiz_core: Any, char_budget: int = CHAR_BUDGET) -> str:
        """把考纲渲染成 Markdown（`{knowledge_context}`），形式对齐 TechSpar 的领域 README。

        超预算时**按章节**取舍（`asked_count` 升序，没被用作上下文的章优先），**绝不切字符**：考纲每章只列
        考点名词，整章丢弃的损失远小于把一节切成半句。

        兼容旧的"扁平条目"格式（历史上 `quiz_core` 存过 `[{point, summary}]`）：按列表渲染，不丢数据。
        """
        if isinstance(quiz_core, list):
            return _render_legacy(quiz_core, char_budget)

        data = quiz_core if isinstance(quiz_core, dict) else {}
        blocks: list[str] = []
        used = 0

        def push(block: str) -> bool:
            """整块放入预算；放不下返回 False（调用方决定丢弃还是终止）。"""
            nonlocal used
            cost = len(block) + 2
            if used + cost > char_budget:
                return False
            blocks.append(block)
            used += cost
            return True

        title = str(data.get("title") or "知识边界").strip()
        push(f"# {title}")

        frequently_tested = [str(x).strip() for x in (data.get("frequently_tested") or []) if str(x).strip()]
        if frequently_tested:
            push("## 最常考什么\n" + "\n".join(f"- {item}" for item in frequently_tested))

        followup_paths = [str(x).strip() for x in (data.get("followup_paths") or []) if str(x).strip()]
        if followup_paths:
            push("## 常见追问路径\n" + "\n".join(f"- {item}" for item in followup_paths))

        sections = [item for item in (data.get("sections") or []) if isinstance(item, dict)]
        sections.sort(key=lambda item: int(item.get("asked_count") or 0))
        outline: list[str] = []
        for index, section in enumerate(sections, start=1):
            heading = str(section.get("heading") or "").strip()
            if not heading:
                continue
            topics = [str(t).strip() for t in (section.get("topics") or []) if str(t).strip()]
            line = f"{index}. {heading}"
            if topics:
                line += "\n   " + "、".join(topics)
            # 章节只走预算记账，不直接进 blocks（否则会和下面的"核心知识边界"块重复）
            cost = len(line) + 1
            if used + cost > char_budget:
                break  # 整章丢弃，绝不切半章
            used += cost
            outline.append(line)

        if outline:
            blocks.append("## 核心知识边界\n" + "\n".join(outline))
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------ 提炼内部
    @staticmethod
    def _group_chunks(chunks: list[KnowledgeChunk]) -> list[list[KnowledgeChunk]]:
        """按累计字符数分组：一组 ≈ `CHARS_PER_EXTRACTION` 字，对应 1 次 LLM 调用。"""
        batches: list[list[KnowledgeChunk]] = []
        current: list[KnowledgeChunk] = []
        used = 0
        for chunk in chunks:
            size = chunk.char_count or len(str(chunk.text or ""))
            if current and used + size > CHARS_PER_EXTRACTION:
                batches.append(current)
                current, used = [], 0
            current.append(chunk)
            used += size
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _extract_batch(
        llm: Any, batch: list[KnowledgeChunk], *, resource_id: int
    ) -> dict[str, Any]:
        """提炼一批切片，产出一份"局部考纲"（章节 + 考点名词 + 元信息）。

        单批失败只返回空结果，不拖垮整份资料。失败含：调用失败/超时，以及输出被 `max_tokens`
        截断导致 JSON 不完整（`extract_json_object` 会抛异常）。
        """
        chunk_from = min(chunk.chunk_index for chunk in batch)
        chunk_to = max(chunk.chunk_index for chunk in batch)
        empty: dict[str, Any] = {"sections": [], "frequently_tested": [], "followup_paths": []}
        try:
            data = invoke_json(llm, QuizCoreService._build_prompt(batch), provider="deepseek")
        except Exception as exc:  # noqa: BLE001 - 单批失败不拖垮整份资料
            main_logger.warning(
                f"quiz_core 单批提炼失败（跳过）chunk {chunk_from}-{chunk_to}: {exc}"
            )
            return empty
        if not isinstance(data, dict):
            return empty

        sections: list[dict[str, Any]] = []
        # 截断到 MAX_SECTIONS_PER_BATCH：模型偶尔会无视"2–4 章"的约束给出七八章
        for item in ensure_list(data.get("sections"))[:MAX_SECTIONS_PER_BATCH]:
            if not isinstance(item, dict):
                continue
            heading = _clip(item.get("heading"), MAX_HEADING_CHARS)
            if not heading:
                continue
            topics: list[str] = []
            for topic in ensure_list(item.get("topics"))[:MAX_TOPICS_PER_SECTION]:
                clipped = _clip(topic, MAX_TOPIC_CHARS)
                if clipped and clipped not in topics:
                    topics.append(clipped)
            chunk_no = _clamp_int(item.get("chunk"), chunk_from, chunk_to)
            sections.append(
                {
                    "heading": heading,
                    "topics": topics,
                    "source_ref": {
                        "resource_id": resource_id,
                        "chunk_from": chunk_no,
                        "chunk_to": chunk_no,
                    },
                    "asked_count": 0,
                    "last_asked_at": None,
                }
            )

        return {
            "sections": sections,
            "frequently_tested": _take_meta(data.get("frequently_tested")),
            "followup_paths": _take_meta(data.get("followup_paths")),
        }

    @staticmethod
    def _build_prompt(batch: list[KnowledgeChunk]) -> str:
        """提炼提示词：要"考纲"，不是"内容摘要"。"""
        material = "\n\n".join(
            f"[chunk {chunk.chunk_index}] {str(chunk.text or '').strip()}" for chunk in batch
        )
        return f"""
你是「出题知识库」的考纲提炼 Agent。

你产出的**不是内容摘要，也不是知识点讲解**，而是一份**考纲**：告诉出题的人"这套资料能考哪些章节与考点、哪些最常考、面试官会怎么往下追问"。具体的事实细节不用你写，出题时会另外给出原文。

要求：
1. 给出本批资料覆盖的 **2–{MAX_SECTIONS_PER_BATCH} 个章节**：heading 用很短的章节名（如"索引入门""并发容器"），不要写成句子；**章节名要具体，不要用「概述」「其他」「杂项」这类泛词**（泛名会让不同资料的章节被错误合并）。资料本身没有章节结构时，请自己归纳出边界清晰的章节。
2. 每章列 **3–{MAX_TOPICS_PER_SECTION} 个考点名词**（短语，如"聚簇索引与回表""最左前缀"），**只给名词，不要解释**。
3. 给出本批**最常考的 1–3 个考点**（frequently_tested，短语，可带一句原因）。
4. 给出 1–3 条**常见追问路径**（followup_paths：面试官会从这个考点往下追什么，如"从索引失效追问到执行计划与回表代价"）。
5. 每条填 chunk 字段：该章主要依据的切片号（资料里用 [chunk N] 标出）。
6. 只输出 JSON，不要 Markdown，不要解释。

JSON 格式必须如下：
{{
  "sections": [
    {{"heading": "索引入门", "topics": ["聚簇索引与回表", "联合索引与最左前缀", "索引失效的常见写法"], "chunk": 40}}
  ],
  "frequently_tested": ["最左前缀与索引失效：几乎每轮都会问"],
  "followup_paths": ["从「这个索引为什么没用上」追问到执行计划与回表代价"]
}}

学习资料：
{material}
""".strip()


def _render_legacy(items: list[Any], char_budget: int) -> str:
    """旧格式（`[{point, summary}]`）渲染：按条目取舍，不切字符。"""
    lines: list[str] = []
    used = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        point = str(item.get("point") or "").strip()
        if not point:
            continue
        summary = str(item.get("summary") or "").strip()
        line = f"- **{point}**：{summary}" if summary else f"- **{point}**"
        if used + len(line) + 1 > char_budget:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _clip(value: Any, limit: int) -> str | None:
    """去空白并截断到上限；空值统一为 None。"""
    text = str(value).strip() if value is not None else ""
    return text[:limit] or None


def _take_meta(value: Any) -> list[str]:
    """元信息条目：裁剪、去重、限量。"""
    items: list[str] = []
    for raw in ensure_list(value):
        text = str(raw).strip()
        if text and text not in items:
            items.append(text)
        if len(items) >= MAX_META_ITEMS:
            break
    return items


def _clamp_int(value: Any, low: int, high: int) -> int:
    """把模型给的切片号夹在本批范围内；缺值/非法值退回组内起始切片。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return low
    return min(max(number, low), high)


def _source_resource_id(item: Any) -> int | None:
    """取章节来源的资源 ID（用于"按资源幂等替换"）。"""
    if not isinstance(item, dict):
        return None
    source_ref = item.get("source_ref")
    if not isinstance(source_ref, dict):
        return None
    raw = source_ref.get("resource_id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _has_content(quiz_core: Any) -> bool:
    """考纲里有没有可用内容（章节或元信息都算；只有 title 不算）。"""
    if not isinstance(quiz_core, dict):
        return False
    return bool(
        quiz_core.get("sections")
        or quiz_core.get("frequently_tested")
        or quiz_core.get("followup_paths")
    )


def _normalize_heading(text: Any) -> str:
    """章节名归一化：只保留字母数字，用于跨批/跨资料合并同章。"""
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def _merge_quiz_core(
    existing: Any,
    incoming: list[dict[str, Any]],
    *,
    resource_id: int,
    title: str | None,
) -> dict[str, Any]:
    """把本批产出并进已有考纲。

    - **幂等**：先摘掉该资源上一次提炼出的章节（重传/重提炼不叠加）；
    - **同章合并**：不同资料提炼出同名章节时合并考点名词，保留原有 `asked_count`；
    - **元信息**：合并去重，各限量 `MAX_META_ITEMS` 条；
    - **旧格式**：历史 `[{point, summary}]` 转成"章 = point / 考点 = summary"，不丢数据。
    """
    base: dict[str, Any] = {
        "title": title,
        "frequently_tested": [],
        "followup_paths": [],
        "sections": [],
    }

    if isinstance(existing, dict):
        base["title"] = existing.get("title") or title
        base["frequently_tested"] = _take_meta(existing.get("frequently_tested"))
        base["followup_paths"] = _take_meta(existing.get("followup_paths"))
        base["sections"] = [
            dict(item) for item in (existing.get("sections") or []) if isinstance(item, dict)
        ]
    elif isinstance(existing, list):
        for item in existing:
            if not isinstance(item, dict):
                continue
            heading = _clip(item.get("point"), MAX_HEADING_CHARS)
            if not heading:
                continue
            base["sections"].append(
                {
                    "heading": heading,
                    "topics": [topic for topic in [_clip(item.get("summary"), MAX_TOPIC_CHARS)] if topic],
                    "source_ref": item.get("source_ref") if isinstance(item.get("source_ref"), dict) else {},
                    "asked_count": int(item.get("asked_count") or 0),
                    "last_asked_at": item.get("last_asked_at"),
                }
            )

    # 1) 幂等：摘掉本资源上一次提炼出的章节
    sections = [
        section for section in base["sections"] if _source_resource_id(section) != resource_id
    ]

    # 2) 追加 / 合并新章节
    for batch in incoming:
        if not isinstance(batch, dict):
            continue
        for section in batch.get("sections") or []:
            key = _normalize_heading(section.get("heading"))
            if not key:
                continue
            matched = next(
                (item for item in sections if _normalize_heading(item.get("heading")) == key), None
            )
            if matched is None:
                sections.append(section)
                continue
            topics = list(matched.get("topics") or [])
            for topic in section.get("topics") or []:
                if topic not in topics:
                    topics.append(topic)
            matched["topics"] = topics[:MAX_TOPICS_PER_SECTION]

        base["frequently_tested"] = _take_meta(
            list(base["frequently_tested"]) + list(batch.get("frequently_tested") or [])
        )
        base["followup_paths"] = _take_meta(
            list(base["followup_paths"]) + list(batch.get("followup_paths") or [])
        )

    base["sections"] = sections
    return base

import re
from http import HTTPStatus

import dashscope
from dashscope import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.rag.chunk_store import fetch_chunks_by_vector_ids
from app.rag.core import COLLECTION_KNOWLEDGE, EMBEDDING_DIM, EMBEDDING_MODEL

SCORE_THRESHOLD = 0.4
MAX_TEXT_LEN = 800
# 收窄到某个知识库时，越界片段会占掉 top_k 名额，所以按这个倍数多取候选。
# 实测本项目的库是"1 库 / 1–N 份资料"，这个开销可以忽略；上限 64 防止大库放大成慢查询。
_SCOPED_CANDIDATE_FACTOR = 4

_SKIP_RETRIEVE_PATTERNS = [
    r"^[好嗯哦噢啊呢哈]{1,4}[，,。！!～~]*$",
    r"^(好的|知道了|明白了?|收到|了解|懂了|可以|没问题|行|好吧|好啊|好呀)[～~！!。，,]*$",
    r"^(继续|下一题|下一步|再来一道|再来一个|接着说|继续吧)[吧啊呢～~！!。]*$",
    r"^(谢谢|感谢|谢了|谢谢你|辛苦了)[！!～~。]*$",
    r"^(哈哈|嗯嗯|哦哦|啊啊|emm+|嗯+)[！!～~。,，]*$",
    r"^.{1,2}$",
]

_SKIP_RETRIEVE_COMPILED = [re.compile(p) for p in _SKIP_RETRIEVE_PATTERNS]


def should_retrieve(text: str) -> bool:
    """
    判断用户输入是否需要触发 RAG 检索。

    策略：轻量跳过 + 默认检索。
    只跳过明显无信息量的确认语、推进语、礼貌语和极短输入；
    其余输入默认检索
    """
    t = text.strip()
    if not t:
        return False
    return not any(p.match(t) for p in _SKIP_RETRIEVE_COMPILED)


def _embed_query(query: str) -> list[float]:
    if not settings.dashscope_api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置")

    dashscope.api_key = settings.dashscope_api_key
    resp = TextEmbedding.call(
        model=EMBEDDING_MODEL,
        input=query,
        dimension=EMBEDDING_DIM,
        # 查询侧用 query，入库侧用 document，DashScope 官方要求区分两个角色
        text_type="query",
    )
    if getattr(resp, "status_code", None) != HTTPStatus.OK:
        raise RuntimeError(
            f"查询向量化失败：code={getattr(resp, 'code', None)}, "
            f"message={getattr(resp, 'message', None)}"
        )
    return resp.output["embeddings"][0]["embedding"]


def search_similar_chunks(
    query: str,
    user_id: int,
    db: Session,
    top_k: int = 3,
    resource_ids: list[int] | None = None,
) -> list[dict]:
    """
    输入用户问题，返回最相似的知识库片段。

    检索分两步：Qdrant 向量检索拿到 vector_id 和分数，
    再用 vector_id 回查 MySQL 拿到原文。

    `resource_ids` 用于把检索范围收窄到"某个知识库里的资料"（升级规划 §七
    「出题取原文的过滤收窄到本库 resource_ids」）。**过滤在 MySQL 侧按真实
    `resource_id` 做**，不靠 Qdrant payload：存量点的 payload 里没有
    `resource_id`，只靠 payload 过滤会把这些点误判成越界而丢结果。
    代价是必须多取候选（见 `_SCOPED_CANDIDATE_FACTOR`），因为越界片段会占用 top_k 名额。
    """
    try:
        # 第一步：确认 Qdrant 集合存在；未上传过知识库时直接返回空结果，避免 404 中断聊天流程
        client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
        if not client.collection_exists(COLLECTION_KNOWLEDGE):
            return []

        # 第二步：把用户问题向量化，得到 1024 维查询向量
        query_vector = _embed_query(query)

        # 第三步：构建过滤条件，user_id 必须过滤
        must = [FieldCondition(key="user_id", match=MatchValue(value=user_id))]

        # 第四步：在 Qdrant 中执行向量相似度检索，with_payload=True拿到元信息
        # 使用 query_points 替代已废弃的 search，兼容 Qdrant 1.18+ 服务端
        scoped = resource_ids is not None
        limit = min(top_k * _SCOPED_CANDIDATE_FACTOR, 64) if scoped else top_k
        response = client.query_points(
            collection_name=COLLECTION_KNOWLEDGE,
            query=query_vector,
            query_filter=Filter(must=must),
            limit=limit,
            with_payload=True,
        )
        hits = response.points

        if not hits:
            return []

        # 第四步：用 Qdrant 返回的 vector_id 批量回查 MySQL，取出原文
        # Qdrant payload 不存原文，原文在 MySQL knowledge_chunks 表里
        vector_ids = [str(hit.id) for hit in hits]
        chunk_map = fetch_chunks_by_vector_ids(db=db, vector_ids=vector_ids)
        allowed = set(resource_ids) if scoped else None

        # 第五步：分数阈值过滤，低于 SCORE_THRESHOLD 的片段直接丢弃
        # Qdrant 永远返回 top_k 条，不过滤会把无关片段塞进 Prompt
        results = []
        for hit in hits:
            if len(results) >= top_k:
                break
            if hit.score < SCORE_THRESHOLD:
                continue
            vid = str(hit.id)
            chunk = chunk_map.get(vid)
            chunk_resource_id = chunk.resource_id if chunk else None
            if allowed is not None and chunk_resource_id not in allowed:
                continue
            results.append({
                "chunk_id": chunk.id if chunk else None,
                "resource_id": chunk_resource_id,
                "chunk_index": chunk.chunk_index if chunk else hit.payload.get("chunk_index"),
                "text": chunk.text if chunk else "",
                "text_preview": (chunk.text[:200] if chunk else ""),
                "score": hit.score,
                "file_name": hit.payload.get("file_name", ""),
            })

        # 第六步：单条 text 截断，防止单条过长
        for r in results:
            if len(r["text"]) > MAX_TEXT_LEN:
                r["text"] = r["text"][:MAX_TEXT_LEN]

        return results
    except Exception as e:
        # 捕获所有与向量数据库交互时可能出现的异常（如连接失败、集合不存在等）
        print(f"向量数据库搜索失败，将跳过知识库增强。错误: {e}")
        return []

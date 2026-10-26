"""
RAG 领域核心：单文件聚合「解析 / 分块 / 向量化 / 写库 / pipeline」。

教学说明：
ingest_file()               ← 唯一对外公开的函数（入口）
    │
    ├── 1. _extract_text()        解析文件文本
    ├── 2. _split_chunks()        切分文本块
    ├── 3. _generate_embeddings() 向量化
    ├── 4. _upsert_chunks()       写入 Qdrant（payload 带 resource_id）
    └── 返回 IngestResult
"""

import hashlib
import re
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

import pypdfium2 as pdfium
import dashscope
import docx
from docx.oxml.ns import qn
from dashscope import TextEmbedding
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.core.settings import settings
from app.rag.ocr import ocr_pdf

# 业务固定常量
# ============================================================

# DashScope Embedding 模型与维度：业务统一选型，跨环境保持一致。
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIM = 1024
# DashScope 单次最多 10 条输入。
EMBED_BATCH_SIZE = 10

# Qdrant collection 名：文件知识库现在用，对话记忆库后续接 Memory 时用。
COLLECTION_KNOWLEDGE = "knowledge_chunks"
COLLECTION_MEMORY = "conversation_memory"

# 分块参数。
CHUNK_SIZE = 500
OVERLAP = 50
MIN_CHUNK_LEN = 20
# 递归切片分隔符：优先按标题/段落/句子切，最后才按字符兜底，比固定窗口更能保留语义结构。
_CHUNK_SEPARATORS = ["\n### ", "\n## ", "\n# ", "\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]

# 文本清洗用正则（编译一次复用）。
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\ufeff]")  # 零宽不可见字符
_MULTI_BLANK = re.compile(r"[ \t\u3000]+")              # 连续空白（含全角空格）
_MULTI_NEWLINE = re.compile(r"\n{3,}")                  # 3 个以上换行压成 2 个
_PAGE_NUMBER = re.compile(r"^\s*[-—]?\s*\d{1,4}\s*[-—]?\s*$")  # 独立成行的页码

# 落库的解析全文上限（字符）。MEDIUMTEXT 上限 16MB 字节，按 utf8mb4 最坏 4 字节/字符
# 折算约 400 万字符，取 300 万留足余量；超长文档在阅读器里只展示前段，切片与向量仍完整。
MAX_CONTENT_CHARS = 3_000_000


# ============================================================
# 1) 文件文本提取（PDF / DOCX / TXT）
# ============================================================

def _normalize(text: str) -> str:
    """统一清洗：去不可见字符 / 合并多空格 / 压缩连续换行 / 删除独立页码行，并保留段落边界。"""
    text = _INVISIBLE.sub("", text)
    lines: list[str] = []
    for ln in text.splitlines():
        stripped = ln.strip()
        # 删除独立成行的页码（PDF 抽取常见噪声），但保留空行作为段落分隔。
        if stripped and _PAGE_NUMBER.match(stripped):
            continue
        lines.append(stripped)
    text = "\n".join(lines)
    text = _MULTI_BLANK.sub(" ", text)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


def _parse_pdf(path: Path) -> str:
    """
    用 pypdfium2 抽取每页文本；若平均每页字符数过低，判定为扫描版并走火山 OCR 兜底。

    教学说明：电子版 PDF（文字可选中）走 pypdfium2，速度快、零成本；
    扫描版 PDF（页面是图片）才走 OCR，避免对所有 PDF 收取 OCR 成本。
    """
    pages: list[str] = []
    pdf = pdfium.PdfDocument(str(path))
    try:
        for page in pdf:
            textpage = page.get_textpage()
            t = textpage.get_text_range()
            textpage.close()
            page.close()
            if t and t.strip():
                pages.append(t.strip())
    finally:
        pdf.close()

    text = "\n".join(pages)
    page_count = max(len(pages), 1)
    avg_chars = len(text) / page_count
    print(f"[RAG][PDF] pypdfium2 抽取：页数={len(pages)}, 平均每页={avg_chars:.1f} 字")

    # 触发条件 1：完全没抽到文本（多见于纯扫描件）
    # 触发条件 2：平均每页字符数过低（可能是扫描件 + 个别页有零星可选文字）
    if not text or avg_chars < _SCANNED_PDF_AVG_CHARS:
        print("[RAG][PDF] 判定为扫描版，走火山 OCR 兜底")
        return ocr_pdf(path)

    return text


def _parse_docx(path: Path) -> str:
    """提取 Word 文本；通过段落大纲级别识别标题并注入 Markdown 标题标记，兼容中英文 Word。"""
    document = docx.Document(str(path))
    lines: list[str] = []
    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            lines.append("")
            continue

        pPr = para._p.pPr
        outline = pPr.find(qn("w:outlineLvl")) if pPr is not None else None
        outline_lvl = int(outline.get(qn("w:val"))) if outline is not None else None
        print(outline_lvl,'标题')
        if outline_lvl == 0:
            lines.append(f"# {text}")
        elif outline_lvl == 1:
            lines.append(f"## {text}")
        elif outline_lvl == 2:
            lines.append(f"### {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


def _parse_txt(path: Path) -> str:
    # errors="ignore" 兼容编码不规范的文件。
    return path.read_text(encoding="utf-8", errors="ignore")


_PARSERS = {".pdf": _parse_pdf, ".docx": _parse_docx, ".txt": _parse_txt}
SUPPORTED_EXTENSIONS = frozenset(_PARSERS.keys())

# 扫描版 PDF 判定阈值：平均每页字符数 < 该值则判定为扫描版，触发火山 OCR 兜底。
_SCANNED_PDF_AVG_CHARS = 20


def _extract_text(file_path: Path) -> str:
    """按扩展名分派解析器并统一清洗。"""
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在：{file_path}")
    # 字典分派：新增格式只需在 _PARSERS 里加一行。
    parser = _PARSERS.get(file_path.suffix.lower())
    if parser is None:
        raise ValueError(f"不支持的文件类型：{file_path.suffix}")
    return _normalize(parser(file_path))


# ============================================================
# 2) 文本分块（递归智能切片 + MD5 去重）
# ============================================================

def _filter_and_dedupe_chunks(chunks: list[str]) -> list[str]:
    """过滤过短文本块，并按 MD5 指纹去重，保留首次出现顺序。"""
    seen, unique = set(), []
    for chunk in chunks:
        chunk = chunk.strip()
        if len(chunk) < MIN_CHUNK_LEN:
            continue
        fp = hashlib.md5(chunk.encode("utf-8")).hexdigest()
        if fp not in seen:
            seen.add(fp)
            unique.append(chunk)
    return unique

# 旧方案：固定窗口 + 句子边界回退。
# 保留作教学对比：它能避免一部分硬切断，但主要仍按长度推进，无法优先感知标题/段落结构。
# def _split_chunks(text: str) -> list[str]:
#     """切分长文本：保证 start 单调推进；MD5 指纹完全相同的块去重。"""
#     if not text:
#         return []
#
#     chunks: list[str] = []
#     start, n = 0, len(text)
#     while start < n:
#         end = min(start + CHUNK_SIZE, n)
#         # 非末尾窗口尝试回退到最近的句子终止符，避免硬切断句子。
#         if end < n:
#             for i in range(end, max(start + OVERLAP, end - 100), -1):
#                 if text[i] in _SENTENCE_ENDS:
#                     end = i + 1
#                     break
#
#         chunk = text[start:end].strip()
#         if len(chunk) >= MIN_CHUNK_LEN:
#             chunks.append(chunk)
#
#         # 窗口已触底：本轮是最后一块，直接退出。
#         if end >= n:
#             break
#
#         # 推进步长 = end - overlap；若回退后没前进则强制 +1，杜绝死循环。
#         nxt = end - OVERLAP
#         start = nxt if nxt > start else start + 1
#
#     # MD5 指纹去重，保留首次出现顺序。
#     seen, unique = set(), []
#     for c in chunks:
#         fp = hashlib.md5(c.encode("utf-8")).hexdigest()
#         if fp not in seen:
#             seen.add(fp)
#             unique.append(c)
#     print(f"[RAG][分块] 切分前={len(chunks)} 块, 去重后={len(unique)} 块")
#     return unique


def _split_chunks(text: str) -> list[str]:
    """递归切分长文本：优先按标题/段落/句子边界切分；MD5 指纹完全相同的块去重。"""
    if not text:
        return []

    # 新方案：递归切片优先保留文档结构，标题/段落切不开时再逐级降级到句子、标点和字符。
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=OVERLAP,
        separators=_CHUNK_SEPARATORS,
    )
    chunks = splitter.split_text(text)
    unique = _filter_and_dedupe_chunks(chunks)
    print(f"[RAG][分块] 切分前={len(chunks)} 块, 去重后={len(unique)} 块")
    return unique


# ============================================================
# 3) DashScope 向量化（批量 + 状态校验）
# ============================================================

def _generate_embeddings(texts: list[str]) -> list[list[float]]:
    """批量调用 DashScope；按 text_index 还原顺序对齐输入。"""
    if not texts:
        return []
    if not settings.dashscope_api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置，无法生成向量")

    dashscope.api_key = settings.dashscope_api_key
    all_vectors: list[list[float]] = []

    # DashScope text-embedding-v4 单次最多 10 条，分批调用。
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        resp = TextEmbedding.call(
            model=EMBEDDING_MODEL,
            input=batch,
            dimension=EMBEDDING_DIM,
            text_type="document",
        )
        if getattr(resp, "status_code", None) != HTTPStatus.OK:
            raise RuntimeError(
                f"DashScope Embedding 调用失败：code={getattr(resp, 'code', None)}, "
                f"message={getattr(resp, 'message', None)}"
            )
        # 按 text_index 排序确保向量与原始文本一一对应。
        items = sorted(resp.output["embeddings"], key=lambda x: x["text_index"])
        all_vectors.extend(it["embedding"] for it in items)

    return all_vectors


def embed_texts(texts: list[str]) -> list[list[float]]:
    """对外的批量向量化入口（与入库同模型/同维度）。

    供「掌握度弱点归并」等非 RAG 场景复用：弱点是自由文本，需要同一套 embedding 才能算余弦。
    """
    return _generate_embeddings(texts)


# ============================================================
# 4) Qdrant 写入（UUID id + payload）
# ============================================================

def _get_qdrant_client() -> QdrantClient:
    return QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)


def _ensure_collection(client: QdrantClient) -> None:
    """文件知识库 collection 不存在时按维度创建，余弦相似度。"""
    if not client.collection_exists(COLLECTION_KNOWLEDGE):
        client.create_collection(
            collection_name=COLLECTION_KNOWLEDGE,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )


def _upsert_chunks(
    chunks: list[str],
    vectors: list[list[float]],
    user_id: int,
    resource_id: int,
    file_name: str,
) -> tuple[int, list[str]]:
    """批量写入 Qdrant 向量，payload 只存过滤字段；元数据存 MySQL，检索靠 vector_id 回查原文。

    **这里故意不做"先删旧点"**。历史上它按 `(user_id, file_name)` 删，理由是"同一份资料重复上传
    不要留下新旧两批点"。那个做法有两个坑，都被实测踩过：

    1. payload 里没有 `resource_id`，`file_name` 就成了"这份资料"的代理键——同一用户可以有
       **同名不同资料**（两个知识库里都有 `简历.pdf`），于是一次入库会把**别人的**向量删掉；
    2. 新点拿的是**新 uuid**，而 MySQL 的 `knowledge_chunks.vector_id` 不会跟着更新 → 旧切片行
       指向已删的点，检索里永远回查不到原文（"入库看着成功、内容搜不到"）。

    正确做法是由调用方（`RagService.ingest_from_bytes`）**按 `resource_id` 从 MySQL 读出旧
    `vector_id`、等新切片提交成功后再按 id 精确删**。这么做的前提是 `resource_id` 在 payload 里
    （下面写进去了），所以"这份资料有哪些点"以后永远是可精确判断的，不再依赖文件名猜测。
    用 `uuid` 作为 point id 的策略不变：它同时是 MySQL 那一列的取值，双向定位同一片段。
    """
    if not chunks or not vectors:
        return 0, []
    if len(chunks) != len(vectors):
        raise ValueError("chunks 与 vectors 长度不一致")

    client = _get_qdrant_client()
    _ensure_collection(client)

    points = []
    vector_ids: list[str] = []
    for chunk_idx, (chunk_text, chunk_vector) in enumerate(zip(chunks, vectors)):
        vid = str(uuid.uuid4())
        vector_ids.append(vid)
        point = PointStruct(
            id=vid,
            vector=chunk_vector,
            payload={
                # 仅保留过滤/定位必需字段；原文不写入以节省 Qdrant 存储（原文在 MySQL）。
                "user_id": user_id,
                # resource_id 是"这份资料"的权威标识，替换历史的 (user_id, file_name) 代理键。
                "resource_id": resource_id,
                # file_name 仍写入：检索结果要显示来源文件名，且它是覆盖语义的历史字段。
                "file_name": file_name,
                "chunk_index": chunk_idx,
            },
        )
        points.append(point)

    client.upsert(collection_name=COLLECTION_KNOWLEDGE, points=points)
    print(f"[RAG][写库] 写入 {len(points)} 个 point, resource_id={resource_id}")
    return len(points), vector_ids


# ============================================================
# 5) 一站式摄入入口（对外唯一公开函数）
# ============================================================

@dataclass
class IngestResult:
    """摄入结果摘要：透传给上层接口，同时携带 MySQL 备份所需的 chunks 与 vector_ids。"""

    file_name: str
    chunk_count: int
    chunks: list[str]
    vector_ids: list[str]
    # 解析清洗后的全文：上游落进 resources.content_text，供知识库阅读器使用。
    text: str = ""


def ingest_file(
    file_path: str | Path,
    user_id: int,
    resource_id: int,
    original_file_name: str | None = None,
) -> IngestResult:
    """单文件摄入：解析 → 清洗 → 分块去重 → 向量化 → 写 Qdrant。

    `resource_id` 会写进 Qdrant payload，是"这份资料的点"的**唯一可靠标识**：
    payload 里没有它时，只能按 `(user_id, file_name)` 猜，而同一用户可以有**同名不同资料**
    （典型：两个知识库里都有 `简历.pdf`），猜错就会删掉别人的向量。**必填**。
    """
    path = Path(file_path)
    file_name = original_file_name or path.name
    print(
        f"\n[RAG][入口] === 开始摄入 file_name={file_name}, "
        f"user_id={user_id}, resource_id={resource_id} ==="
    )

    text = _extract_text(path)
    print(f"[RAG][解析] 清洗后文本长度={len(text)} 字符")

    chunks = _split_chunks(text)
    vectors = _generate_embeddings(chunks)
    count, vector_ids = _upsert_chunks(
        chunks=chunks,
        vectors=vectors,
        user_id=user_id,
        resource_id=resource_id,
        file_name=file_name,
    )
    print(f"[RAG][入口] === 摄入完成 file_name={file_name}, chunk_count={count} ===\n")
    return IngestResult(
        file_name=file_name,
        chunk_count=count,
        chunks=chunks,
        vector_ids=vector_ids,
        text=text,
    )

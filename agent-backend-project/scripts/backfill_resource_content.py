"""给老资料补 `resources.content_text`（知识库阅读器的完整正文）。

背景：`content_text` 是 2026-10-18 新增的列，**入库时**由 `RagService.ingest_from_bytes`
写入解析清洗后的全文。这个列之前上传的资料是空的，阅读器只能"用切片拼回"
（`app/rag/merge_text.py`，已消除块间重叠，但排版仍有损失）。

这个脚本把老资料补齐：从 `knowledge_chunks` 按 `chunk_index` 读回、合并、写回
`content_text` / `content_chars`。**纯本地、不调模型、不碰 Qdrant**，可反复执行。

用法（在 agent-backend-project 目录下）：

    python scripts/backfill_resource_content.py --dry-run      # 先看会改哪些，不写库
    python scripts/backfill_resource_content.py --all          # 全部缺正文的 ready 资料
    python scripts/backfill_resource_content.py --resource-id 2
    python scripts/backfill_resource_content.py --all --overwrite   # 已有正文也重算

注意：拼回的是**切片文本**，不是重新解析原文件。想要"最权威"的全文（与 PDF 排版一致度更高），
把原文件重新上传一次即可（重传命中去重时会重新入库并写入 content_text）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import SessionLocal
from app.db.models import KnowledgeChunk, Resource
from app.rag.core import MAX_CONTENT_CHARS
from app.rag.merge_text import join_chunks


def _load_chunk_texts(db, resource_id: int) -> list[str]:
    """按 chunk_index 升序取回切片原文。"""
    rows = db.scalars(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.resource_id == resource_id)
        .order_by(KnowledgeChunk.chunk_index.asc())
    )
    return [str(chunk.text or "") for chunk in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="补齐 resources.content_text")
    parser.add_argument("--resource-id", type=int, default=None, help="只处理这一份资料")
    parser.add_argument("--all", action="store_true", help="处理所有还缺正文的资料")
    parser.add_argument(
        "--overwrite", action="store_true", help="已有 content_text 也重算（默认跳过）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    args = parser.parse_args()

    if not args.all and args.resource_id is None:
        parser.error("要么 --all，要么 --resource-id <id>")

    db = SessionLocal()
    try:
        stmt = select(Resource).order_by(Resource.id.asc())
        if args.resource_id is not None:
            stmt = stmt.where(Resource.id == args.resource_id)
        resources = list(db.scalars(stmt))
        if not resources:
            print("没有匹配的资料")
            return 0

        changed = skipped = empty = 0
        for resource in resources:
            if resource.content_text and not args.overwrite:
                print(f"[跳过] id={resource.id} {resource.file_name}（已有正文 {resource.content_chars} 字）")
                skipped += 1
                continue

            chunks = _load_chunk_texts(db, resource.id)
            text = join_chunks(chunks).strip()
            if not text:
                print(f"[空]   id={resource.id} {resource.file_name}（没有切片可拼，可能还没入库成功）")
                empty += 1
                continue

            truncated = len(text) > MAX_CONTENT_CHARS
            if truncated:
                text = text[:MAX_CONTENT_CHARS]
            print(
                f"[补齐] id={resource.id} {resource.file_name}：{len(chunks)} 片 → {len(text)} 字"
                + ("（已截断到上限）" if truncated else "")
            )
            if not args.dry_run:
                resource.content_text = text
                resource.content_chars = len(text)
            changed += 1

        if args.dry_run:
            print(f"\n[dry-run] 将补齐 {changed} 份，跳过 {skipped} 份，无内容 {empty} 份（未写库）")
        else:
            db.commit()
            print(f"\n完成：补齐 {changed} 份，跳过 {skipped} 份，无内容 {empty} 份")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

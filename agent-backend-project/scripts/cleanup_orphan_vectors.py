"""清理 Qdrant 里的**孤儿向量点**（`knowledge_chunks` 里已经没有对应行的点）。

什么时候会有孤儿：

- 入库中途失败（Qdrant 写成功、MySQL 提交失败）——原始代码里 `_upsert_chunks` 先写 Qdrant
  再 `save_chunks`，两步之间崩溃就会留下；
- 手写的验证/临时脚本建的合成点（本项目的 `verify_kb_document_*.py` 会自清理，但中途崩溃会留）；
- 早期版本的 payload 结构不同（比如没有 `user_id`），后来换了 collection 却没有清理。

**危害**：孤儿点不会被检索到（检索走 `vector_id` 回查 MySQL 原文，查不到就丢弃），
所以不影响正确性，只占存储、让 `points_count` 对不上号。

**默认 dry-run**：不加 `--apply` 只统计和打印，绝不删。真删要显式 `--apply`。

用法（在 agent-backend-project 目录下）：

    python scripts/cleanup_orphan_vectors.py                # 只看，不删
    python scripts/cleanup_orphan_vectors.py --apply        # 真的删
    python scripts/cleanup_orphan_vectors.py --apply --limit 100
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
from app.db.models import KnowledgeChunk
from app.rag.core import COLLECTION_KNOWLEDGE, _get_qdrant_client

SCROLL_PAGE = 256


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 Qdrant 孤儿向量点")
    parser.add_argument("--apply", action="store_true", help="真的删除（默认只统计）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少个孤儿点（0=不限）")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        known = set(db.scalars(select(KnowledgeChunk.vector_id)))
        print(f"MySQL 里有 {len(known)} 个 vector_id")

        client = _get_qdrant_client()
        if not client.collection_exists(COLLECTION_KNOWLEDGE):
            print(f"Qdrant 里没有 collection {COLLECTION_KNOWLEDGE}，无需清理")
            return 0

        orphans: list[str] = []
        offset = None
        scanned = 0
        truncated = False
        while True:
            points, offset = client.scroll(
                collection_name=COLLECTION_KNOWLEDGE,
                limit=SCROLL_PAGE,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            for point in points:
                scanned += 1
                if str(point.id) not in known:
                    orphans.append(str(point.id))
                    # 在**收集时**就截断：以前是在整页扫完之后才判断，一页 256 个点会让
                    # `--limit 10` 实际收集到最多 256 个，选项形同虚设。
                    if args.limit and len(orphans) >= args.limit:
                        truncated = True
                        break
            if truncated or offset is None:
                break

        print(f"Qdrant 扫描 {scanned} 个点，其中孤儿 {len(orphans)} 个")
        for vid in orphans[:10]:
            print(f"  - {vid}")
        if len(orphans) > 10:
            print(f"  …（其余 {len(orphans) - 10} 个略）")

        if not orphans:
            print("没有孤儿点，无需清理")
            return 0
        if not args.apply:
            print("\n[dry-run] 什么都没删。确认无误后加 --apply 再跑一次。")
            return 0

        targets = orphans[: args.limit] if args.limit else orphans
        # **逐个删，不整批**：Qdrant 的 points 只接受 UUID 或整数（json 数字），
        # 而历史遗留点的 id 有可能是字符串数字（如 "0"）——整批删会被判格式错误 400，
        # 结果一条都删不掉。逐个删还能把失败范围限制在单个脏点上。
        deleted = 0
        failed = 0
        for vid in targets:
            try:
                client.delete(
                    collection_name=COLLECTION_KNOWLEDGE,
                    points_selector=_id_selector(vid),
                    wait=True,
                )
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if failed <= 5:
                    print(f"  删除失败 {vid}: {str(exc)[:120]}")
        print(f"\n已删除 {deleted} / {len(targets)} 个孤儿点（失败 {failed}）")
        remaining = client.get_collection(COLLECTION_KNOWLEDGE).points_count
        print(f"删除后 collection 点数：{remaining}")
        return 0 if deleted == len(targets) else 1
    finally:
        db.close()


def _id_selector(raw: str):
    """按 id 形态构造删除选择器：纯数字用 int（历史点可能是 json 数字），否则原样传字符串。"""
    if raw.lstrip("-").isdigit():
        return [int(raw)]
    return [raw]


if __name__ == "__main__":
    raise SystemExit(main())

"""验收：知识库文档阅读（离线版，直接打服务层，不起 HTTP）。

为什么直接打服务层：验收要盯的是"数据取对了没有"，路由层只是参数接入；
HTTP 那层留给 `scripts/` 里的端到端脚本（需要后端在跑）。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_kb_documents.py --kb-id 50
    python scripts/verify_kb_documents.py --kb-id 50 --resource-id 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import KnowledgeBase
from app.services.knowledge_base_service import KnowledgeBaseService


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库文档阅读验收")
    parser.add_argument("--kb-id", type=int, default=None, help="知识库ID（不传则取第一个）")
    parser.add_argument("--resource-id", type=int, default=None, help="只看这份文档的详情")
    args = parser.parse_args()

    db = SessionLocal()
    passed = failed = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {name} {extra}")
        else:
            failed += 1
            print(f"  FAIL {name} {extra}")

    try:
        knowledge_base = (
            db.get(KnowledgeBase, args.kb_id)
            if args.kb_id
            else db.scalar(select(KnowledgeBase).order_by(KnowledgeBase.id.asc()))
        )
        if knowledge_base is None:
            print("没有知识库可验收")
            return 1
        user_id = knowledge_base.user_id
        kb_id = knowledge_base.id
        print(f"知识库 id={kb_id} name={knowledge_base.name} user_id={user_id}")

        print("\n[1] 库内文档列表")
        items = KnowledgeBaseService.list_resources(
            db, user_id=user_id, knowledge_base_id=kb_id
        )
        check("列表非空", bool(items), f"共 {len(items)} 份")
        if not items:
            return 1
        item = items[0] if args.resource_id is None else next(
            (row for row in items if row["id"] == args.resource_id), items[0]
        )
        print(f"      首份：{item}")
        for field in (
            "id", "file_name", "group_name", "ingest_status",
            "chunk_count", "char_count", "has_text", "can_preview", "create_time",
        ):
            check(f"列表项含 {field}", field in item)
        check("不含正文字段（列表要轻）", "text" not in item)
        check("create_time 是 Unix 秒", isinstance(item["create_time"], int) and item["create_time"] > 1_600_000_000)

        print("\n[2] 文档详情")
        detail = KnowledgeBaseService.get_resource_detail(
            db, user_id=user_id, resource_id=item["id"]
        )
        print(f"      text_source={detail['text_source']} text_chars={detail['text_chars']} "
              f"chunk_count={detail['chunk_count']} can_preview={detail['can_preview']}")
        check("有正文", detail["text_chars"] > 0)
        check("text_source 合法", detail["text_source"] in {"original", "chunks", "none"})
        check("切片按序返回", [c["chunk_index"] for c in detail["chunks"]] == sorted(
            c["chunk_index"] for c in detail["chunks"]))
        check("切片数一致", len(detail["chunks"]) == detail["chunk_count"])
        if detail["chunks"]:
            check("正文长度 ≥ 最长切片", detail["text_chars"] >= max(
                c["char_count"] for c in detail["chunks"]))
        if detail["can_preview"]:
            # 预览走后端代理（服务端路径 + 必须带令牌），不是 COS 直链——原因见路由注释：
            # 2024 年后创建的桶用 COS 默认域名一律被强制下载，无法内联预览。
            check("预览路径是后端代理路径", str(detail["preview_path"]).startswith("/knowledge-bases/"),
                  str(detail["preview_path"]))
            check("预览路径指向本资料", str(detail["preview_path"]).endswith(f"/resources/{item['id']}/file"))
        else:
            check("不可预览时路径为空", detail["preview_path"] is None)

        print("\n[3] 按考纲 source_ref 定位切片")
        quiz_core = knowledge_base.quiz_core or {}
        sections = [s for s in (quiz_core.get("sections") or []) if isinstance(s, dict)]
        if sections:
            ref = sections[0].get("source_ref") or {}
            print(f"      章节「{sections[0].get('heading')}」 -> {ref}")
            if isinstance(ref.get("chunk_from"), int):
                chunk = KnowledgeBaseService.get_chunk(
                    db, user_id=user_id, resource_id=item["id"], chunk_index=ref["chunk_from"]
                )
                check("按 chunk_from 取到片段", chunk["chunk"]["chunk_index"] == ref["chunk_from"])
                check("片段非空", len(chunk["chunk"]["text"]) > 0)
                check("返回了所属文件名", bool(chunk["resource_name"]))
        else:
            print("      （该库没有考纲章节，跳过）")

        print("\n[4] 越权与不存在")
        try:
            KnowledgeBaseService.get_resource_detail(db, user_id=user_id + 999_999, resource_id=item["id"])
            check("别人的资料应 404", False)
        except Exception as exc:  # noqa: BLE001
            check("别人的资料应 404", "不存在" in str(exc), type(exc).__name__)
        try:
            KnowledgeBaseService.get_chunk(db, user_id=user_id, resource_id=item["id"], chunk_index=999_999)
            check("不存在的片段应 404", False)
        except Exception as exc:  # noqa: BLE001
            check("不存在的片段应 404", "不存在" in str(exc), type(exc).__name__)

        print("\n[5] 分组列表")
        group_rows = KnowledgeBaseService.list_groups(db, user_id=user_id, knowledge_base_id=kb_id)
        check("分组列表非空", bool(group_rows))
        check("分组项字段齐全", all({"name", "resource_count"} <= set(row) for row in group_rows))
        check(
            "未分组（name=None）排在最后",
            all(row["name"] is not None for row in group_rows[:-1]),
            str([row["name"] for row in group_rows]),
        )

        print("\n[6] 库内搜索")
        # 用真实文件名片段搜（中文文件名里的连续一段）
        stem = item["file_name"].rsplit(".", 1)[0]
        name_keyword = stem[:4] if len(stem) >= 4 else stem
        hits = KnowledgeBaseService.list_resources(
            db, user_id=user_id, knowledge_base_id=kb_id, keyword=name_keyword
        )
        check("按文件名能搜到", bool(hits), f"关键词「{name_keyword}」命中 {len(hits)} 份")
        if hits:
            check("命中项带 match_field", hits[0]["match_field"] in {"name", "both"})
        # 用正文里真实出现的一段搜（切第一片中间 8 个字）
        body_keyword = ""
        if detail["chunks"]:
            raw = detail["chunks"][0]["text"]
            if len(raw) >= 30:
                body_keyword = raw[10:18].strip()
        if body_keyword:
            body_hits = KnowledgeBaseService.list_resources(
                db, user_id=user_id, knowledge_base_id=kb_id, keyword=body_keyword
            )
            check("按正文能搜到", bool(body_hits), f"关键词「{body_keyword}」命中 {len(body_hits)} 份")
            if body_hits:
                first = body_hits[0]
                check("正文命中数 ≥ 1", first["matched_chunk_count"] >= 1)
                check("带命中片段", bool(first["match_snippets"]))
                check(
                    "片段里含关键词",
                    any(body_keyword in snippet for snippet in first["match_snippets"]),
                    str(first["match_snippets"][:1]),
                )
        else:
            print("      （首片太短，跳过正文搜索）")
        # LIKE 通配符必须被转义：搜 % 不该命中全库
        wildcard_hits = KnowledgeBaseService.list_resources(
            db, user_id=user_id, knowledge_base_id=kb_id, keyword="%"
        )
        check("搜 % 不会命中全库", len(wildcard_hits) == 0, f"命中 {len(wildcard_hits)} 份")
        missed = KnowledgeBaseService.list_resources(
            db, user_id=user_id, knowledge_base_id=kb_id, keyword="绝不存在的关键词zzzq"
        )
        check("搜不存在的词返回空", missed == [])

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

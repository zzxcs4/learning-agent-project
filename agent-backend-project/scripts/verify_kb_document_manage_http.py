"""验收：库内文档**管理接口**的 HTTP 层（路由 + 鉴权 + 真实响应体，不起 uvicorn）。

为什么要有这一套（`verify_kb_document_manage.py` 已经验过 service 层了）：
service 层直接调函数，**绕过了路由**。而「改 / 删」这两类接口最容易在路由层出事：
路径里的 `knowledge_base_id` 有没有和资料真正的归属比对、请求体是 `PATCH` 部分更新还是全量、
`delete_file` 这个 query 参数的默认值、返回体字段名与前端 TS 约定是否一致——这些只有打 HTTP 才能发现。

重点覆盖：
1. 未带令牌一律拦住；`groups` 接口存在且 shape 正确（含"未分组"这一档）；
2. `PATCH` 的部分更新语义（只改传了的字段）、空名/超长名被拦、跨库资料被拒（404 而不是静默改掉）；
3. `DELETE` 真删：切片 + Qdrant 向量一起清、原文件从 COS 删掉，且**默认就删文件**；
4. 库内搜索 `keyword` 的命中归因（`match_field` / `match_snippets`），以及 LIKE 通配符转义。

为什么全程自建临时数据：改名与删除会**真改数据、真删文件**，绝不能拿真实资料试。
临时账号 + 临时知识库 + 资料全在 finally 里清干净；原文件用的是这里现传的一个临时对象。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_kb_document_manage_http.py
    python scripts/verify_kb_document_manage_http.py --keep   # 不清理，方便手工看数据
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from app.core.settings import settings as app_settings
from app.db.database import SessionLocal
from app.db.models import KnowledgeBase, KnowledgeChunk, Resource, User
from app.main import app
from app.rag import delete_vectors_by_ids, save_chunks
from app.rag.core import COLLECTION_KNOWLEDGE, EMBEDDING_DIM, _get_qdrant_client
from app.security import hash_password
from app.services.cos_storage_service import CosStorageService

TEMP_PASSWORD = "verify_pass_123"

# 一份最小可用 PDF：真的能被 /file 接口流出来，用来验证"删除原文件"这条链路。
MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _make_resource(
    db, *, kb_id: int, user_id: int, name: str, chunks: list[str], storage_path: str = ""
) -> tuple[Resource, list[str]]:
    """造一份带真实切片与真实 Qdrant 点的临时资料。

    `vector_id` 必须是**真 UUID**：Qdrant 的 point id 只接受 UUID/整数。这里用真的，
    删除时才能真正验证"向量被清掉了"（而且 `delete_vectors_by_ids` 会把非法 id 静默过滤，
    用假 id 会让这条断言变成假通过）。
    """
    resource = Resource(
        resource_type=0,
        storage_scene=0,
        upload_purpose=0,
        file_name=name,
        file_hash=f"mh{int(time.time() * 1000) % 10**10}".ljust(32, "0")[:32],
        storage_path=storage_path,
        knowledge_base_id=kb_id,
        ingest_status="ready",
        user_id=user_id,
    )
    db.add(resource)
    db.flush()
    vector_ids = [str(uuid.uuid4()) for _ in chunks]
    save_chunks(db, resource.id, chunks, vector_ids)
    db.commit()
    db.refresh(resource)

    # 真写 Qdrant，好让"删除时连向量一起清"可验证
    client = _get_qdrant_client()
    if not client.collection_exists(COLLECTION_KNOWLEDGE):
        from qdrant_client.models import Distance, VectorParams

        client.create_collection(
            collection_name=COLLECTION_KNOWLEDGE,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
    client.upsert(
        collection_name=COLLECTION_KNOWLEDGE,
        points=[
            {
                "id": vid,
                "vector": [0.0] * EMBEDDING_DIM,
                "payload": {
                    "user_id": user_id,
                    "file_name": name,
                    "chunk_index": index,
                },
            }
            for index, vid in enumerate(vector_ids)
        ],
    )
    return resource, vector_ids


def _points_alive(vector_ids: list[str]) -> set[str]:
    """回查 Qdrant，返回仍然存在的 point id 集合。"""
    if not vector_ids:
        return set()
    client = _get_qdrant_client()
    if not client.collection_exists(COLLECTION_KNOWLEDGE):
        return set()
    records = client.retrieve(
        collection_name=COLLECTION_KNOWLEDGE, ids=vector_ids, with_payload=False
    )
    return {str(record.id) for record in records}


def main() -> int:
    parser = argparse.ArgumentParser(description="库内文档管理接口 HTTP 验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
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

    suffix = str(int(time.time()))[-8:]
    username = f"vkmh{suffix}"  # users.username 只有 20 字符
    user_id = kb_id = None
    probe_path = None
    alpha_vectors: list[str] = []
    orphan_vectors_to_clean: list[str] = []
    try:
        user = User(username=username, password=hash_password(TEMP_PASSWORD))
        db.add(user)
        db.flush()
        user_id = user.id
        knowledge_base = KnowledgeBase(user_id=user_id, name=f"临时管理接口验收库-{suffix}")
        db.add(knowledge_base)
        db.flush()
        kb_id = knowledge_base.id
        print(f"临时用户 id={user_id}（{username}） 知识库 id={kb_id}")

        # 上传一个临时对象，作为"要连原文件一起删"的那份资料
        cos_client = CosStorageService.get_client()
        bucket = app_settings.cos_bucket
        object_name = f"verify/manage-{suffix}.pdf"
        CosStorageService.put_object(
            cos_client, bucket, object_name, MINIMAL_PDF, len(MINIMAL_PDF), "application/pdf"
        )
        probe_path = f"cos://{bucket}/{object_name}"

        alpha, alpha_vectors = _make_resource(
            db, kb_id=kb_id, user_id=user_id, name="索引原理.md",
            chunks=["聚簇索引的叶子节点存整行数据。", "二级索引的叶子节点存主键值。"],
            storage_path=probe_path,
        )
        beta, _ = _make_resource(
            db, kb_id=kb_id, user_id=user_id, name="事务隔离级别.md",
            chunks=["REPEATABLE READ 下快照读不加锁。", "间隙锁用于防止幻读。"],
        )
        print(f"资料 alpha={alpha.id}（带原文件）beta={beta.id}")

        with TestClient(app) as client:
            # ---------------------------------------------------------- [1] 鉴权
            print("\n[1] 未带令牌 / 越权")
            for method, path in (
                ("get", f"/knowledge-bases/{kb_id}/groups"),
                ("patch", f"/knowledge-bases/{kb_id}/resources/{alpha.id}"),
                ("delete", f"/knowledge-bases/{kb_id}/resources/{alpha.id}"),
            ):
                resp = getattr(client, method)(path)
                check(
                    f"{method.upper()} {'/groups' if method == 'get' else '资料接口'} 未授权被拦",
                    resp.status_code in (401, 403),
                    f"-> {resp.status_code}",
                )

            reg = client.post(
                "/auth/register", json={"username": username, "password": TEMP_PASSWORD}
            )
            login = client.post(
                "/auth/login", json={"username": username, "password": TEMP_PASSWORD}
            )
            if login.status_code != 200:
                raise SystemExit(f"登录失败: {login.status_code} {login.text[:300]}")
            headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
            print(f"      注册临时账号 -> {reg.status_code}，登录 -> 200")

            # ---------------------------------------------------------- [2] 分组列表
            print("\n[2] 分组列表 GET /groups")
            resp = client.get(f"/knowledge-bases/{kb_id}/groups", headers=headers)
            check("分组 200", resp.status_code == 200, f"-> {resp.status_code}")
            data = resp.json()["data"]
            check("回显 knowledge_base_id", data["knowledge_base_id"] == kb_id)
            check("total 与 items 一致", data["total"] == len(data["items"]), str(data["total"]))
            check("未分组这一档存在", any(row["name"] is None for row in data["items"]))
            ungrouped = next(row for row in data["items"] if row["name"] is None)
            check("未分组文档数 = 2", ungrouped["resource_count"] == 2, str(ungrouped))
            check(
                "分组项字段与前端 TS 约定一致",
                all({"name", "resource_count"} == set(row) for row in data["items"]),
            )

            # ---------------------------------------------------------- [3] 部分更新语义
            print("\n[3] PATCH 部分更新语义")
            resp = client.patch(
                f"/knowledge-bases/{kb_id}/resources/{alpha.id}",
                headers=headers,
                json={"file_name": "B+树与索引.md"},
            )
            check("改名 200", resp.status_code == 200, f"-> {resp.status_code}")
            detail = resp.json()["data"]
            check("返回新文件名", detail["file_name"] == "B+树与索引.md", detail["file_name"])
            check("正文仍在", detail["text_chars"] > 0, f"{detail['text_chars']} 字")
            check("切片数没变", detail["chunk_count"] == 2, f"{detail['chunk_count']} 片")
            check("详情带 group_name 字段", "group_name" in detail)
            check(
                "返回体与详情接口同构",
                {"id", "file_name", "chunk_count", "text_source", "preview_path"} <= set(detail),
                str(sorted(detail)[:6]),
            )

            resp = client.patch(
                f"/knowledge-bases/{kb_id}/resources/{beta.id}",
                headers=headers,
                json={"group_name": "数据库"},
            )
            check("改分组 200", resp.status_code == 200, f"-> {resp.status_code}")
            check("分组已生效", resp.json()["data"]["group_name"] == "数据库")
            # 关键的部分更新语义：没传 file_name 就绝不能动它
            check("未传的字段没被清空", resp.json()["data"]["file_name"] == "事务隔离级别.md",
                  resp.json()["data"]["file_name"])

            resp = client.patch(
                f"/knowledge-bases/{kb_id}/resources/{beta.id}",
                headers=headers,
                json={"group_name": ""},
            )
            check("空字符串 = 移出分组", resp.status_code == 200 and resp.json()["data"]["group_name"] is None,
                  str(resp.json()["data"]["group_name"]))

            print("\n[3b] 非法入参必须被拦")
            resp = client.patch(
                f"/knowledge-bases/{kb_id}/resources/{alpha.id}",
                headers=headers,
                json={"file_name": "   "},
            )
            check("纯空白文件名 400", resp.status_code == 400, f"-> {resp.status_code}")
            resp = client.patch(
                f"/knowledge-bases/{kb_id}/resources/{alpha.id}",
                headers=headers,
                json={"file_name": ""},
            )
            check("空文件名被 schema 拦（422）", resp.status_code == 422, f"-> {resp.status_code}")
            resp = client.patch(
                f"/knowledge-bases/{kb_id}/resources/{alpha.id}",
                headers=headers,
                json={"file_name": "x" * 256},
            )
            check("超长文件名被 schema 拦（422）", resp.status_code == 422, f"-> {resp.status_code}")

            print("\n[3c] 路径里的库 ID 必须与资料真实归属比对")
            resp = client.patch(
                f"/knowledge-bases/{kb_id + 9999}/resources/{alpha.id}",
                headers=headers,
                json={"file_name": "偷改.md"},
            )
            check("挪到别的库 ID 下应 404", resp.status_code == 404, f"-> {resp.status_code}")
            resp = client.get(f"/knowledge-bases/{kb_id}/resources/{alpha.id}", headers=headers)
            check("资料没被偷改", resp.json()["data"]["file_name"] == "B+树与索引.md",
                  resp.json()["data"]["file_name"])

            # ---------------------------------------------------------- [4] 搜索
            print("\n[4] 库内搜索的命中归因")
            resp = client.get(
                f"/knowledge-bases/{kb_id}/resources", headers=headers, params={"keyword": "B+树"}
            )
            hit = resp.json()["data"]["items"][0]
            check("按文件名命中", hit["id"] == alpha.id and hit["match_field"] == "name",
                  f"{hit['file_name']} / {hit['match_field']}")

            resp = client.get(
                f"/knowledge-bases/{kb_id}/resources", headers=headers, params={"keyword": "间隙锁"}
            )
            data = resp.json()["data"]
            check("按正文命中", len(data["items"]) == 1 and data["items"][0]["id"] == beta.id,
                  f"命中 {len(data['items'])} 份")
            hit = data["items"][0]
            check("命中归因为 content", hit["match_field"] == "content", str(hit["match_field"]))
            check("带命中片段且含关键词", any("间隙锁" in s for s in hit["match_snippets"]),
                  str(hit["match_snippets"][:1]))
            check("回显本次关键词", data["keyword"] == "间隙锁", str(data["keyword"]))
            check("搜索时 matched_chunk_count ≥ 1", hit["matched_chunk_count"] >= 1,
                  str(hit["matched_chunk_count"]))

            resp = client.get(
                f"/knowledge-bases/{kb_id}/resources", headers=headers, params={"keyword": "索引"}
            )
            both = {row["id"]: row["match_field"] for row in resp.json()["data"]["items"]}
            check("文件名与正文都命中时归因 both", both.get(alpha.id) == "both", str(both))

            resp = client.get(
                f"/knowledge-bases/{kb_id}/resources", headers=headers, params={"keyword": "%"}
            )
            check("LIKE 通配符被转义（搜 % 不返回全库）",
                  resp.json()["data"]["items"] == [], f"命中 {len(resp.json()['data']['items'])} 份")
            resp = client.get(
                f"/knowledge-bases/{kb_id}/resources", headers=headers,
                params={"keyword": "_"},
            )
            check("下划线通配符被转义", resp.json()["data"]["items"] == [])
            resp = client.get(
                f"/knowledge-bases/{kb_id}/resources", headers=headers,
                params={"keyword": "绝不存在的词zzzq"},
            )
            check("搜不存在的词返回空", resp.json()["data"]["items"] == [])

            print("\n[4b] 非搜索时不带命中字段（列表要轻）")
            resp = client.get(f"/knowledge-bases/{kb_id}/resources", headers=headers)
            plain = resp.json()["data"]
            check("keyword 回显 null", plain["keyword"] is None)
            first = plain["items"][0]
            check("match_field 为 null", first["match_field"] is None, str(first["match_field"]))
            check("match_snippets 为空数组", first["match_snippets"] == [])
            check("非搜索时 chunk_count 是总数", first["chunk_count"] == 2, str(first["chunk_count"]))

            print("\n[4c] 分组过滤")
            client.patch(
                f"/knowledge-bases/{kb_id}/resources/{beta.id}",
                headers=headers,
                json={"group_name": "数据库"},
            )
            resp = client.get(
                f"/knowledge-bases/{kb_id}/resources", headers=headers,
                params={"group_name": "数据库"},
            )
            data = resp.json()["data"]
            check("只看某分组只返回组内文档",
                  len(data["items"]) == 1 and data["items"][0]["id"] == beta.id,
                  f"{len(data['items'])} 份")
            check("回显分组条件", data["group_name"] == "数据库")
            resp = client.get(
                f"/knowledge-bases/{kb_id}/resources", headers=headers,
                params={"group_name": "不存在的组"},
            )
            check("不存在的分组返回空", resp.json()["data"]["items"] == [])

            # ---------------------------------------------------------- [5] 删除
            print("\n[5] DELETE：切片 + 向量 + 原文件一起清")
            check("删除前 Qdrant 上确实有这些点", _points_alive(alpha_vectors) == set(alpha_vectors),
                  f"{len(alpha_vectors)} 个")
            resp = client.delete(
                f"/knowledge-bases/{kb_id}/resources/{alpha.id}", headers=headers
            )
            check("删除 200", resp.status_code == 200, f"-> {resp.status_code}")
            result = resp.json()["data"]
            check("返回体字段与前端 TS 约定一致",
                  {"resource_id", "file_name", "knowledge_base_id", "deleted_chunk_count",
                   "deleted_vector_count", "deleted_file", "removed_quiz_sections"} == set(result),
                  str(sorted(result)))
            check("删除切片数 = 2", result["deleted_chunk_count"] == 2, str(result))
            check("删除向量数 = 2", result["deleted_vector_count"] == 2, str(result))
            check("默认就连原文件一起删", result["deleted_file"] is True, str(result))
            check("回显原文件名", result["file_name"] == "B+树与索引.md", result["file_name"])

            # 关键：接口删数据用的是**另一个 Session**（走 TestClient），本脚本的 Session 还停在
            # 早先那次 SELECT 打开的 REPEATABLE READ 快照上，不结束事务就永远看不到删掉的行。
            # 但 rollback 会把 alpha/beta 这些 ORM 实例置为过期，之后读 `.id` 会抛
            # ObjectDeletedError（行确实没了），所以先把要用的标量取出来。
            alpha_id, beta_id = alpha.id, beta.id
            db.rollback()
            check("MySQL 切片已清空",
                  db.scalar(
                      select(func.count()).select_from(KnowledgeChunk)
                      .where(KnowledgeChunk.resource_id == alpha_id)
                  ) == 0)
            check("Qdrant 向量已清空", _points_alive(alpha_vectors) == set(), str(_points_alive(alpha_vectors)))
            check("资料行已删",
                  db.scalar(select(func.count()).select_from(Resource).where(Resource.id == alpha_id)) == 0)
            try:
                cos_client.head_object(Bucket=bucket, Key=object_name)
                check("COS 原文件已删", False, "对象仍然存在")
            except Exception:  # noqa: BLE001 - 取不到就是删掉了
                check("COS 原文件已删", True)
            if not probe_path:
                probe_path = f"cos://{bucket}/{object_name}"  # 仍存在时让 finally 兜底删
            else:
                probe_path = None  # 已删干净，finally 不必再动它

            resp = client.get(f"/knowledge-bases/{kb_id}/resources/{alpha_id}", headers=headers)
            check("删除后详情 404", resp.status_code == 404, f"-> {resp.status_code}")
            resp = client.delete(
                f"/knowledge-bases/{kb_id}/resources/{alpha_id}", headers=headers
            )
            check("重复删除 404（不是 500）", resp.status_code == 404, f"-> {resp.status_code}")

            print("\n[5b] delete_file=false 只出库、保留原文件")
            keep_object = f"verify/keep-{suffix}.txt"
            body = b"keep me"
            CosStorageService.put_object(cos_client, bucket, keep_object, body, len(body), "text/plain")
            gamma, gamma_vectors = _make_resource(
                db, kb_id=kb_id, user_id=user_id, name="只出库.md",
                chunks=["这份的原文件要留着。"],
                storage_path=f"cos://{bucket}/{keep_object}",
            )

            resp = client.delete(
                f"/knowledge-bases/{kb_id}/resources/{gamma.id}",
                headers=headers,
                params={"delete_file": "false"},
            )
            check("只出库 200", resp.status_code == 200, f"-> {resp.status_code}")
            check("报告未删原文件", resp.json()["data"]["deleted_file"] is False,
                  str(resp.json()["data"]))
            try:
                cos_client.head_object(Bucket=bucket, Key=keep_object)
                check("原文件仍在 COS 上", True)
            except Exception as exc:  # noqa: BLE001
                check("原文件仍在 COS 上", False, str(exc)[:80])
            check("向量仍被清掉", _points_alive(gamma_vectors) == set())

            print("\n[5c] 删资料同时摘掉它的考纲章节（HTTP 层）")
            # 此时 alpha 已被上面删掉，beta 还在。造一份考纲覆盖多种情况：
            # 指向已删资料 / 指向还在的资料 / 指向从来没存在过的资料 / 没有 source_ref。
            from sqlalchemy.orm.attributes import flag_modified

            knowledge_base.quiz_core = {
                "title": "临时考纲",
                "sections": [
                    {
                        "heading": "已删资料的章节",
                        "topics": ["聚簇索引"],
                        "source_ref": {"resource_id": alpha_id, "chunk_from": 0, "chunk_to": 1},
                    },
                    {
                        "heading": "还在的资料的章节",
                        "topics": ["间隙锁"],
                        "source_ref": {"resource_id": beta_id, "chunk_from": 0, "chunk_to": 0},
                    },
                    {
                        "heading": "从未存在的资料的章节",
                        "topics": ["幻读"],
                        "source_ref": {"resource_id": 2**31 - 1, "chunk_from": 0, "chunk_to": 0},
                    },
                    {"heading": "没有原文定位的章节", "topics": ["无来源"]},
                ],
            }
            flag_modified(knowledge_base, "quiz_core")
            db.commit()

            resp = client.get(f"/knowledge-bases/{kb_id}", headers=headers)
            check("详情接口 200", resp.status_code == 200, f"-> {resp.status_code}")
            sections = ((resp.json()["data"].get("quiz_core") or {}).get("sections")) or []
            statuses = [section.get("source_status") for section in sections]
            check(
                "详情接口给每个章节补了 source_status",
                statuses == ["missing", "ok", "missing", "unknown"],
                str(statuses),
            )
            db.refresh(knowledge_base)
            stored = (knowledge_base.quiz_core or {}).get("sections") or []
            check(
                "标注只加在响应里，没有写回库",
                all("source_status" not in section for section in stored),
                str([sorted(section.keys()) for section in stored]),
            )

            # 新造一份**还在**的资料，考纲里给它一章，然后真删它：章节必须一起消失
            victim, _ = _make_resource(
                db, kb_id=kb_id, user_id=user_id, name="待删章节.md", chunks=["这份要被删掉。"]
            )
            knowledge_base.quiz_core = {
                "title": "临时考纲",
                "sections": [
                    {
                        "heading": "待删资料的章节",
                        "topics": ["待删考点"],
                        "source_ref": {"resource_id": victim.id, "chunk_from": 0, "chunk_to": 0},
                    },
                    {
                        "heading": "还在的资料的章节",
                        "topics": ["间隙锁"],
                        "source_ref": {"resource_id": beta_id, "chunk_from": 0, "chunk_to": 0},
                    },
                ],
            }
            flag_modified(knowledge_base, "quiz_core")
            db.commit()

            resp = client.delete(
                f"/knowledge-bases/{kb_id}/resources/{victim.id}",
                headers=headers,
                params={"delete_file": "false"},
            )
            check("删除 200", resp.status_code == 200, f"-> {resp.status_code}")
            check(
                "删除响应报告摘掉的章节数",
                resp.json()["data"]["removed_quiz_sections"] == 1,
                str(resp.json()["data"]),
            )
            headings = [
                section.get("heading")
                for section in (
                    (client.get(f"/knowledge-bases/{kb_id}", headers=headers).json()["data"].get(
                        "quiz_core"
                    ) or {}).get("sections")
                    or []
                )
            ]
            check("待删资料的章节已从考纲消失", "待删资料的章节" not in headings, str(headings))
            check("其它资料的章节照旧保留", headings == ["还在的资料的章节"], str(headings))

            # ---------------------------------------------- [6] 未归档资料（删库留下的孤儿）
            print("\n[6] 未归档资料：看得见 / 收得回 / 删得掉（删库留下的孤儿资料）")
            # ⚠️ 不能拿上面删掉的那几份来验：`delete_file=false` 只是"不删 COS 原文件"，
            # 资料行与切片**照样删**。真正会制造孤儿资料的只有"删掉整个知识库"（只解绑、不删）。
            # 所以这里再建一个库、放一份资料，然后**把整个库删掉**造出孤儿。
            orphan_kb_id = None

            def make_orphan(name: str, chunks: list[str]) -> tuple[int, list[str]]:
                """建库 → 放一份资料 → 删库，返回（资料 id, 向量 id）。资料会被解绑保留。"""
                nonlocal orphan_kb_id
                tmp_kb = KnowledgeBase(user_id=user_id, name=f"临时孤儿来源库-{suffix}-{name}")
                db.add(tmp_kb)
                db.flush()
                tmp_kb_id = tmp_kb.id
                created, vectors = _make_resource(
                    db, kb_id=tmp_kb_id, user_id=user_id, name=name, chunks=chunks
                )
                created_id = created.id
                resp = client.delete(f"/knowledge-bases/{tmp_kb_id}", headers=headers)
                if resp.status_code != 200:
                    raise SystemExit(f"造孤儿失败（删库 {resp.status_code}: {resp.text[:200]}）")
                body = resp.json()["data"]
                if orphan_kb_id is None:
                    orphan_kb_id = tmp_kb_id
                    check(
                        "删库响应字段与前端 TS 约定一致",
                        set(body) == {
                            "knowledge_base_id", "released_resource_count",
                            "released_practice_count", "removed_mastery_count",
                            "retention_policy",
                        },
                        str(sorted(body)),
                    )
                    check(
                        "★ 删库响应带保留策略（调用方不必猜什么被保留）",
                        {part.strip() for part in body["retention_policy"].split(";")} == {
                            "resources_unbound", "practice_history_kept",
                            "weak_points_generalized", "mastery_removed",
                        },
                        body["retention_policy"],
                    )
                db.rollback()
                return created_id, list(vectors)

            orphan_id, orphan_vectors = make_orphan("孤儿资料.md", ["孤儿片段甲", "孤儿片段乙"])
            orphan_vectors_to_clean.extend(orphan_vectors)

            resp = client.get("/knowledge-bases/unbound-resources", headers=headers)
            check("列表 200（路由没被 /{kb_id} 抢走）", resp.status_code == 200, f"-> {resp.status_code}")
            unbound = resp.json()["data"]
            unbound_ids = [row["id"] for row in unbound["items"]]
            check("解绑的资料出现在未归档列表里", orphan_id in unbound_ids, str(unbound_ids))
            row = next((r for r in unbound["items"] if r["id"] == orphan_id), {})
            check("带切片数（切片与向量仍保留，不是空壳）", row.get("chunk_count") == 2, str(row))
            check("恒不属于任何库", row.get("knowledge_base_id") is None)
            check(
                "字段与前端 TS 约定一致",
                set(row) == {
                    "id", "file_name", "group_name", "knowledge_base_id", "ingest_status",
                    "ingest_error", "chunk_count", "char_count", "has_text", "can_preview",
                    "create_time", "inactive_days",
                },
                str(sorted(row)),
            )
            check("未授权拿不到未归档列表",
                  client.get("/knowledge-bases/unbound-resources").status_code in (401, 403))

            # 收得回：归档到还活着的 kb
            resp = client.patch(
                f"/knowledge-bases/unbound-resources/{orphan_id}",
                headers=headers,
                json={"knowledge_base_id": kb_id},
            )
            check("归档回库 200", resp.status_code == 200, f"-> {resp.status_code}")
            check("归档后带上目标库 ID",
                  resp.json()["data"]["knowledge_base_id"] == kb_id, str(resp.json()["data"])[:120])
            after = client.get("/knowledge-bases/unbound-resources", headers=headers).json()["data"]
            check("归档后不再出现在未归档列表", orphan_id not in [r["id"] for r in after["items"]],
                  str([r["id"] for r in after["items"]]))
            resp = client.get(f"/knowledge-bases/{kb_id}/resources/{orphan_id}", headers=headers)
            check("★ 归档是纯元数据变更（同一 resource_id 直接可读，没重新入库）",
                  resp.status_code == 200, f"-> {resp.status_code}")
            check("切片数没有变（没有重算向量）",
                  len(resp.json()["data"]["chunks"]) == 2, str(len(resp.json()["data"]["chunks"])))

            # 删得掉：再造一个孤儿，走未归档删除接口
            orphan2_id, orphan2_vectors = make_orphan("孤儿资料2.md", ["孤儿片段"])
            orphan_vectors_to_clean.extend(orphan2_vectors)
            resp = client.delete(
                f"/knowledge-bases/unbound-resources/{orphan2_id}",
                headers=headers,
                params={"delete_file": "false"},
            )
            check("未归档删除 200", resp.status_code == 200, f"-> {resp.status_code}")
            check("报告删除的切片数", resp.json()["data"]["deleted_chunk_count"] == 1,
                  str(resp.json()["data"]))
            gone = client.get("/knowledge-bases/unbound-resources", headers=headers).json()["data"]
            check("删除后列表里也没有了", orphan2_id not in [r["id"] for r in gone["items"]])
            check(
                "还属于某个库的资料不能走未归档删除口（409，避免绕过归属校验）",
                client.delete(
                    f"/knowledge-bases/unbound-resources/{beta_id}", headers=headers
                ).status_code == 409,
            )
            orphan_vectors_to_clean = list(orphan_vectors) + list(orphan2_vectors)
            orphan_vectors_to_clean = list(orphan_vectors) + list(orphan2_vectors)
            # 顺带确认 [5b] 的语义：`delete_file=false` 只保住 COS 原文件，资料行照样删
            # （真正"只解绑、不删"的只有删库路径；这一点此前容易被误解，故在此钉住）
            db.rollback()
            gone_rows = list(db.scalars(select(Resource).where(Resource.id == alpha_id)))
            check("delete_file=false 只保住原文件：资料行确实被删了（不是解绑）",
                  gone_rows == [], f"剩 {len(gone_rows)} 行")

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0
    finally:
        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id} kb={kb_id}")
        else:
            try:
                if kb_id:
                    resource_ids = list(
                        db.scalars(select(Resource.id).where(Resource.knowledge_base_id == kb_id))
                    )
                    leftover_vectors = list(
                        db.scalars(
                            select(KnowledgeChunk.vector_id).where(
                                KnowledgeChunk.resource_id.in_(resource_ids)
                            )
                        )
                    ) if resource_ids else []
                    if resource_ids:
                        db.execute(
                            sql_delete(KnowledgeChunk).where(
                                KnowledgeChunk.resource_id.in_(resource_ids)
                            )
                        )
                        db.execute(sql_delete(Resource).where(Resource.id.in_(resource_ids)))
                    db.execute(sql_delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
                    db.commit()
                    # 临时脚本造的向量点自己收尾，别给 Qdrant 留孤儿
                    if leftover_vectors:
                        delete_vectors_by_ids(leftover_vectors)
                # [6] 孤儿段落造过的向量点：孤儿资料可能已被 API 删掉，
                # 但 Qdrant 点只认 vector_id，所以这里按 id 兜底清一次
                if orphan_vectors_to_clean:
                    delete_vectors_by_ids(orphan_vectors_to_clean)
                if user_id:
                    # [6] 造的"孤儿来源库"若在造完孤儿后就被删掉则不留痕；
                    # 中途失败时可能还有残留，这里按 user_id 兜底
                    db.execute(sql_delete(KnowledgeBase).where(KnowledgeBase.user_id == user_id))
                    db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"已清理临时数据：user={user_id} kb={kb_id}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"清理失败（可手工删）：{exc}")
            if probe_path:
                try:
                    CosStorageService.delete_object_by_storage_path(cos_client, probe_path)
                    print("兜底删除了临时 COS 对象")
                except Exception:  # noqa: BLE001
                    pass
            try:
                CosStorageService.delete_object_by_storage_path(
                    cos_client, f"cos://{app_settings.cos_bucket}/verify/keep-{suffix}.txt"
                )
            except Exception:  # noqa: BLE001
                pass
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

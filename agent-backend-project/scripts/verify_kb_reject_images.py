"""验收：知识库**只收文档**——图片/音频（知识卡 png、语音、附件）永远不进库。

为什么要有这一套（用户 2026-10-20 反馈"知识卡片 png 也会被归于知识库"）：
`resources.knowledge_base_id` 为空的**不止**"删库留下的孤儿资料"，聊天里的每一张图、
每一个知识卡 png、每个头像/语音天生就是 NULL。于是「未归档文件」列表把它们全列了出来，
并贴着"已删除的知识库留下的"这个标签——用户在知识库页看到 `knowledge_card_xxx.png`，
但那个库根本没删过。要修的不是那条列表的文案，而是**口径**：

1. 带库上传时只放文档进库（图片/音频 400，挡在入口，不静默忽略）；
2. 系统生成物（`UploadService.save_bytes`：知识卡出图）即使被传了库也强制不带库；
3. 未归档列表只列文档型资料（`resource_type = 0`）；
4. 「归档到…」（`PATCH .../unbound-resources/{id}`）是唯一能把**已有资源**塞进库的入口，
   这里也必须挡住附件；
5. 回归：库内删除文档 / 未归档删除仍然可用（本轮同时修了前端"删除文档静默 no-op"，
   后端这半边用本脚本钉住）。

为什么把 `KbPipelineService.process_resource` 打成桩：文档带库上传会触发后台的
"向量入库 + 考纲提炼"（embedding + LLM，真花钱真花时间）。本脚本验的是**归属与过滤**，
不是入库质量，所以后台任务直接空转（`verify_difficulty_card.py` 里 patch `save_bytes`
是同一手法）。图片那条根本不会进后台任务（`upload_routes` 只对文档加任务）。

⚠️ 临时账号与知识库一律**走 HTTP 建**（不用脚本的 session 先插一行）：脚本 session 里
未提交的 INSERT 会一直握着唯一索引上的锁，紧接着 HTTP 的注册请求插同名用户就撞
`1205 Lock wait timeout`（实测卡满 50s 才报，看起来像"数据库挂了"）。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_kb_reject_images.py
    python scripts/verify_kb_reject_images.py --keep   # 不清理，方便手工看数据
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sqlalchemy import delete as sql_delete
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from app.db.database import SessionLocal
from app.db.models import KnowledgeBase, KnowledgeChunk, Resource, User
from app.main import app
from app.services.kb_pipeline_service import KbPipelineService
from app.services.upload_service_cos import (
    STORAGE_SCENE_EXTRACT_ONLY,
    UploadService,
)

TEMP_PASSWORD = "verify_pass_123"

# 一张最小的真 PNG（1x1）：内容不重要，后缀与 MIME 才决定资源类型
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库只收文档（图片/音频被拦）验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    args = parser.parse_args()

    db = SessionLocal()
    passed = failed = 0
    suffix = str(int(time.time()))[-8:]
    username = f"vkrej{suffix}"  # users.username 只有 20 字符
    user_id = kb_id = None
    stubbed = False
    real_process = KbPipelineService.process_resource

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {name} {extra}")
        else:
            failed += 1
            print(f"  FAIL {name} {extra}")

    try:
        # 后台任务打成桩：本脚本只验归属与过滤，不跑 embedding / LLM
        KbPipelineService.process_resource = staticmethod(  # type: ignore[assignment]
            lambda *a, **k: {"status": "stubbed"}
        )
        stubbed = True

        with TestClient(app) as client:
            # ---------------------------------------------------------- [1] 鉴权
            print("\n[1] 未带令牌一律拦住")
            resp = client.post(
                "/upload/file",
                files={"file": ("a.png", TINY_PNG, "image/png")},
                data={"knowledge_base_id": "1"},
            )
            check("POST /upload/file 未授权被拦", resp.status_code in (401, 403),
                  f"-> {resp.status_code}")

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

            # 临时库也走 HTTP 建（理由见模块 docstring 的锁等待警告）
            resp = client.post(
                "/knowledge-bases",
                headers=headers,
                json={"name": f"临时只收文档验收库-{suffix}"},
            )
            if resp.status_code != 200:
                raise SystemExit(f"建库失败: {resp.status_code} {resp.text[:300]}")
            kb_id = resp.json()["data"]["id"]
            db.rollback()
            user_id = db.scalar(select(User.id).where(User.username == username))
            print(f"      临时用户 id={user_id}（{username}） 知识库 id={kb_id}")

            # ---------------------------------------------------------- [2] 入口闸：带库上传只收文档
            print("\n[2] 带库上传：图片/音频必须 400，文档放行")
            for name, body, mime, label in (
                ("知识卡.png", TINY_PNG, "image/png", "图片(png)"),
                ("语音.mp3", b"ID3\x03\x00\x00\x00", "audio/mpeg", "音频(mp3)"),
                ("表情.gif", b"GIF89a\x01\x00\x01\x00\x00\xff\x00,", "image/gif", "图片(gif)"),
            ):
                resp = client.post(
                    "/upload/file",
                    files={"file": (name, body, mime)},
                    data={"knowledge_base_id": str(kb_id)},
                    headers=headers,
                )
                check(f"{label} 带库上传被拦（400）", resp.status_code == 400,
                      f"-> {resp.status_code} {resp.text[:120]}")
                if resp.status_code == 400:
                    check(f"{label} 的文案说清「只收文档」",
                          "只收文档" in resp.text and "不能归档" in resp.text,
                          resp.text[:160])

            doc_name = f"临时资料-{suffix}.txt"
            resp = client.post(
                "/upload/file",
                files={"file": (doc_name, "知识库资料正文。".encode(), "text/plain")},
                data={"knowledge_base_id": str(kb_id)},
                headers=headers,
            )
            check("文档(txt) 带库上传放行（201）", resp.status_code == 201,
                  f"-> {resp.status_code} {resp.text[:120]}")
            doc_id = resp.json()["data"]["resource_id"] if resp.status_code == 201 else None
            if doc_id:
                db.rollback()
                row = db.get(Resource, doc_id)
                check("文档真的归档进了目标库",
                      row is not None and row.knowledge_base_id == kb_id,
                      f"kb={getattr(row, 'knowledge_base_id', None)}")
                db.rollback()
                check("被拦的图片/音频没有留下资源行",
                      db.scalar(
                          select(Resource.id).where(
                              Resource.user_id == user_id, Resource.resource_type != 0
                          )
                      ) is None)

            print("\n[2b] 不带库上传图片仍然允许（别把「上传图片」整体砍掉）")
            resp = client.post(
                "/upload/file",
                files={"file": (f"knowledge_card_{suffix}.png", TINY_PNG, "image/png")},
                headers=headers,
            )
            check("图片不带库上传 201", resp.status_code == 201, f"-> {resp.status_code}")
            png_id = resp.json()["data"]["resource_id"] if resp.status_code == 201 else None
            if png_id:
                check("资源类型是图片(1)", resp.json()["data"]["resource_type"] == 1,
                      str(resp.json()["data"]["resource_type"]))
                db.rollback()
                row = db.get(Resource, png_id)
                check("图片天生不属于任何库", row is not None and row.knowledge_base_id is None,
                      f"kb={getattr(row, 'knowledge_base_id', None)}")

            # ---------------------------------------------------------- [3] 未归档列表只列文档
            print("\n[3] 未归档列表：png 不该被当成「孤儿文档」")
            resp = client.get("/knowledge-bases/unbound-resources", headers=headers)
            check("列表 200", resp.status_code == 200, f"-> {resp.status_code}")
            items = resp.json()["data"]["items"]
            names = [row["file_name"] for row in items]
            check("png 不出现在未归档列表里", png_id not in [row["id"] for row in items],
                  str(names))
            check("列表里只有文档型资料", all(not name.lower().endswith(".png") for name in names),
                  str(names))

            # ---------------------------------------------------------- [4] 归档入口同样挡住附件
            print("\n[4] 「归档到…」是唯一能把已有资源塞进库的入口，必须同样挡住")
            if png_id:
                resp = client.patch(
                    f"/knowledge-bases/unbound-resources/{png_id}",
                    headers=headers,
                    json={"knowledge_base_id": kb_id},
                )
                check("图片归档回库被拦（400）", resp.status_code == 400,
                      f"-> {resp.status_code} {resp.text[:120]}")
                db.rollback()
                row = db.get(Resource, png_id)
                check("被拦后图片仍然不属于任何库",
                      row is not None and row.knowledge_base_id is None,
                      f"kb={getattr(row, 'knowledge_base_id', None)}")

            # ---------------------------------------------------------- [5] 系统生成物强制不带库
            print("\n[5] save_bytes（知识卡出图）即使传了库也不进库")
            db.rollback()
            owner = db.get(User, user_id)
            generated, _ = UploadService.save_bytes(
                db=db,
                user=owner,
                file_bytes=TINY_PNG,
                file_name=f"knowledge_card_gen_{suffix}.png",
                content_type="image/png",
                storage_scene=STORAGE_SCENE_EXTRACT_ONLY,
                knowledge_base_id=kb_id,
            )
            generated_id = generated.id
            check("生成物落库成功（没有因为带库而报错）", bool(generated_id))
            check("生成物的 knowledge_base_id 被强制置空",
                  generated.knowledge_base_id is None, str(generated.knowledge_base_id))

            # ---------------------------------------------------------- [6] 回归：删除文档仍然可用
            print("\n[6] 回归：库内删除文档 / 未归档删除")
            resp = client.get(f"/knowledge-bases/{kb_id}/resources", headers=headers)
            check("库内文档列表只列那份文档",
                  [row["id"] for row in resp.json()["data"]["items"]] == [doc_id],
                  str([row["file_name"] for row in resp.json()["data"]["items"]]))
            if doc_id:
                resp = client.delete(
                    f"/knowledge-bases/{kb_id}/resources/{doc_id}", headers=headers
                )
                check("库内删除 200", resp.status_code == 200, f"-> {resp.status_code}")
                check("删除响应带切片/向量/原文件三项",
                      {"deleted_chunk_count", "deleted_vector_count", "deleted_file"} <= set(
                          resp.json()["data"]
                      ), str(resp.json()["data"]))
                db.rollback()
                check("资料行真的没了", db.get(Resource, doc_id) is None)
            if png_id:
                # 附件虽然不再列在未归档页，但直接按 id 删仍然要能删（否则历史脏数据没法清）
                resp = client.delete(
                    f"/knowledge-bases/unbound-resources/{png_id}", headers=headers
                )
                check("未归档删除仍能删掉图片（清理历史脏数据用）", resp.status_code == 200,
                      f"-> {resp.status_code}")
                db.rollback()
                check("图片资料行真的没了", db.get(Resource, png_id) is None)

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0
    finally:
        if stubbed:
            KbPipelineService.process_resource = real_process  # type: ignore[assignment]
        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id} kb={kb_id}")
        else:
            try:
                db.rollback()
                if user_id:
                    resource_ids = list(
                        db.scalars(select(Resource.id).where(Resource.user_id == user_id))
                    )
                    if resource_ids:
                        db.execute(sql_delete(KnowledgeChunk).where(
                            KnowledgeChunk.resource_id.in_(resource_ids)))
                        db.execute(sql_delete(Resource).where(Resource.id.in_(resource_ids)))
                    # 父表最后删（本项目没有 ORM relationship，顺序必须显式）
                    db.execute(sql_delete(KnowledgeBase).where(KnowledgeBase.user_id == user_id))
                    db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"已清理临时数据：user={user_id} kb={kb_id}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"清理失败（可手工删）：{exc}")
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

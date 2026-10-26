"""验收：知识库文档接口的 HTTP 层（走 FastAPI 路由 + 真实鉴权，不起 uvicorn）。

要点：直接 `TestClient(app)` 打路由，覆盖三件事——
1. 未带令牌一律 401；
2. 列表 / 详情 / 切片三个只读接口的真实响应体；
3. **原文件转发**真的把 PDF 流出来，且响应头是 `inline`（内联预览的关键）。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_kb_documents_http.py --username 你的账号 --password 你的密码
    python scripts/verify_kb_documents_http.py            # 自动注册临时账号（看不到你已有的库）
    python scripts/verify_kb_documents_http.py --keep-user   # 保留临时账号，方便手工看数据
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sqlalchemy import delete as sql_delete

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from app.db.database import SessionLocal
from app.db.models import User
from app.main import app

# 本次自注册的临时账号名（模块级：`__main__` 的 finally 要读它来清理，不能是 main() 的局部变量）
TEMP_USERNAME: str | None = None
# --keep-user（同样是模块级，`__main__` 里读不到 main() 的 args）
KEEP_TEMP_USER = False


def _token(client: TestClient, username: str, password: str) -> str:
    """注册（若还没注册）并登录，返回 access_token。

    `username` 由调用方传入而不是这里现算：清理时要按同一个名字删账号，
    两处各算一次 `int(time.time())` 会因秒界错位而删不掉。
    """
    resp = client.post("/auth/register", json={"username": username, "password": password})
    print(f"[1] 注册临时账号 {username}: {resp.status_code}")
    resp = client.post("/auth/login", json={"username": username, "password": password})
    if resp.status_code != 200:
        raise SystemExit(f"登录失败: {resp.status_code} {resp.text[:300]}")
    print("[2] 登录成功")
    return resp.json()["data"]["access_token"]


def _cleanup_temp_user(username: str) -> None:
    """删掉本次自注册的临时账号。

    不清理的后果是实测过的：`users` 表会攒下一堆 `verify_kb_*` 空账号
    （没有知识库、没有资料，但会出现在任何按用户统计的地方）。只在"确实是我们注册的"
    时候调用——传了 `--username` 用已有账号时绝不删别人的数据。
    """
    db = SessionLocal()
    try:
        deleted = db.execute(sql_delete(User).where(User.username == username)).rowcount or 0
        db.commit()
        print(f"已清理临时账号 {username}（{deleted} 行）")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"临时账号清理失败（可手工删）：{exc}")
    finally:
        db.close()


def main() -> int:
    global TEMP_USERNAME, KEEP_TEMP_USER
    parser = argparse.ArgumentParser(description="知识库文档 HTTP 验收")
    parser.add_argument("--username", default=None, help="已有账号（不传则注册临时账号）")
    parser.add_argument("--password", default="verify_pass_123", help="账号密码")
    parser.add_argument("--kb-id", type=int, default=None, help="知识库ID（不传则用列表第一个）")
    parser.add_argument("--keep-user", action="store_true", help="不清理自注册的临时账号")
    args = parser.parse_args()

    passed = failed = 0
    KEEP_TEMP_USER = bool(args.keep_user)
    TEMP_USERNAME = None if args.username else f"verify_kb_{int(time.time())}"

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {name} {extra}")
        else:
            failed += 1
            print(f"  FAIL {name} {extra}")

    with TestClient(app) as client:
        print("\n[0] 未带令牌")
        for path in ("/knowledge-bases", "/knowledge-bases/1/resources"):
            resp = client.get(path)
            # HTTPBearer 对"完全没带头"返回 403，对"头无效"返回 401；都算拦住
            check(f"GET {path} 未授权被拦", resp.status_code in (401, 403), f"-> {resp.status_code}")

        token = _token(client, args.username or TEMP_USERNAME, args.password)
        headers = {"Authorization": f"Bearer {token}"}

        print("\n[1] 知识库列表")
        resp = client.get("/knowledge-bases", headers=headers)
        check("列表 200", resp.status_code == 200, f"-> {resp.status_code}")
        items = resp.json()["data"]["items"]
        if not items:
            print("      该账号没有知识库：接口连通性已验证，末尾几项跳过")
            print(f"\n结果：{passed} 通过 / {failed} 失败")
            return 1 if failed else 0
        kb_id = args.kb_id or items[0]["id"]
        # 列表不带考纲（每库 8000 字会让响应变大），详情才带 —— 前端靠这个约定取 quiz_core
        check("列表不带 quiz_core", all(row.get("quiz_core") is None for row in items))
        print(f"      使用知识库 id={kb_id} name={items[0]['name']}")

        print("\n[1b] 知识库详情带考纲")
        resp = client.get(f"/knowledge-bases/{kb_id}", headers=headers)
        check("详情 200", resp.status_code == 200, f"-> {resp.status_code}")
        kb_detail = resp.json()["data"]
        check("详情含 quiz_core 字段", "quiz_core" in kb_detail)
        if kb_detail.get("quiz_core"):
            sections = kb_detail["quiz_core"].get("sections") or []
            check("考纲有章节可跳转", bool(sections), f"{len(sections)} 章")
        else:
            print("      该库还没有考纲，跳过章节断言")

        print("\n[2] 库内文档列表")
        resp = client.get(f"/knowledge-bases/{kb_id}/resources", headers=headers)
        check("列表 200", resp.status_code == 200, f"-> {resp.status_code}")
        data = resp.json()["data"]
        check("带 total", data["total"] == len(data["items"]), f"total={data['total']}")
        if not data["items"]:
            print("      库里没有文档，后续跳过")
            print(f"\n结果：{passed} 通过 / {failed} 失败")
            return 1 if failed else 0
        resource = data["items"][0]
        print(f"      首份：{resource['file_name']}（{resource['chunk_count']} 片 / {resource['char_count']} 字）")
        check("列表项不含正文", "text" not in resource)

        print("\n[3] 文档详情")
        resp = client.get(f"/knowledge-bases/{kb_id}/resources/{resource['id']}", headers=headers)
        check("详情 200", resp.status_code == 200, f"-> {resp.status_code}")
        detail = resp.json()["data"]
        check("有正文", detail["text_chars"] > 0, f"{detail['text_chars']} 字 ({detail['text_source']})")

        print("\n[4] 原文件转发（内联预览的关键）")
        if not detail["can_preview"]:
            print("      该资料没有可预览的原文件，跳过")
        else:
            with client.stream(
                "GET", f"/knowledge-bases/{kb_id}/resources/{resource['id']}/file", headers=headers
            ) as resp:
                check("文件 200", resp.status_code == 200, f"-> {resp.status_code}")
                disposition = resp.headers.get("content-disposition", "")
                check("响应头是 inline", disposition.startswith("inline"), disposition[:70])
                check("Content-Type 已声明", bool(resp.headers.get("content-type")),
                      str(resp.headers.get("content-type")))
                body = b"".join(resp.iter_bytes())
                check("拿到了文件内容", len(body) > 1000, f"{len(body)} 字节")
                if resource["file_name"].lower().endswith(".pdf"):
                    check("内容是 PDF", body[:5] == b"%PDF-", repr(body[:8]))

        print("\n[5] 越权：换一个不存在的资料 id")
        resp = client.get(f"/knowledge-bases/{kb_id}/resources/999999", headers=headers)
        check("不存在应 404", resp.status_code == 404, f"-> {resp.status_code}")

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0


if __name__ == "__main__":
    # 自注册的临时账号用完就删（传了 --username 时 TEMP_USERNAME 为 None，绝不动别人的数据）
    try:
        raise SystemExit(main())
    finally:
        if TEMP_USERNAME and not KEEP_TEMP_USER:
            _cleanup_temp_user(TEMP_USERNAME)

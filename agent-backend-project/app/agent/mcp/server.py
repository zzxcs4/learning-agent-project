"""自建 MCP server（stdio）：把知识库检索与掌握度查询**暴露给外部 MCP 客户端**。

这是升级规划 §7.2「MCP：两侧各最小一个」的 **server 侧**：
Claude Desktop / MCP Inspector 这类外部 host 连上它，就能以"某个用户"的身份检索知识库、
查掌握度——即把本项目的原子能力变成**跨 agent 的标准接口**。

## 为什么工具实现直接复用 `app/agent/tools/*`

本项目的原子工具（`search_material` / `get_mastery` / `get_weak_points`）已经是
"不依赖调用方身份、只依赖注入上下文"的纯函数。MCP server 只是**换一个入口**去调它们：
同一个能力不会有两份实现，权限边界（`user_id` 由服务端注入、模型给不出）也保持一致。
外部客户端看到的工具名与内部 loop 的工具名一一对应，讲解时能直接对上。

## 身份从哪来（重要）

MCP 协议里没有"用户"概念，host 也不会替你传。所以本进程用环境变量 `MCP_USER_ID`
指定"以谁的身份查询"。不设时，**只在库里恰好只有一个用户**的情况下自动采用该用户
（本机演示环境正是这种），否则工具直接返回可操作的报错，而不是随便挑一个用户。

跑法：

    python -m app.agent.mcp.server                 # stdio（由 host 拉起）
    python -m app.agent.mcp.server --info          # 自查：打印身份与工具清单后退出

Claude Desktop 配置（示例）：

```json
{
  "mcpServers": {
    "learning-kb": {
      "command": "D:\\\\conda_envs\\\\zxlc_agent\\\\python.exe",
      "args": ["-m", "app.agent.mcp.server"],
      "cwd": "D:\\\\PythonProject\\\\harness-hermes-multiagent-code\\\\agent-backend-project",
      "env": {"MCP_USER_ID": "1"}
    }
  }
}
```

> ⚠️ stdio 传输下 **stdout 只能走 JSON-RPC**。本项目的 loguru 控制台 handler 写的是
> stderr（`app/core/logging_config.py`），所以日志不会污染协议流；新增任何 print 都要走 stderr。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.tools.knowledge import search_material as _search_material
from app.agent.tools.profile import get_mastery as _get_mastery
from app.agent.tools.profile import get_weak_points as _get_weak_points
from app.core.logging_config import main_logger
from app.db.database import SessionLocal
from app.db.models import User

SERVER_NAME = "learning-kb"
USER_ID_ENV = "MCP_USER_ID"

INSTRUCTIONS = """本 server 提供"个人学习资料库"的检索与画像查询能力：

- search_material：按关键词检索用户知识库里的原始资料片段（带来源文件名与相关度）；
- get_mastery：查某个知识库的掌握度分数（0–100）与练习场次；
- get_weak_points：查当前未改进的薄弱点（按出现次数降序）。

建议先 get_mastery / get_weak_points 了解用户水平，再用 search_material 取原文依据。
所有工具都是**只读**的：本 server 不接受任何写入或删除操作。
"""


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------
def resolve_user_id(db: Session) -> tuple[int, str]:
    """确定"以谁的身份查询"；返回 (user_id, 错误说明)，错误说明非空时 user_id 为 0。"""
    raw = (os.getenv(USER_ID_ENV) or "").strip()
    if raw:
        try:
            candidate = int(raw)
        except ValueError:
            return 0, f"环境变量 {USER_ID_ENV}={raw!r} 不是整数"
        if db.get(User, candidate) is None:
            return 0, f"环境变量 {USER_ID_ENV}={candidate} 指向的用户不存在"
        return candidate, ""

    ids = list(db.scalars(select(User.id).order_by(User.id).limit(2)))
    if len(ids) == 1:
        return int(ids[0]), ""
    if not ids:
        return 0, "数据库里没有任何用户，无法确定以谁的身份查询"
    return 0, (
        f"数据库里有多个用户，必须用环境变量 {USER_ID_ENV} 指定以谁的身份查询"
        f"（例如 {USER_ID_ENV}=1）"
    )


def _invoke_atomic(tool: Any, *, knowledge_base_id: int | None = None, **kwargs: Any) -> str:
    """开一个短事务会话、注入上下文、调用内部原子工具，返回它的 JSON 字符串。

    每次调用一个独立会话：MCP 的一次工具调用就是一次完整请求，
    不跨调用持有事务（也就不会把长事务带进 stdio 服务进程）。
    """
    with SessionLocal() as db:
        user_id, error = resolve_user_id(db)
        if error:
            return json.dumps({"error": error}, ensure_ascii=False)
        ctx = {
            "db": db,
            "user_id": user_id,
            "session_id": 0,
            "knowledge_base_id": knowledge_base_id,
            "run_id": None,
        }
        return tool.invoke({**kwargs, "ctx": ctx})


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def build_server() -> FastMCP:
    """构造 MCP server（工具即本项目的原子能力，只读）。"""
    server = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS)

    @server.tool(
        name="search_material",
        description="检索用户知识库里的资料原文，返回最相关的片段与来源文件名。",
    )
    def search_material(
        query: str,
        top_k: int = 3,
        knowledge_base_id: int | None = None,
    ) -> str:
        """按关键词/问句检索知识库原文。

        Args:
            query: 检索用的关键词或问句（用资料里可能出现的具体名词效果最好）。
            top_k: 返回片段数（1–10，默认 3）。
            knowledge_base_id: 只在某个知识库内检索；不传则在该用户全部资料里检索。
        """
        return _invoke_atomic(
            _search_material,
            knowledge_base_id=knowledge_base_id,
            query=query,
            top_k=top_k,
        )

    @server.tool(
        name="get_mastery",
        description="查询用户的知识库掌握度（0–100）与练习场次。",
    )
    def get_mastery(knowledge_base_id: int | None = None) -> str:
        """查掌握度。

        Args:
            knowledge_base_id: 知识库 ID；不传则返回该用户全部知识库的掌握度概览。
        """
        return _invoke_atomic(_get_mastery, knowledge_base_id=knowledge_base_id)

    @server.tool(
        name="get_weak_points",
        description="查询用户当前未改进的薄弱点，按出现次数降序。",
    )
    def get_weak_points(knowledge_base_id: int | None = None, limit: int = 5) -> str:
        """查薄弱点。

        Args:
            knowledge_base_id: 知识库 ID；不传则返回全部（含未归属任何库的）薄弱点。
            limit: 最多返回条数（1–20，默认 5）。
        """
        return _invoke_atomic(
            _get_weak_points,
            knowledge_base_id=knowledge_base_id,
            limit=limit,
        )

    return server


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _print_info() -> int:
    """自查：不进入 MCP 会话，打印身份与工具清单（排查"host 连上但没工具"时用）。

    输出走 **stderr**：这个模块永远不该往 stdout 写协议之外的东西，
    即使是自查模式也保持一致，免得哪天被误用到 stdio 会话里。
    """
    with SessionLocal() as db:
        user_id, error = resolve_user_id(db)
    payload: dict[str, Any] = {
        "server": SERVER_NAME,
        "user_id": user_id if not error else None,
        "identity_error": error,
        "user_id_env": os.getenv(USER_ID_ENV, ""),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    return 1 if error else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.agent.mcp.server", description="学习资料库 MCP server")
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help=f"以哪个用户身份查询（等价于设置 {USER_ID_ENV} 环境变量）",
    )
    parser.add_argument("--info", action="store_true", help="打印身份自查结果后退出（不启动服务）")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="传输方式；默认 stdio（由 host 以子进程方式拉起）",
    )
    args = parser.parse_args(argv)

    if args.user_id is not None:
        os.environ[USER_ID_ENV] = str(args.user_id)

    if args.info:
        return _print_info()

    main_logger.info("启动 MCP server {}（transport={}）", SERVER_NAME, args.transport)
    build_server().run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""探测：DeepWiki 远端 MCP（streamable-http）连通性与工具清单。

只做握手 + 列工具（**不调业务工具**），零 token、不写库：把"远端传输能不能连上"
从一堆配置问题里剥出来单独排查——连不上时这里会给出最直接的报错，而不是
"agent loop 里怎么没有 DeepWiki 工具"这种悬案。

用法（在 agent-backend-project 目录下）：

    python scripts/probe_deepwiki_mcp.py                 # 默认连官方 DeepWiki
    python scripts/probe_deepwiki_mcp.py --url <url>     # 探别的远端 MCP server
    python scripts/probe_deepwiki_mcp.py --timeout 60    # 网络慢时放大超时

退出码：0 = 握手成功并列到工具；1 = 连接失败（原因打印在最后）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.mcp import MCPClientManager, MCPServerSpec

DEFAULT_URL = "https://mcp.deepwiki.com/mcp"


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepWiki 远端 MCP 连通性探测")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"远端 MCP 端点（默认 {DEFAULT_URL}）")
    parser.add_argument("--timeout", type=float, default=30.0, help="连接与列工具超时（秒）")
    args = parser.parse_args()

    spec = MCPServerSpec(name="deepwiki", transport="streamable-http", url=args.url)
    manager = MCPClientManager([spec], connect_timeout=args.timeout, call_timeout=args.timeout)
    try:
        manager.start()
        row = manager.status()[0]
        print(f"endpoint : {args.url}")
        print(f"transport: {row.transport}")
        print(f"connected: {row.connected}")
        if not row.connected:
            print(f"error    : {row.error}")
            return 1
        print(f"tools({row.tool_count}):")
        for remote in manager.remote_tools():
            schema = json.dumps(remote.input_schema, ensure_ascii=False)
            print(f"  - {remote.name}: {remote.description or '（无说明）'}")
            print(f"    schema: {schema[:200]}")
        return 0
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())

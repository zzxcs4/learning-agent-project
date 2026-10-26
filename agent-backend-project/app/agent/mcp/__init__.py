"""MCP（Model Context Protocol）接入层（升级规划 §八 第 3 期）。

两个方向各最小一个：

| 方向 | 文件 | 作用 |
|---|---|---|
| **server** | `server.py` | 自建 stdio MCP server，把知识库检索 / 掌握度 / 薄弱点暴露给外部 host（Claude Desktop、MCP Inspector） |
| **client** | `client.py` | 连外部 MCP server，把它们的工具以 `mcp_<server>_<tool>` 注册进 supervisor 的 loop |
| 配置 | `spec.py` | 解析 `MCP_SERVERS`（JSON 数组） |

`MCP_ENABLED` 默认 **false**：MCP 会把"读用户资料"的能力交给外部进程、把外部工具交给模型执行，
必须由部署方显式开启。
"""

from __future__ import annotations

from app.agent.mcp.client import (
    MCP_TOOL_PREFIX,
    MCPClientManager,
    MCPRemoteTool,
    build_langchain_tools,
    describe_mcp_tools_for_prompt,
    get_mcp_manager,
    is_mcp_tool_name,
    mcp_langchain_tools,
    mcp_status_payload,
    mcp_tool_name,
    reset_mcp_manager,
    result_to_text,
    schema_to_pydantic,
)
from app.agent.mcp.spec import (
    TRANSPORT_STDIO,
    TRANSPORT_STREAMABLE_HTTP,
    MCPServerSpec,
    load_specs,
    parse_specs,
)

__all__ = [
    "MCP_TOOL_PREFIX",
    "TRANSPORT_STDIO",
    "TRANSPORT_STREAMABLE_HTTP",
    "MCPClientManager",
    "MCPRemoteTool",
    "MCPServerSpec",
    "build_langchain_tools",
    "describe_mcp_tools_for_prompt",
    "get_mcp_manager",
    "is_mcp_tool_name",
    "load_specs",
    "mcp_langchain_tools",
    "mcp_status_payload",
    "mcp_tool_name",
    "parse_specs",
    "reset_mcp_manager",
    "result_to_text",
    "schema_to_pydantic",
]

"""MCP server 的配置规格：把 `MCP_SERVERS` 这一段 JSON 解析成结构化对象。

**为什么配置是一段 JSON 而不是一组扁平环境变量**：一个 server 需要
"名字 + 传输方式 + 命令 + 参数 + 环境变量 + 是否启用"这一整组字段，
用扁平变量表达就得写 `MCP_SERVER_1_COMMAND` 这种靠下标猜的键；JSON 数组
可以整段复制粘贴给同事，也能直接在 Claude Desktop 的配置里对照着看。

写法（`MCP_SERVERS`，缺省为空 = 没有任何外部 server）：

```json
[
  {"name": "self-kb", "transport": "stdio", "command": "python", "args": ["-m", "app.agent.mcp.server"]},
  {"name": "fetch",   "transport": "stdio", "command": "mcp-server-fetch"},
  {"name": "remote",  "transport": "streamable-http", "url": "http://127.0.0.1:9000/mcp"}
]
```

解析刻意**不静默吞错**：坏掉的条目会被剔除，同时把原因回给调用方（由它记日志），
否则"我配了 MCP 却没有工具"会变成一个查不出来的悬案。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.core.settings import settings

# 传输方式：stdio（本地子进程，最常用）与 streamable-http（远端服务）
TRANSPORT_STDIO = "stdio"
TRANSPORT_STREAMABLE_HTTP = "streamable-http"
SUPPORTED_TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_STREAMABLE_HTTP)


@dataclass(frozen=True)
class MCPServerSpec:
    """一个要连接的 MCP server。字段与 MCP 客户端配置的习惯写法保持一致。"""

    name: str
    transport: str = TRANSPORT_STDIO
    # stdio 传输
    command: str = ""
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    # streamable-http 传输
    url: str = ""
    enabled: bool = True

    def describe(self) -> str:
        """一行可读描述（日志与系统提示词里都用它）。"""
        if self.transport == TRANSPORT_STREAMABLE_HTTP:
            return f"{self.name}({self.transport} {self.url})"
        return f"{self.name}({self.transport} {self.command} {' '.join(self.args)}".rstrip() + ")"

    def missing_field(self) -> str:
        """返回"这条配置缺什么"；完整时返回空串（供解析期校验）。"""
        if not self.name:
            return "缺少 name"
        if self.transport == TRANSPORT_STDIO and not self.command:
            return "stdio 传输必须给 command"
        if self.transport == TRANSPORT_STREAMABLE_HTTP and not self.url:
            return "streamable-http 传输必须给 url"
        return ""


def _as_str_tuple(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw)
    return ()


def _as_env_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def parse_specs(raw: str) -> tuple[list[MCPServerSpec], list[str]]:
    """解析 `MCP_SERVERS`；返回（可用的规格列表, 问题清单）。

    `raw` 为空 → 空列表且无问题（"没配"不是错误）。
    """
    text = (raw or "").strip()
    if not text:
        return [], []

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], [f"MCP_SERVERS 不是合法 JSON：{exc}"]

    if not isinstance(payload, list):
        return [], ["MCP_SERVERS 必须是 JSON 数组（每个元素是一个 server 配置）"]

    specs: list[MCPServerSpec] = []
    problems: list[str] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            problems.append(f"第 {index + 1} 项不是对象，已跳过")
            continue
        transport = str(item.get("transport") or TRANSPORT_STDIO).strip().lower()
        if transport not in SUPPORTED_TRANSPORTS:
            problems.append(
                f"第 {index + 1} 项的 transport={transport!r} 不支持"
                f"（只支持 {'/'.join(SUPPORTED_TRANSPORTS)}），已跳过"
            )
            continue
        spec = MCPServerSpec(
            name=str(item.get("name") or "").strip(),
            transport=transport,
            command=str(item.get("command") or "").strip(),
            args=_as_str_tuple(item.get("args")),
            cwd=(str(item["cwd"]).strip() or None) if item.get("cwd") else None,
            env=_as_env_map(item.get("env")),
            url=str(item.get("url") or "").strip(),
            enabled=bool(item.get("enabled", True)),
        )
        missing = spec.missing_field()
        if missing:
            problems.append(f"第 {index + 1} 项（{spec.name or '未命名'}）{missing}，已跳过")
            continue
        specs.append(spec)

    duplicated = _duplicated_names(specs)
    if duplicated:
        problems.append(f"server 名字重复：{', '.join(sorted(duplicated))}（只保留第一个）")
        specs = _drop_duplicates(specs)

    return specs, problems


def _duplicated_names(specs: list[MCPServerSpec]) -> set[str]:
    seen: set[str] = set()
    duplicated: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            duplicated.add(spec.name)
        seen.add(spec.name)
    return duplicated


def _drop_duplicates(specs: list[MCPServerSpec]) -> list[MCPServerSpec]:
    seen: set[str] = set()
    unique: list[MCPServerSpec] = []
    for spec in specs:
        if spec.name in seen:
            continue
        seen.add(spec.name)
        unique.append(spec)
    return unique


def load_specs() -> tuple[list[MCPServerSpec], list[str]]:
    """按当前配置返回"要连接的 server"（已过滤 `enabled=false`）。"""
    specs, problems = parse_specs(settings.mcp_servers_json)
    return [spec for spec in specs if spec.enabled], problems

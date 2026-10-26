"""验收：第 3 期 MCP（自建 stdio server + 外部 server 客户端 + 接进 agent loop）。

**全程零 token**；除第 9 段外不联网（第 9 段离线时自动跳过）。分层验证，越往下越"真"：

| 段 | 验的是什么 | 手段 |
|---|---|---|
| [1] | `MCP_SERVERS` 配置解析 | 纯函数，含各种坏配置 |
| [2] | 工具名/schema/结果收敛 | 纯函数（`schema_to_pydantic` / `result_to_text` / 包装成 LangChain 工具） |
| [3] | **自建 server 的协议往返** | `mcp.shared.memory` 内存流 + 真 MySQL（不启动子进程） |
| [4] | 客户端 manager（连接失败/超时/未知工具） | 内存流 + 一个故意失败的 server + 一个故意很慢的工具 |
| [5] | **接进 loop**（MCP 工具出现在 trace 里、不被误当 worker） | 假模型 + 真 server 代码 + 真 MySQL |
| [6] | **真 stdio 子进程往返**（外部 host 的连法） | 真起 `python -m app.agent.mcp.server`，真 MySQL，不调模型 |
| [7] | 第三方 fetch MCP 握手 | 真起 `mcp-server-fetch`，只列工具**不抓网页** |
| [8] | 官方 time MCP（日期/时区） | 真起 `python -m mcp_server_time`，stdio 真子进程 + 真实调用 |
| [9] | DeepWiki 远端 MCP（streamable-http） | **需联网**；只握手 + 列工具，连不上自动跳过 |

第 6 段是"外部 MCP 客户端能连上并拿到数据"这条验收口径的离线版：它走的是真实的
stdio + JSON-RPC 握手，只是把"客户端"换成了本仓的 client，并且不产生任何模型调用。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_mcp.py
    python scripts/verify_mcp.py --keep          # 保留临时数据
    python scripts/verify_mcp.py --skip-stdio    # 跳过第 6/7/8 段（真子进程，排查沙箱限制时用）
    python scripts/verify_mcp.py --skip-net      # 跳过第 9 段（DeepWiki 远端，离线时用）
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 第 6/7 段会起子进程，子进程同样要 `import app`（写日志）。受限权限下项目根/logs
# 不可写会让 loguru 初始化直接失败（交接文档坑 #40），所以这里显式把日志目录指到可写处；
# 只影响子进程继承的环境变量，父进程的日志配置在 import 时已经定型、不受影响。
if not os.environ.get("LOG_DIR"):
    os.environ["LOG_DIR"] = str(Path(os.environ.get("TEMP") or "/tmp") / "dsh-verify-logs")

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage, ToolMessage
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from app.agent import learning_partner as lp
from app.agent.learning_partner import LearningPartnerService
from app.agent.mcp import (
    MCPClientManager,
    MCPServerSpec,
    build_langchain_tools,
    describe_mcp_tools_for_prompt,
    is_mcp_tool_name,
    mcp_langchain_tools,
    mcp_status_payload,
    mcp_tool_name,
    parse_specs,
    reset_mcp_manager,
    result_to_text,
    schema_to_pydantic,
)
from app.agent.mcp.client import MAX_TOOL_NAME_CHARS
from app.agent.mcp.server import USER_ID_ENV, build_server, resolve_user_id
import app.agent.mcp.spec as spec_module
from app.core.settings import settings
from app.db.database import SessionLocal
from app.db.models import (
    ChatMessage,
    KnowledgeBase,
    Session as SessionModel,
    User,
    UserKnowledgeMastery,
    UserProfile,
)
from app.security import hash_password

TEMP_PASSWORD = "verify_pass_123"
# 临时掌握度分数刻意取一个"不可能是默认值"的数，用来证明数据真的从库里流到了 MCP 客户端
TEMP_MASTERY_SCORE = 42.5

passed = 0
failed = 0
skipped = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def skip(name: str, why: str) -> None:
    global skipped
    skipped += 1
    print(f"  SKIP {name} —— {why}")


def _call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class FakeToolCallingModel:
    """按预设脚本"流"出回答的假模型（与 `verify_agent_loop.py` 同款思路）。"""

    def __init__(self, script: list[tuple[str, list[dict[str, Any]]]]):
        self.script = list(script)
        self.calls: list[list[Any]] = []
        self.bound_tools: list[str] = []

    def bind_tools(self, tools: list[Any]) -> "FakeToolCallingModel":
        self.bound_tools = [getattr(tool, "name", str(tool)) for tool in tools]
        return self

    def stream(self, messages: list[Any], config: Any = None) -> Any:
        # config：loop 以 `config={"callbacks": [用量收集器]}` 调用（第 3 期可观测），
        # 假模型不消费它，但形参必须接住，否则 TypeError。
        self.calls.append(list(messages))
        index = min(len(self.calls) - 1, len(self.script) - 1)
        text, tool_calls = self.script[index]
        if text:
            yield AIMessageChunk(content=text)
        if tool_calls:
            yield AIMessageChunk(content="", tool_calls=list(tool_calls))

    def invoke(self, messages: list[Any]) -> AIMessage:  # pragma: no cover - 本脚本不用
        self.calls.append(list(messages))
        return AIMessage(content="（假模型）")


class FakeWorkerGateway:
    """假 worker 网关：只记录委派。MCP 工具**不该**走到这里。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def delegate(
        self, worker_name: str, payload: dict[str, Any], *, user_id: int, session_id: int
    ) -> dict[str, Any]:
        self.calls.append((worker_name, payload))
        return {"worker": worker_name, "ok": True, "status": "dispatched", "data": {}}


def drive(service: LearningPartnerService, **kwargs: Any) -> tuple[lp.LoopResult, list[dict]]:
    generator = service.run_events(**kwargs)
    events: list[dict] = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stop:
            return stop.value or lp.LoopResult(), events


# ---------------------------------------------------------------------------
# [1] 配置解析
# ---------------------------------------------------------------------------
def verify_config() -> None:
    print("\n[1] MCP_SERVERS 配置解析（配错了要说清原因，不能静默没有工具）")

    specs, problems = parse_specs("")
    check("空配置 = 没有 server、也不算错", specs == [] and problems == [], str(problems))

    raw = json.dumps(
        [
            {
                "name": "self-kb",
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "app.agent.mcp.server"],
                "cwd": ".",
                "env": {"MCP_USER_ID": 1},
            },
            {"name": "remote", "transport": "streamable-http", "url": "http://127.0.0.1:9000/mcp"},
            {"name": "off", "transport": "stdio", "command": "x", "enabled": False},
        ]
    )
    specs, problems = parse_specs(raw)
    check("合法配置全部解析出来", len(specs) == 3 and problems == [], str(problems))
    check(
        "args/env 归一成元组与字符串映射",
        specs[0].args == ("-m", "app.agent.mcp.server") and specs[0].env == {"MCP_USER_ID": "1"},
    )
    check("streamable-http 只认 url", specs[1].url == "http://127.0.0.1:9000/mcp")

    bad, problems = parse_specs("{不是 json")
    check("非法 JSON：无规格 + 明确报错", bad == [] and len(problems) == 1, str(problems))

    bad, problems = parse_specs('{"name": "x"}')
    check("不是数组：报错而不是猜", bad == [] and "数组" in problems[0], str(problems))

    bad, problems = parse_specs(
        json.dumps(
            [
                {"name": "a", "transport": "carrier-pigeon", "command": "x"},
                {"name": "b", "transport": "stdio"},
                {"name": "c", "transport": "streamable-http"},
                {"name": "d", "transport": "stdio", "command": "ok"},
                {"name": "d", "transport": "stdio", "command": "dup"},
            ]
        )
    )
    check("不支持的 transport 被剔除并报原因", len(bad) == 1 and bad[0].name == "d", str([s.name for s in bad]))
    check("问题清单逐条给出（3 条 + 重名 1 条）", len(problems) == 4, str(problems))
    check("重名只保留第一个", [s.command for s in bad] == ["ok"], str([s.command for s in bad]))

    # enabled=false 由 load_specs 过滤（走真实 settings 路径）
    original = settings.mcp_servers_json
    try:
        object.__setattr__(settings, "mcp_servers_json", raw)
        loaded, _ = spec_module.load_specs()
        check("load_specs 过滤 enabled=false", [s.name for s in loaded] == ["self-kb", "remote"])
    finally:
        object.__setattr__(settings, "mcp_servers_json", original)

    # MCP 关闭时：零副作用（不建线程、不连接、工具表为空）
    original_enabled = settings.mcp_enabled
    try:
        object.__setattr__(settings, "mcp_enabled", False)
        reset_mcp_manager()
        check("MCP 关闭 → 不给 loop 挂任何工具", mcp_langchain_tools() == {})
        status = mcp_status_payload()
        check("MCP 关闭 → 状态里写明原因", status["enabled"] is False and bool(status["reason"]))
        check("MCP 关闭 → 提示词不加 MCP 段落", describe_mcp_tools_for_prompt({}) == "")
    finally:
        object.__setattr__(settings, "mcp_enabled", original_enabled)
        reset_mcp_manager()


# ---------------------------------------------------------------------------
# [2] 工具名 / schema / 结果收敛
# ---------------------------------------------------------------------------
def verify_wrapping() -> None:
    print("\n[2] 工具名、参数 schema 与结果收敛（纯函数）")

    check(
        "工具名带 mcp_<server>_<tool> 前缀",
        mcp_tool_name("self-kb", "search_material") == "mcp_self-kb_search_material",
        mcp_tool_name("self-kb", "search_material"),
    )
    check("名字里的非法字符被收敛", mcp_tool_name("a.b c", "t/1") == "mcp_a_b_c_t_1")
    long_name = mcp_tool_name("s" * 80, "t" * 80)
    check(
        f"超长名字截断到 {MAX_TOOL_NAME_CHARS} 字符内",
        len(long_name) <= MAX_TOOL_NAME_CHARS,
        str(len(long_name)),
    )
    check(
        "前缀判定：内置工具不算 MCP",
        is_mcp_tool_name("mcp_x_y") and not is_mcp_tool_name("search_material"),
    )

    # schema → pydantic
    model = schema_to_pydantic(
        "t_args",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词"},
                "top_k": {"type": "integer"},
                "mode": {"type": "string", "enum": ["fast", "deep"]},
            },
            "required": ["query"],
        },
    )
    fields = model.model_fields
    check(
        "schema 字段名与类型映射正确",
        set(fields) == {"query", "top_k", "mode"}
        and fields["query"].annotation is str
        and fields["top_k"].annotation is int,
        str({k: str(v.annotation) for k, v in fields.items()}),
    )
    check("required 与 optional 区分正确", fields["query"].is_required() and not fields["top_k"].is_required())
    check("enum 变成受限取值", "fast" in str(fields["mode"].annotation), str(fields["mode"].annotation))
    check("参数说明被保留", fields["query"].description == "检索词")

    loose = schema_to_pydantic("loose", {"type": "object", "properties": {"a-b": {"type": "string"}}})
    check(
        "参数名不是合法标识符 → 退化为原样透传",
        loose.model_config.get("extra") == "allow" and not loose.model_fields,
    )
    check(
        "schema 没有 properties → 原样透传",
        schema_to_pydantic("empty", {"type": "object"}).model_config.get("extra") == "allow",
    )

    # 结果收敛
    from mcp import types

    text_result = types.CallToolResult(
        content=[types.TextContent(type="text", text='{"count": 2}')], isError=False
    )
    check("文本结果原样给出", result_to_text(text_result) == '{"count": 2}')

    error_result = types.CallToolResult(
        content=[types.TextContent(type="text", text="工具内部炸了")], isError=True
    )
    check(
        "isError → 收敛成 error JSON（不是把错误当数据）",
        json.loads(result_to_text(error_result))["error"] == "工具内部炸了",
    )

    structured = types.CallToolResult(
        content=[types.TextContent(type="text", text="摘要")],
        structuredContent={"score": 42.5},
        isError=False,
    )
    combined = json.loads(result_to_text(structured))
    check("结构化结果与文本都给出来", combined["text"] == "摘要" and combined["structured"]["score"] == 42.5)

    # FastMCP 会给"只返回 str 的工具"套一层 {"result": <同一个字符串>}，这是协议噪声
    wrapped = types.CallToolResult(
        content=[types.TextContent(type="text", text='{"items": []}')],
        structuredContent={"result": '{"items": []}'},
        isError=False,
    )
    check(
        "剥掉 FastMCP 的 {\"result\": ...} 包装（还模型一段干净 JSON）",
        result_to_text(wrapped) == '{"items": []}',
        result_to_text(wrapped),
    )

    different = types.CallToolResult(
        content=[types.TextContent(type="text", text="摘要")],
        structuredContent={"result": "别的字符串"},
        isError=False,
    )
    check(
        "包装里装的不是同一段文本时不做剥离（不丢真实结构化数据）",
        "structured" in json.loads(result_to_text(different)),
    )

    image_only = types.CallToolResult(
        content=[types.ImageContent(type="image", data="aGk=", mimeType="image/png")], isError=False
    )
    check("只有非文本内容 → 明确说明未纳入上下文", "非文本内容" in result_to_text(image_only))
    check("空结果也不返回空串", "没有返回任何内容" in result_to_text(types.CallToolResult(content=[])))

    # 包装成 LangChain 工具
    recorded: list[tuple[str, str, dict[str, Any]]] = []

    def fake_call(server: str, tool: str, args: dict[str, Any]) -> str:
        recorded.append((server, tool, args))
        return '{"ok": true}'

    from app.agent.mcp.client import MCPRemoteTool

    tools = build_langchain_tools(
        [
            MCPRemoteTool(
                server="self-kb",
                name="search_material",
                description="检索资料",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            )
        ],
        fake_call,
    )
    check("包装出的工具名与远端一一对应", list(tools) == ["mcp_self-kb_search_material"], str(list(tools)))
    tool = tools["mcp_self-kb_search_material"]
    check("描述里标出它来自哪个 MCP server", "[MCP:self-kb]" in tool.description, tool.description)
    check("模型可见的参数只有远端的业务参数", set(tool.args) == {"query"}, str(tool.args))
    check("调用被转发到 (server, tool, args)", tool.invoke({"query": "索引"}) == '{"ok": true}')
    check(
        "转发时 server/tool/参数都没串味",
        recorded == [("self-kb", "search_material", {"query": "索引"})],
        str(recorded),
    )


# ---------------------------------------------------------------------------
# [3] 自建 server 的协议往返（内存流 + 真 MySQL）
# ---------------------------------------------------------------------------
async def _server_roundtrip(kb_id: int, user_id: int) -> None:
    server = build_server()
    async with create_connected_server_and_client_session(server) as session:
        listed = await session.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        check(
            "server 暴露 3 个只读工具",
            names == ["get_mastery", "get_weak_points", "search_material"],
            str(names),
        )
        by_name = {tool.name: tool for tool in listed.tools}
        props = set((by_name["search_material"].inputSchema or {}).get("properties", {}))
        check(
            "search_material 的参数是 query/top_k/knowledge_base_id",
            props == {"query", "top_k", "knowledge_base_id"},
            str(sorted(props)),
        )
        check(
            "get_mastery 只暴露 knowledge_base_id（没有 user_id 这种可越权的参数）",
            set((by_name["get_mastery"].inputSchema or {}).get("properties", {})) == {"knowledge_base_id"},
            str(sorted((by_name["get_mastery"].inputSchema or {}).get("properties", {}))),
        )

        result = await session.call_tool("get_mastery", {"knowledge_base_id": kb_id})
        payload = json.loads(result_to_text(result))
        scores = [item["mastery_score"] for item in payload.get("items", [])]
        check(
            "跨 MCP 协议拿到本库真实掌握度",
            scores == [TEMP_MASTERY_SCORE],
            str(payload)[:200],
        )

        weak = await session.call_tool("get_weak_points", {})
        check(
            "get_weak_points 也是可用工具（返回 JSON 而不是报错）",
            isinstance(json.loads(result_to_text(weak)), dict),
        )


def verify_server_protocol(user_id: int, kb_id: int) -> None:
    print("\n[3] 自建 server 的协议往返（内存流 + 真 MySQL，零 token）")
    original = os.environ.get(USER_ID_ENV)
    os.environ[USER_ID_ENV] = str(user_id)
    try:
        asyncio.run(_server_roundtrip(kb_id, user_id))
    finally:
        if original is None:
            os.environ.pop(USER_ID_ENV, None)
        else:
            os.environ[USER_ID_ENV] = original


def verify_identity(db: Any, user_id: int) -> None:
    """身份判定：设了就照设的来；没设且库里多用户时必须报错而不是猜一个。"""
    print("\n[3b] 身份判定（MCP 协议里没有用户概念，必须显式确定）")
    original = os.environ.get(USER_ID_ENV)
    try:
        os.environ[USER_ID_ENV] = str(user_id)
        resolved, error = resolve_user_id(db)
        check("MCP_USER_ID 有效 → 采用它", resolved == user_id and not error, error)

        os.environ[USER_ID_ENV] = "99999999"
        resolved, error = resolve_user_id(db)
        check("MCP_USER_ID 指向不存在的用户 → 明确报错", resolved == 0 and "不存在" in error, error)

        os.environ[USER_ID_ENV] = "abc"
        resolved, error = resolve_user_id(db)
        check("MCP_USER_ID 不是整数 → 明确报错", resolved == 0 and "不是整数" in error, error)

        os.environ.pop(USER_ID_ENV, None)
        user_count = len(list(db.scalars(select(User.id).limit(2))))
        resolved, error = resolve_user_id(db)
        if user_count > 1:
            check(
                "未设 MCP_USER_ID 且库里多用户 → 拒绝猜（报错里给出怎么设）",
                resolved == 0 and USER_ID_ENV in error,
                error,
            )
        else:
            check("未设 MCP_USER_ID 且库里唯一用户 → 自动采用", resolved > 0 and not error, error)
    finally:
        if original is None:
            os.environ.pop(USER_ID_ENV, None)
        else:
            os.environ[USER_ID_ENV] = original


# ---------------------------------------------------------------------------
# [4] 客户端 manager：连接失败 / 超时 / 未知工具
# ---------------------------------------------------------------------------
def _slow_server() -> FastMCP:
    """一个故意很慢的工具，用来确定性地触发调用超时。"""
    server = FastMCP(name="slow")

    @server.tool(name="slow_tool", description="睡 5 秒")
    async def slow_tool() -> str:
        await asyncio.sleep(5)
        return "终于好了"

    return server


def _memory_factory(server_builder: Any = build_server) -> Any:
    """内存流工厂：把"连 server"替换成"进程内直连"，于是不需要子进程/网络。"""

    @asynccontextmanager
    async def factory(spec: MCPServerSpec):
        if spec.name == "broken":
            raise RuntimeError("故意连不上（验收用）")
        builder = _slow_server if spec.name == "slow" else server_builder
        async with create_connected_server_and_client_session(builder()) as session:
            yield session

    return factory


def verify_client_manager(user_id: int, kb_id: int) -> None:
    print("\n[4] 客户端 manager（连接失败降级 / 超时 / 未知工具都不打断调用方）")

    # 内存流也走真实的 server 工具实现，而工具要"以某个用户身份"查询。
    # 临时用户一建出来，库里就有不止一个用户了 → 必须显式指定身份（这正是 [3b] 守的行为）。
    original_user_env = os.environ.get(USER_ID_ENV)
    os.environ[USER_ID_ENV] = str(user_id)
    manager = MCPClientManager(
        [
            MCPServerSpec(name="self-kb", transport="stdio", command="unused"),
            MCPServerSpec(name="broken", transport="stdio", command="unused"),
        ],
        connect_timeout=15.0,
        call_timeout=15.0,
        session_factory=_memory_factory(),
    )
    try:
        manager.start()
        rows = {row.name: row for row in manager.status()}
        check("连上的 server 状态为 connected 且工具数正确", rows["self-kb"].connected and rows["self-kb"].tool_count == 3, str(rows["self-kb"].as_dict()))
        check(
            "连不上的 server 记下原因而不是抛异常",
            (not rows["broken"].connected) and "故意连不上" in rows["broken"].error,
            rows["broken"].error,
        )
        check("工具清单只来自连上的 server", {tool.server for tool in manager.remote_tools()} == {"self-kb"})

        payload = json.loads(manager.call("self-kb", "get_mastery", {"knowledge_base_id": kb_id}))
        check(
            "call() 正常返回远端数据",
            [item["mastery_score"] for item in payload["items"]] == [TEMP_MASTERY_SCORE],
            str(payload)[:160],
        )

        broken = json.loads(manager.call("broken", "get_mastery", {}))
        check("调用未连接的 server → error JSON（不抛）", "未连接" in broken["error"], str(broken))

        unknown = json.loads(manager.call("self-kb", "not_a_real_tool", {}))
        check("调用不存在的远端工具 → error JSON（不抛）", "error" in unknown, str(unknown)[:160])
    finally:
        manager.close()
        if original_user_env is None:
            os.environ.pop(USER_ID_ENV, None)
        else:
            os.environ[USER_ID_ENV] = original_user_env

    # 超时路径
    slow = MCPClientManager(
        [MCPServerSpec(name="slow", transport="stdio", command="unused")],
        connect_timeout=15.0,
        call_timeout=0.3,
        session_factory=_memory_factory(),
    )
    try:
        slow.start()
        started = time.perf_counter()
        payload = json.loads(slow.call("slow", "slow_tool", {}))
        elapsed = time.perf_counter() - started
        check("调用超时 → error JSON（不抛）", "超时" in payload["error"], str(payload))
        check("超时确实按配置生效（0.3s 就返回）", elapsed < 3.0, f"{elapsed:.2f}s")
    finally:
        slow.close()


# ---------------------------------------------------------------------------
# [5] 接进 loop：MCP 工具出现在 trace 里，且不会被误当 worker
# ---------------------------------------------------------------------------
def verify_loop_integration(db: Any, user_id: int, session_id: int, kb_id: int) -> None:
    print("\n[5] 接进 agent loop（真 server 代码 + 假模型）")

    # 与 [4] 同理：内存流后面跑的是真 server 工具，需要有明确身份
    original_user_env = os.environ.get(USER_ID_ENV)
    os.environ[USER_ID_ENV] = str(user_id)
    manager = MCPClientManager(
        [MCPServerSpec(name="self-kb", transport="stdio", command="unused")],
        connect_timeout=15.0,
        call_timeout=15.0,
        session_factory=_memory_factory(),
    )
    try:
        manager.start()
        mcp_tools = build_langchain_tools(manager.remote_tools(), manager.call)
        tool_name = mcp_tool_name("self-kb", "get_mastery")
        check("包装出的工具名就是 loop 里要调的名字", tool_name in mcp_tools, str(list(mcp_tools)))

        model = FakeToolCallingModel(
            [
                ("", [_call(tool_name, {"knowledge_base_id": kb_id}, "m1")]),
                ("你的掌握度是 42.5，属于待巩固区间。", []),
            ]
        )
        gateway = FakeWorkerGateway()
        # mcp_tools 显式注入：既证明 loop 支持 MCP，也让验收不依赖 MCP_ENABLED 配置
        service = LearningPartnerService(
            db, llm=model, worker_gateway=gateway, mcp_tools=mcp_tools
        )
        result, events = drive(
            service,
            user_id=user_id,
            session_id=session_id,
            request_text="我的掌握度现在多少",
            load_history=False,
        )

        check("MCP 工具被绑给模型（白名单里可见）", tool_name in model.bound_tools, str(model.bound_tools))
        check("模型只调了 1 次工具就收敛", len(model.calls) == 2, f"calls={len(model.calls)}")

        kinds = [event["type"] for event in events]
        check(
            "事件序列与内置工具完全一致（step/tool_call/tool_result/step/delta）",
            kinds == ["step", "tool_call", "tool_result", "step", "delta"],
            str(kinds),
        )
        tool_call = [event for event in events if event["type"] == "tool_call"][0]
        check("tool_call 事件带 MCP 工具名与参数", tool_call["name"] == tool_name and tool_call["args"] == {"knowledge_base_id": kb_id}, str(tool_call))
        tool_result = [event for event in events if event["type"] == "tool_result"][0]
        check("tool_result 事件标记成功", tool_result["ok"] is True, str(tool_result)[:160])

        observation = [
            message for message in model.calls[1] if isinstance(message, ToolMessage)
        ]
        check(
            "MCP 结果以 ToolMessage 回灌，且含真实掌握度",
            bool(observation) and "42.5" in str(observation[0].content),
            str(observation[0].content)[:160] if observation else "无 ToolMessage",
        )

        check(
            "MCP 工具**不会**被当成 worker 名字去委派",
            gateway.calls == [],
            str(gateway.calls),
        )
        mcp_record = result.tool_calls[0] if result.tool_calls else {}
        check(
            "MCP 工具调用进了落库/回放的轨迹（含成败与耗时）",
            mcp_record.get("name") == tool_name
            and mcp_record.get("args") == {"knowledge_base_id": kb_id}
            and mcp_record.get("ok") is True
            and isinstance(mcp_record.get("elapsed_ms"), int),
            str(mcp_record)[:160],
        )
        check("stop_reason 正常收敛", result.stop_reason == "final_answer", result.stop_reason)

        system_prompt = str(model.calls[0][0].content)
        check(
            "supervisor 提示词里列出了可用的 MCP 工具",
            "【可用的外部 MCP 工具】" in system_prompt and tool_name in system_prompt,
        )

        # 没挂 MCP 时行为必须与升级前一致（默认路径不受影响）
        bare_model = FakeToolCallingModel([("直接回答。", [])])
        bare_service = LearningPartnerService(db, llm=bare_model, worker_gateway=FakeWorkerGateway(), mcp_tools={})
        bare_result, _ = drive(
            bare_service,
            user_id=user_id,
            session_id=session_id,
            request_text="你好",
            load_history=False,
        )
        check("显式不挂 MCP 时 loop 行为不变（1 次模型调用 + 直接答）", len(bare_model.calls) == 1 and bare_result.stop_reason == "final_answer")
        check("MCP 为空时提示词不加 MCP 段落", "【可用的外部 MCP 工具】" not in str(bare_model.calls[0][0].content))
    finally:
        manager.close()
        if original_user_env is None:
            os.environ.pop(USER_ID_ENV, None)
        else:
            os.environ[USER_ID_ENV] = original_user_env


# ---------------------------------------------------------------------------
# [6] 真 stdio 子进程往返（外部 host 的连法）
# ---------------------------------------------------------------------------
def verify_stdio_roundtrip(user_id: int, kb_id: int) -> None:
    print("\n[6] 真 stdio 子进程往返（真起 python -m app.agent.mcp.server）")

    spec = MCPServerSpec(
        name="self-kb",
        transport="stdio",
        command=sys.executable,
        args=("-m", "app.agent.mcp.server"),
        cwd=str(PROJECT_ROOT),
        env={USER_ID_ENV: str(user_id)},
    )
    manager = MCPClientManager([spec], connect_timeout=90.0, call_timeout=90.0)
    try:
        manager.start()
        row = manager.status()[0]
        check("子进程 stdio 握手成功", row.connected, row.error or "")
        if not row.connected:
            return
        check("列出 3 个工具", row.tool_count == 3, str(row.tools))

        payload = manager.call("self-kb", "get_mastery", {"knowledge_base_id": kb_id})
        parsed = json.loads(payload)
        check(
            "跨真实 stdio 拿到临时库掌握度",
            [item["mastery_score"] for item in parsed.get("items", [])] == [TEMP_MASTERY_SCORE],
            str(parsed)[:200],
        )
        check(
            "返回值是干净 JSON（日志没有污染 stdout）",
            isinstance(parsed, dict) and "items" in parsed,
        )

        # 身份由 spec.env 传进子进程：换个不存在的用户，工具必须报错而不是照样返回
        bad_spec = MCPServerSpec(
            name="bad-user",
            transport="stdio",
            command=sys.executable,
            args=("-m", "app.agent.mcp.server"),
            cwd=str(PROJECT_ROOT),
            env={USER_ID_ENV: "99999999"},
        )
        bad_manager = MCPClientManager([bad_spec], connect_timeout=90.0, call_timeout=90.0)
        try:
            bad_manager.start()
            bad_payload = json.loads(bad_manager.call("bad-user", "get_mastery", {}))
            check(
                "子进程身份无效 → 工具返回可读错误（不是 0 分、也不是假数据）",
                "error" in bad_payload and "不存在" in bad_payload["error"],
                str(bad_payload)[:160],
            )
        finally:
            bad_manager.close()
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# [7] 第三方 fetch MCP（只握手 + 列工具，不抓网页）
# ---------------------------------------------------------------------------
def verify_third_party_client() -> None:
    print("\n[7] 第三方 MCP server（官方 mcp-server-fetch，只握手不抓网页）")

    if importlib.util.find_spec("mcp_server_fetch") is None:
        skip("第三方 fetch server", "未安装 mcp-server-fetch（pip install mcp-server-fetch 后再跑）")
        return

    spec = MCPServerSpec(
        name="fetch",
        transport="stdio",
        command=sys.executable,
        args=("-m", "mcp_server_fetch"),
    )
    manager = MCPClientManager([spec], connect_timeout=90.0, call_timeout=60.0)
    try:
        manager.start()
        row = manager.status()[0]
        check("第三方 server 握手成功（证明 client 通用，不只会连自家 server）", row.connected, row.error or "")
        if not row.connected:
            return
        check("列到 fetch 工具", "fetch" in row.tools, str(row.tools))
        tools = build_langchain_tools(manager.remote_tools(), manager.call)
        check(
            "第三方工具也被包成 mcp_fetch_fetch",
            "mcp_fetch_fetch" in tools,
            str(list(tools)),
        )
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# [8] 官方 time MCP（stdio 真子进程：日期/时区是学习排期的硬需求）
# ---------------------------------------------------------------------------
def verify_time_server() -> None:
    print("\n[8] 官方 mcp-server-time（stdio 真子进程，真实日期/时区）")

    if importlib.util.find_spec("mcp_server_time") is None:
        skip("官方 time server", "未安装 mcp-server-time（pip install mcp-server-time==2026.10.10）")
        return

    spec = MCPServerSpec(
        name="time",
        transport="stdio",
        command=sys.executable,
        args=("-m", "mcp_server_time", "--local-timezone=Asia/Shanghai"),
    )
    manager = MCPClientManager([spec], connect_timeout=90.0, call_timeout=60.0)
    try:
        manager.start()
        row = manager.status()[0]
        check("time server 握手成功", row.connected, row.error or "")
        if not row.connected:
            return
        check(
            "列到 get_current_time / convert_time 两个工具",
            row.tools == ("get_current_time", "convert_time"),
            str(row.tools),
        )
        payload = json.loads(manager.call("time", "get_current_time", {"timezone": "Asia/Shanghai"}))
        check(
            "get_current_time 返回真实时间（含时区，模型不用再心算日期）",
            "datetime" in payload and payload.get("timezone") == "Asia/Shanghai",
            str(payload)[:140],
        )
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# [9] DeepWiki 远端 MCP（streamable-http：远端传输的实战验收，需联网）
# ---------------------------------------------------------------------------
def verify_deepwiki_remote() -> None:
    print("\n[9] DeepWiki 远端 MCP（streamable-http，只握手 + 列工具，不调业务工具）")

    spec = MCPServerSpec(
        name="deepwiki", transport="streamable-http", url="https://mcp.deepwiki.com/mcp"
    )
    manager = MCPClientManager([spec], connect_timeout=45.0, call_timeout=45.0)
    try:
        manager.start()
        row = manager.status()[0]
        if not row.connected:
            # 外部服务不在验收的可控范围内：连不上如实跳过，不算失败（离线跑本脚本仍然全绿）
            skip("DeepWiki 远端握手", f"连不上（{row.error}）——离线或服务不可用")
            return
        check("远端 streamable-http 传输打通（本项目 client 连远端 server）", True, f"{row.tool_count} 个工具")
        check(
            "列到 wiki 问答/阅读工具",
            {"ask_wiki_question", "read_wiki_contents", "read_wiki_structure"} & set(row.tools) != set(),
            str(row.tools),
        )
        tools = build_langchain_tools(manager.remote_tools(), manager.call)
        check(
            "远端工具同样被包装成 mcp_deepwiki_*（与本地 server 无差别进 loop）",
            any(name.startswith("mcp_deepwiki_") for name in tools),
            str(list(tools))[:140],
        )
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="第 3 期 MCP 验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    parser.add_argument("--skip-stdio", action="store_true", help="跳过第 6/7/8 段（真子进程）")
    parser.add_argument("--skip-net", action="store_true", help="跳过第 9 段（DeepWiki 远端，需联网）")
    args = parser.parse_args()

    global db
    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    username = f"vmcp{suffix}"  # users.username 只有 20 字符
    user_id = kb_id = session_id = None
    try:
        user = User(username=username, password=hash_password(TEMP_PASSWORD))
        db.add(user)
        db.flush()
        user_id = user.id

        kb = KnowledgeBase(user_id=user_id, name=f"临时MCP验收库-{suffix}")
        db.add(kb)
        db.flush()
        kb_id = kb.id

        session = SessionModel(user_id=user_id, title="临时MCP会话", session_model=0)
        db.add(session)
        db.commit()
        session_id = session.id

        db.add(
            UserKnowledgeMastery(
                user_id=user_id,
                knowledge_base_id=kb_id,
                mastery_score=TEMP_MASTERY_SCORE,
                practice_count=2,
            )
        )
        db.commit()
        print(
            f"临时数据：user={user_id}（{username}） kb={kb_id} session={session_id} "
            f"mastery={TEMP_MASTERY_SCORE}"
        )

        verify_config()
        verify_wrapping()
        verify_server_protocol(user_id, kb_id)
        verify_identity(db, user_id)
        verify_client_manager(user_id, kb_id)
        verify_loop_integration(db, user_id, session_id, kb_id)
        if args.skip_stdio:
            skip("真 stdio 子进程往返", "--skip-stdio")
            skip("第三方 fetch MCP", "--skip-stdio")
        else:
            verify_stdio_roundtrip(user_id, kb_id)
            verify_third_party_client()

        if args.skip_stdio or args.skip_net:
            skip("官方 time MCP", "--skip-stdio/--skip-net")
            skip("DeepWiki 远端 MCP", "--skip-stdio/--skip-net")
        else:
            verify_time_server()
            verify_deepwiki_remote()

        print(f"\n结果：{passed} 通过 / {failed} 失败 / {skipped} 跳过")
        return 1 if failed else 0
    finally:
        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id}")
        else:
            try:
                if user_id:
                    # 清理顺序遵循交接文档坑 #38：先删子表，再删父表
                    db.execute(sql_delete(ChatMessage).where(ChatMessage.user_id == user_id))
                    db.execute(sql_delete(SessionModel).where(SessionModel.user_id == user_id))
                    db.execute(
                        sql_delete(UserKnowledgeMastery).where(
                            UserKnowledgeMastery.user_id == user_id
                        )
                    )
                    db.execute(sql_delete(UserProfile).where(UserProfile.user_id == user_id))
                    db.execute(sql_delete(KnowledgeBase).where(KnowledgeBase.user_id == user_id))
                    db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"已清理临时数据：user={user_id}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"清理失败（可手工删）：{exc}")
        reset_mcp_manager()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

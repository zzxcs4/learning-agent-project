"""MCP 客户端：把外部 MCP server 的工具接进 supervisor 的 agent loop。

## 为什么要有这一层（而不是直接在 loop 里 import mcp）

1. **MCP 客户端是异步的，本项目的 loop 是同步的**（FastAPI 同步生成器 + 同步 SQLAlchemy）。
   所以这里用一个**常驻后台线程 + 它自己的事件循环**做桥：连接、列工具、调用都发生在
   那个循环里，同步侧只通过 `run_coroutine_threadsafe(...).result(timeout)` 取结果。
   另一条路是把整条链路 async 化（含 SQLAlchemy 会话与 SSE 生成器），代价远超这一期。
2. **进程级单例**：一个后端进程只连一次，多个 loop 复用（握手 + 列工具并不便宜）。
3. **工具名加前缀** `mcp_<server>_<tool>`：内置原子工具与 MCP 工具在同一个白名单里并存，
   前缀让"这个工具从哪来"在 trace 与提示词里都一眼可见。
4. **失败绝不打断 loop**：连接失败降级成"没有 MCP 工具"；调用异常/超时收敛成
   给模型看的 JSON 观察结果——与 `ToolRuntime` 的契约一致。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from collections.abc import Callable
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from app.agent.mcp.spec import (
    TRANSPORT_STDIO,
    TRANSPORT_STREAMABLE_HTTP,
    MCPServerSpec,
    load_specs,
)
from app.core.logging_config import main_logger
from app.core.settings import settings

# 工具名前缀：`mcp_<server>_<tool>`。与内置工具（search_material 等）区分开，
# 前端也能只靠名字把"MCP 工具"标出来（不需要额外的映射表）。
MCP_TOOL_PREFIX = "mcp_"
# OpenAI 风格的函数名上限 64 字符，超了会被模型侧拒绝
MAX_TOOL_NAME_CHARS = 64
# 单次 MCP 调用回灌给模型的字符上限，与 ToolRuntime.MAX_OBSERVATION_CHARS 对齐
MAX_OBSERVATION_CHARS = 6000
# 非文本内容（图片/音频）只报告数量，不把二进制塞进上下文
_NON_TEXT_PART_LABEL = {"image": "图片", "audio": "音频", "resource": "资源"}

# 子进程**默认就该继承**的变量：它们决定子进程能不能正常 `import app`。
# 最典型的是 `LOG_DIR`——受限权限环境里日志目录写不了会让 loguru 初始化直接失败
# （交接文档坑 #40），而 SDK 的安全默认环境变量清单里**没有**它，必须显式透传。
_CHILD_ENV_PASSTHROUGH = ("LOG_DIR", "APP_ENV", "PYTHONIOENCODING", "PYTHONPATH", "PYTHONUTF8")


def _sanitize(part: str) -> str:
    """把名字收敛成 `[a-zA-Z0-9_-]`（函数名允许的字符集），其余替换成下划线。"""
    return re.sub(r"[^0-9a-zA-Z_-]", "_", part or "").strip("_") or "x"


def mcp_tool_name(server: str, tool: str) -> str:
    """本地工具名：`mcp_<server>_<tool>`（超长截断，保证是合法函数名）。"""
    return f"{MCP_TOOL_PREFIX}{_sanitize(server)}_{_sanitize(tool)}"[:MAX_TOOL_NAME_CHARS]


def is_mcp_tool_name(name: str) -> bool:
    """是否是我们包装过的 MCP 工具（前端/提示词据此归类，不查服务器状态）。"""
    return str(name or "").startswith(MCP_TOOL_PREFIX)


@dataclass(frozen=True)
class MCPRemoteTool:
    """远端 server 暴露的一个工具（连上后 `list_tools` 的缓存结果）。"""

    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def lc_name(self) -> str:
        return mcp_tool_name(self.server, self.name)


@dataclass
class MCPServerStatus:
    """一个 server 的连接状态（供日志、验收脚本与前端"已接入的 MCP"面板使用）。"""

    name: str
    connected: bool
    transport: str
    tool_count: int = 0
    tools: tuple[str, ...] = ()
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self.connected,
            "transport": self.transport,
            "tool_count": self.tool_count,
            "tools": list(self.tools),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# JSON Schema → pydantic 模型：把远端工具的参数 schema 变成 LangChain 认的 args_schema
# ---------------------------------------------------------------------------
_JSON_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _field_for(raw: Any, required: bool) -> tuple[Any, Any]:
    """把一个 JSON Schema 属性转成 `create_model` 的字段定义。"""
    schema = raw if isinstance(raw, dict) else {}
    hint = _JSON_TYPES.get(str(schema.get("type") or "").strip().lower(), Any)
    if hint is list:
        hint = list[Any]
    elif hint is dict:
        hint = dict[str, Any]

    enum = schema.get("enum")
    if hint is str and isinstance(enum, list) and enum and all(
        isinstance(item, str) for item in enum
    ):
        try:
            # 枚举写成 Literal 可以让模型看到合法取值（比裸 string 更不容易传错）
            hint = Literal[tuple(enum)]  # type: ignore[valid-type]
        except TypeError:  # pragma: no cover - 极端 schema 才走到
            hint = str

    description = str(schema.get("description") or "").strip()
    default: Any = ... if required else None
    return (hint, Field(default, description=description) if description else default)


def _passthrough_model(model_name: str) -> type[BaseModel]:
    """宽松模型：不建模字段，任何参数原样透传。

    什么时候用：远端 schema 的参数名不是合法 Python 标识符（`x-id`、`a.b`），
    或者根本没声明 properties。这种情况下"少校验、不丢参数"比"强行建模"更重要。
    """

    class _Passthrough(BaseModel):
        model_config = {"extra": "allow"}

    return create_model(model_name, __base__=_Passthrough)


def schema_to_pydantic(model_name: str, schema: dict[str, Any] | None) -> type[BaseModel]:
    """把 JSON Schema 转成 pydantic 模型；无法建模时退化成原样透传的宽松模型。"""
    if not isinstance(schema, dict):
        return _passthrough_model(model_name)
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return _passthrough_model(model_name)
    required = set(schema.get("required") or [])
    fields: dict[str, tuple[Any, Any]] = {}
    for key, raw in properties.items():
        if not str(key).isidentifier():
            return _passthrough_model(model_name)
        fields[str(key)] = _field_for(raw, str(key) in required)
    try:
        return create_model(model_name, **fields)
    except Exception as exc:  # pragma: no cover - schema 畸形时的兜底
        main_logger.warning("MCP 工具 {} 的参数 schema 无法建模，退化为透传：{}", model_name, exc)
        return _passthrough_model(model_name)


# ---------------------------------------------------------------------------
# 结果转换
# ---------------------------------------------------------------------------
def _unwrap_unstructured(structured: Any, text: str) -> Any:
    """剥掉 FastMCP 对"只返回字符串的工具"加的那层包装。

    实测（`mcp 1.30.0`）：工具函数返回 `str` 时，SDK 会在 `structuredContent` 里再放一份
    `{"result": <那个字符串>}`。它只是协议机械产物，对模型没有任何信息量，却会把
    "一段干净的 JSON 文本"变成 `{"text": "...", "structured": {"result": "..."}}`——
    既浪费上下文，也让前端预览变难读。识别出这种形状就丢掉它。
    """
    if (
        isinstance(structured, dict)
        and set(structured) == {"result"}
        and isinstance(structured.get("result"), str)
        and structured.get("result") == text
    ):
        return None
    return structured


def result_to_text(result: Any) -> str:
    """把 `CallToolResult` 收敛成给模型看的字符串（文本优先，附结构化结果）。"""
    texts: list[str] = []
    others: list[str] = []
    for part in getattr(result, "content", None) or []:
        part_type = str(getattr(part, "type", "") or "")
        if part_type == "text":
            texts.append(str(getattr(part, "text", "") or ""))
        else:
            others.append(_NON_TEXT_PART_LABEL.get(part_type, part_type or "未知内容"))

    text = "\n".join(item for item in texts if item).strip()
    structured = _unwrap_unstructured(getattr(result, "structuredContent", None), text)
    is_error = bool(getattr(result, "isError", False))

    if is_error:
        payload: dict[str, Any] = {"error": text or "MCP 工具返回了错误但未给出说明"}
        if structured is not None:
            payload["structured"] = structured
        return json.dumps(payload, ensure_ascii=False, default=str)[:MAX_OBSERVATION_CHARS]

    if structured is not None:
        # 同时有文本与结构化结果时都给出来：模型可以选更好用的那份
        payload = {"text": text, "structured": structured} if text else {"structured": structured}
        return json.dumps(payload, ensure_ascii=False, default=str)[:MAX_OBSERVATION_CHARS]

    if text:
        return text[:MAX_OBSERVATION_CHARS]
    if others:
        return json.dumps(
            {"note": f"MCP 工具只返回了非文本内容（{'/'.join(others)}），未纳入上下文"},
            ensure_ascii=False,
        )
    return json.dumps({"note": "MCP 工具没有返回任何内容"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 同步桥：常驻后台线程 + 它自己的事件循环
# ---------------------------------------------------------------------------
@dataclass
class _ServerState:
    """一个已连接 server 的运行时状态。

    `requests`：同步侧把"要调什么"投进来的队列。**调用必须回到 owning task 里执行**
    （见 `_serve_one` 的说明），所以不直接跨任务用 session。
    """

    spec: MCPServerSpec
    session: Any
    tools: tuple[Any, ...] = ()
    requests: Any = None


def _settle(future: Any, *, value: Any = None, error: BaseException | None = None) -> None:
    """把结果交给等待方；对方已经超时/取消时**丢弃结果**而不是抛异常。

    `asyncio.Future.set_result` 在已取消的 future 上会抛 `InvalidStateError`，
    如果不拦住，一次"调用超时"就会把整个 server 任务打死——那比超时本身严重得多。
    """
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(value)


class MCPClientManager:
    """连接若干个 MCP server，并把它们的工具以同步接口暴露给 loop。

    `session_factory` 是给离线验收用的缝隙：传一个
    `async def factory(spec) -> AsyncContextManager[ClientSession]`，
    就能用**内存流**（`mcp.shared.memory`）代替真实进程/网络，
    从而在不启动任何子进程的情况下验证"包装 → 调用 → 结果收敛"整条链路。
    """

    def __init__(
        self,
        specs: list[MCPServerSpec],
        *,
        connect_timeout: float | None = None,
        call_timeout: float | None = None,
        session_factory: Callable[[MCPServerSpec], Any] | None = None,
    ):
        self._specs = tuple(specs)
        self._connect_timeout = float(
            connect_timeout if connect_timeout is not None else settings.mcp_connect_timeout_seconds
        )
        self._call_timeout = float(
            call_timeout if call_timeout is not None else settings.mcp_call_timeout_seconds
        )
        self._session_factory = session_factory

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._serve_future: Any = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._states: dict[str, _ServerState] = {}
        self._errors: dict[str, str] = {}
        self._readiness: dict[str, asyncio.Event] = {}
        self._started = False
        self._start_lock = threading.Lock()

    # ---------------------------------------------------------------- 生命周期
    @property
    def specs(self) -> tuple[MCPServerSpec, ...]:
        return self._specs

    def start(self) -> None:
        """启动后台循环并连接所有 server（幂等；连接失败只记日志）。"""
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            self._started = True
            if not self._specs:
                self._ready.set()
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._thread_main, name="mcp-client", daemon=True
            )
            self._thread.start()
            self._serve_future = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
            # 等待"连完一轮"（每个 server 各自有超时，所以这里不会无限等）
            if not self._ready.wait(timeout=self._connect_timeout + 5.0):
                main_logger.warning("MCP 客户端连接超时（{}s），本轮不挂载 MCP 工具", self._connect_timeout)

    def _thread_main(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _serve(self) -> None:
        """后台循环里的常驻协程：并发连接各 server → 等停止信号 → 收尾。"""
        self._stop_event = asyncio.Event()
        self._readiness = {spec.name: asyncio.Event() for spec in self._specs}
        tasks = [asyncio.create_task(self._serve_one(spec)) for spec in self._specs]
        try:
            # 等每个 server 走完"连接 + 列工具"（各自有超时，所以这里不会无限等）
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in self._readiness.values())),
                timeout=self._connect_timeout + 5.0,
            )
        except (asyncio.TimeoutError, TimeoutError):
            main_logger.warning(
                "MCP 客户端等待连接完成超时（{}s），未连上的 server 本轮不挂载", self._connect_timeout
            )
        except Exception as exc:  # pragma: no cover - 连接阶段不该把 loop 带崩
            main_logger.warning("MCP 客户端连接阶段异常（{}）：{}", type(exc).__name__, exc)
        finally:
            self._ready.set()

        if self._states:
            main_logger.info(
                "MCP 已接入 {} 个 server：{}",
                len(self._states),
                ", ".join(
                    f"{name}({len(state.tools)} 个工具)" for name, state in self._states.items()
                ),
            )
        for name, error in self._errors.items():
            main_logger.warning("MCP server {}(连接失败，已跳过)：{}", name, error)

        await self._stop_event.wait()
        # 用 None 作为"收工"信号，让每个 server 任务在**它自己的任务里**退出 async with，
        # 从而在同一个任务里关闭会话（anyio 的 cancel scope 不允许跨任务退出）。
        for state in list(self._states.values()):
            try:
                state.requests.put_nowait(None)
            except Exception:  # pragma: no cover - 队列已满/已关闭
                pass
        await asyncio.gather(*tasks, return_exceptions=True)
        self._states.clear()

    async def _serve_one(self, spec: MCPServerSpec) -> None:
        """单个 server 的完整生命周期：连接 → 列工具 → 串行处理调用 → 关闭。

        为什么调用也在这个任务里做（而不是同步侧直接 `session.call_tool`）：
        MCP 会话底层是 anyio 的流与 cancel scope，**跨任务使用会出问题**
        （实测内存传输会直接报 `Attempted to exit cancel scope in a different task`，
        跨任务调用也会拿到错误结果）。所以这里用队列把请求收进来，在 owning task 里执行、
        再把结果交给等待方——同步侧只跟队列打交道。
        """
        ready = self._readiness[spec.name]
        try:
            async with AsyncExitStack() as stack:
                try:
                    session = await asyncio.wait_for(
                        stack.enter_async_context(self._open_session(spec)), self._connect_timeout
                    )
                    listed = await asyncio.wait_for(session.list_tools(), self._connect_timeout)
                except Exception as exc:
                    self._errors[spec.name] = f"{type(exc).__name__}: {exc}"
                    return
                state = _ServerState(
                    spec=spec,
                    session=session,
                    tools=tuple(getattr(listed, "tools", ()) or ()),
                    requests=asyncio.Queue(),
                )
                self._states[spec.name] = state
                ready.set()

                while True:
                    request = await state.requests.get()
                    if request is None:  # 收工信号
                        return
                    future, tool, arguments = request
                    if future.done():  # 同步侧已经超时/取消，没必要再调
                        continue
                    try:
                        result = await session.call_tool(tool, arguments)
                    except Exception as exc:
                        _settle(future, error=exc)
                    else:
                        _settle(future, value=result)
        finally:
            ready.set()

    @asynccontextmanager
    async def _open_session(self, spec: MCPServerSpec):
        """按传输方式打开一个已初始化的 MCP 会话。"""
        if self._session_factory is not None:
            async with self._session_factory(spec) as session:
                yield session
            return

        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

        if spec.transport == TRANSPORT_STREAMABLE_HTTP:
            from mcp.client.streamable_http import streamable_http_client

            # streamable_http_client 产出 (read, write, get_session_id) 三段
            async with streamable_http_client(spec.url) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
            return

        if spec.transport != TRANSPORT_STDIO:  # pragma: no cover - 解析期已拦住
            raise ValueError(f"不支持的 MCP 传输方式：{spec.transport}")

        # 显式给 env 时 SDK **不再**补默认环境变量，所以这里自己把安全默认值并上：
        # 少了 SYSTEMROOT/PATH 这类变量，Windows 上子进程可能直接起不来；
        # 再并上 _CHILD_ENV_PASSTHROUGH（如 LOG_DIR），最后才是 spec 里显式写的。
        inherited = {
            key: os.environ[key] for key in _CHILD_ENV_PASSTHROUGH if os.environ.get(key)
        }
        params = StdioServerParameters(
            command=spec.command,
            args=list(spec.args),
            cwd=spec.cwd,
            env={**get_default_environment(), **inherited, **spec.env},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    def close(self) -> None:
        """停止后台循环并关闭所有会话（幂等）。"""
        if not self._started:
            return
        self._started = False
        loop, thread, stop_event = self._loop, self._thread, self._stop_event
        serve_future = self._serve_future
        self._loop = None
        self._thread = None
        self._serve_future = None
        if loop is None:
            return
        try:
            if stop_event is not None:
                loop.call_soon_threadsafe(stop_event.set)
            if serve_future is not None:
                # 等收尾协程把各个会话关闭掉（它自己会 aclose 所有 ExitStack）
                serve_future.result(timeout=5.0)
            loop.call_soon_threadsafe(loop.stop)
        except Exception as exc:  # pragma: no cover - 退出路径不该抛
            main_logger.warning("关闭 MCP 客户端时出现异常：{}", exc)
        if thread is not None:
            thread.join(timeout=3.0)
        self._states.clear()

    # ---------------------------------------------------------------- 查询与调用
    @property
    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    def remote_tools(self) -> list[MCPRemoteTool]:
        """所有已连接 server 的工具（缓存结果，调用不产生网络往返）。"""
        collected: list[MCPRemoteTool] = []
        for name, state in self._states.items():
            for tool in state.tools:
                collected.append(
                    MCPRemoteTool(
                        server=name,
                        name=str(getattr(tool, "name", "") or ""),
                        description=str(getattr(tool, "description", "") or ""),
                        input_schema=dict(getattr(tool, "inputSchema", None) or {}),
                    )
                )
        return [tool for tool in collected if tool.name]

    def status(self) -> list[MCPServerStatus]:
        """每个 server 的连接状态（含未连上的，带错误原因）。"""
        rows: list[MCPServerStatus] = []
        for spec in self._specs:
            state = self._states.get(spec.name)
            if state is None:
                rows.append(
                    MCPServerStatus(
                        name=spec.name,
                        connected=False,
                        transport=spec.transport,
                        error=self._errors.get(spec.name, "未连接"),
                    )
                )
                continue
            names = tuple(str(getattr(tool, "name", "") or "") for tool in state.tools)
            rows.append(
                MCPServerStatus(
                    name=spec.name,
                    connected=True,
                    transport=spec.transport,
                    tool_count=len(state.tools),
                    tools=names,
                )
            )
        return rows

    def call(self, server: str, tool: str, arguments: dict[str, Any] | None = None) -> str:
        """同步调用一个远端 MCP 工具；**永远返回字符串**（异常与超时也是观察结果）。"""
        self.start()
        state = self._states.get(server)
        if state is None:
            return json.dumps(
                {
                    "error": f"MCP server {server} 未连接，工具 {tool} 未执行",
                    "detail": self._errors.get(server, ""),
                },
                ensure_ascii=False,
            )
        loop = self._loop
        if loop is None or state.requests is None:
            return json.dumps({"error": "MCP 客户端未启动，工具未执行"}, ensure_ascii=False)

        async def _invoke() -> Any:
            """在后台循环里排队等结果；真正的调用由 owning task 执行。"""
            pending: asyncio.Future = asyncio.get_running_loop().create_future()
            await state.requests.put((pending, tool, dict(arguments or {})))
            return await pending

        try:
            future = asyncio.run_coroutine_threadsafe(_invoke(), loop)
            result = future.result(timeout=self._call_timeout)
        except FuturesTimeoutError:
            future.cancel()
            return json.dumps(
                {"error": f"MCP 工具 {server}/{tool} 调用超时（{self._call_timeout:.0f}s）"},
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {"error": f"MCP 工具 {server}/{tool} 调用失败：{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        return result_to_text(result)


# ---------------------------------------------------------------------------
# LangChain 工具包装
# ---------------------------------------------------------------------------
def build_langchain_tools(
    remote_tools: list[MCPRemoteTool],
    call: Callable[[str, str, dict[str, Any]], str],
) -> dict[str, Any]:
    """把远端工具列表包装成 `{本地工具名: StructuredTool}`。

    `call(server, tool, args)` 由调用方提供，所以这个函数可以脱离真实连接单测。
    """
    from langchain_core.tools import StructuredTool

    tools: dict[str, Any] = {}
    for remote in remote_tools:
        def _invoke(_remote: MCPRemoteTool = remote, **kwargs: Any) -> str:
            return call(_remote.server, _remote.name, dict(kwargs))

        tools[remote.lc_name] = StructuredTool.from_function(
            func=_invoke,
            name=remote.lc_name,
            description=(
                f"[MCP:{remote.server}] {remote.description or remote.name}"
            ).strip(),
            args_schema=schema_to_pydantic(f"{remote.lc_name}_args", remote.input_schema),
        )
    return tools


# ---------------------------------------------------------------------------
# 进程级单例
# ---------------------------------------------------------------------------
_manager: MCPClientManager | None = None
_manager_lock = threading.Lock()


def get_mcp_manager() -> MCPClientManager | None:
    """按配置返回进程级单例；`MCP_ENABLED` 未开或没配 server 时返回 None（零副作用）。"""
    if not settings.mcp_enabled:
        return None
    global _manager
    with _manager_lock:
        if _manager is None:
            specs, problems = load_specs()
            for problem in problems:
                main_logger.warning("MCP 配置有问题：{}", problem)
            if not specs:
                main_logger.warning("MCP_ENABLED 已开，但 MCP_SERVERS 里没有可用的 server 配置")
                return None
            _manager = MCPClientManager(specs)
    return _manager


def reset_mcp_manager() -> None:
    """关闭并丢弃单例（验收脚本与配置热改后使用）。"""
    global _manager
    with _manager_lock:
        manager, _manager = _manager, None
    if manager is not None:
        manager.close()


def mcp_langchain_tools() -> dict[str, Any]:
    """给 loop 用的 MCP 工具表；MCP 未开/连不上时返回空字典（loop 行为不变）。"""
    manager = get_mcp_manager()
    if manager is None:
        return {}
    manager.start()
    return build_langchain_tools(manager.remote_tools(), manager.call)


def mcp_status_payload() -> dict[str, Any]:
    """MCP 接入状态（供日志、验收脚本与前端面板）。MCP 未启用时明确说明原因。"""
    if not settings.mcp_enabled:
        return {"enabled": False, "reason": "MCP_ENABLED 未开启", "servers": []}
    manager = get_mcp_manager()
    if manager is None:
        return {"enabled": True, "reason": "MCP_SERVERS 里没有可用配置", "servers": []}
    manager.start()
    servers = [row.as_dict() for row in manager.status()]
    return {
        "enabled": True,
        "reason": "",
        "servers": servers,
        "tool_count": sum(row["tool_count"] for row in servers),
    }


def describe_mcp_tools_for_prompt(tools: dict[str, Any] | None = None) -> str:
    """把可用的 MCP 工具写进 supervisor 提示词（没有时返回空串，不占预算）。

    `tools` 传"已经包装好的本地工具表"时直接读它，不再去问 manager——
    少一次连接、也保证提示词里写的名字与白名单里的名字**必然一致**。
    """
    rows: list[tuple[str, str]] = []
    if tools is not None:
        for name, tool in tools.items():
            if is_mcp_tool_name(name):
                rows.append((name, str(getattr(tool, "description", "") or "")))
    else:
        manager = get_mcp_manager()
        if manager is None:
            return ""
        manager.start()
        rows = [(item.lc_name, item.description) for item in manager.remote_tools()]

    if not rows:
        return ""

    lines = ["", "【可用的外部 MCP 工具】这些工具由外部 MCP server 提供，与上面的原子工具用法相同："]
    for name, description in rows:
        text = (description or "").strip().replace("\n", " ")
        if len(text) > 80:
            text = f"{text[:80]}…"
        lines.append(f"- `{name}`：{text or '（无说明）'}")
    return "\n".join(lines)

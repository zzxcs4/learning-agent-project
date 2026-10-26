"""闸门 + 诊断：agent loop 的 `delta` 事件是"边生成边发"还是"生成完再一起发"。

为什么需要它
------------
用户报的现象是"协议层确实是流式（有 N 个 delta 事件），但界面上没有打字机效果，
文字像一起崩出来"。静态检查（语法/类型/SFC）**证明不了**这件事——它的正确性完全
体现在**时间轴**上，而时间轴只能跑出来看。

这个脚本做两件事：
1. 诊断：给假模型一个"每吐一片都 sleep"的流，打印每个事件的墙钟时间；
2. 闸门：断言增量确实被铺开了（下面的 8 条 check），回归时能立刻抓到
   "有人又把 `_stream_model_step` 改回返回 list"。

背景（第 13 轮的根因，交接 §Q）：`_stream_model_step` 原本是**普通函数**，返回
`(累积消息, 增量列表)`。它必须把 `bound.stream(...)` 抽干才能 `return`，调用方
再 `for delta in deltas` 重放——模型早就生成完了，12 个 delta 全落在同一毫秒
（实测跨度 0.000s、平均间隔 0.0ms）。改成生成器后 yield 点回到模型流内部。

跑法（**只读，不写库，不调 LLM，不花 token**）：
    $env:LOG_DIR="$env:TEMP\\dsh-verify-logs"
    D:\\conda_envs\\zxlc_agent\\python.exe scripts\\probe_loop_delta_timing.py

`--chunks` 改片数，`--delay` 改每片之间的 sleep。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# GBK 控制台下打印 ✅/⚠️ 会抛 UnicodeEncodeError（交接坑 #47）。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.messages import AIMessageChunk  # noqa: E402

from app.agent.learning_partner import LearningPartnerService  # noqa: E402
from app.db.database import SessionLocal  # noqa: E402

passed = 0
failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}{f' —— {detail}' if detail else ''}")


class SleepingModel:
    """假模型：分 `chunks` 片吐字，每片之间真的 sleep。

    `sleep` 是打字机效果的**物理来源**：真模型每生成一片也要花时间，只是那段时间
    原本被 `_stream_model_step` 的抽干循环吃掉了。

    `reasoning_pieces > 0` 时，先吐这么多片"思考内容"（放在 `additional_kwargs`
    的 `reasoning_content` 里，与真 relay 的形状一致），再吐正文。
    """

    def __init__(
        self,
        script: list[tuple[str, list[dict[str, Any]]]],
        delay: float,
        pieces: int = 1,
        reasoning_pieces: int = 0,
    ):
        self.script = list(script)
        self.delay = delay
        self.pieces = pieces
        self.reasoning_pieces = reasoning_pieces
        self.calls = 0

    def bind_tools(self, tools: list[Any]) -> "SleepingModel":
        return self

    def stream(self, messages: list[Any], config: Any = None) -> Any:
        # config：loop 以 `config={"callbacks": [用量收集器]}` 调用（第 3 期可观测），
        # 本桩不消费它，但形参必须接住，否则 TypeError。
        self.calls += 1
        index = min(self.calls - 1, len(self.script) - 1)
        text, tool_calls = self.script[index]
        # 思考内容：模仿 relay，放在 additional_kwargs.reasoning_content
        for piece_index in range(self.reasoning_pieces):
            time.sleep(self.delay)
            yield AIMessageChunk(
                content="",
                additional_kwargs={"reasoning_content": f"想{piece_index}"},
            )
        # 把这一步的文字切成**恰好** `pieces` 片，每片之间 sleep —— 模拟逐 token 生成。
        # 按长度下取整分片在长度不是片数整数倍时会多切/少切（片数断言会误报），
        # 所以用整数边界直接均分，保证片数恒等于 `pieces`。
        if text:
            bounds = [(index * len(text)) // self.pieces for index in range(self.pieces + 1)]
            for index in range(self.pieces):
                piece = text[bounds[index] : bounds[index + 1]]
                if not piece:
                    continue
                time.sleep(self.delay)
                yield AIMessageChunk(content=piece)
        elif not self.reasoning_pieces:
            time.sleep(self.delay)
        if tool_calls:
            yield AIMessageChunk(content="", tool_calls=list(tool_calls))

    def invoke(self, messages: list[Any]) -> Any:  # worker 用，本脚本不会走到
        return AIMessageChunk(content="")


class NoopGateway:
    """避免构造真网关；本脚本只让模型调内置只读工具，不走委派。"""

    def delegate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"worker_error": "probe: 不该被调用"}


def call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def drive(service: LearningPartnerService, **kwargs: Any) -> tuple[Any, list[tuple[float, dict[str, Any]]]]:
    """把 `run_events` 跑到底，返回（LoopResult, [(相对时间, 事件), ...]）。"""
    started = time.perf_counter()
    events: list[tuple[float, dict[str, Any]]] = []
    generator = service.run_events(**kwargs)
    while True:
        try:
            event = next(generator)
        except StopIteration as stop:
            return stop.value, events
        events.append((time.perf_counter() - started, event))


def section(title: str) -> None:
    print(f"\n[{title}]")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", type=int, default=12, help="模型分成几片输出")
    parser.add_argument("--delay", type=float, default=0.05, help="每片之间的秒数")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        # ---------------------------------------------------------------- 单步直答
        section("单步直答：增量必须被时间铺开")
        text = "".join(f"片{index}" for index in range(args.chunks))
        model = SleepingModel([(text, [])], delay=args.delay, pieces=args.chunks)
        service = LearningPartnerService(
            db,
            llm=model,
            worker_gateway=NoopGateway(),
            # 不挂 MCP（免得去连外部进程），也不读历史（免得依赖库里的真实会话）
            mcp_tools={},
        )
        result, timed = drive(
            service,
            user_id=1,
            session_id=1,
            request_text="这一轮只是为了测时间戳",
            load_history=False,
            session_model=0,
        )
        deltas = [t for t, event in timed if event["type"] == "delta"]
        generation = args.chunks * args.delay
        total = timed[-1][0]

        print()  # 空行：时间线之上
        print("  事件时间线（相对开始，秒）：")
        for offset, event in timed:
            print(f"    t={offset:7.3f}  {event['type']}")

        check(f"delta 事件数 = 片数（{len(deltas)}）", len(deltas) == args.chunks, f"{len(deltas)}")

        first = deltas[0] if deltas else 0.0
        spread = (deltas[-1] - deltas[0]) if len(deltas) > 1 else 0.0
        gaps = [b - a for a, b in zip(deltas, deltas[1:])]
        mean_gap = sum(gaps) / len(gaps) if gaps else 0.0

        # 核心不变式①：首个 delta 必须**远早于**生成结束。旧写法这里是等于生成耗时。
        check(
            f"首个 delta 在生成完成前到达（t={first:.3f}s < {generation * 0.6:.3f}s）",
            first < generation * 0.6,
            f"t={first:.3f}s，生成耗时约 {generation:.3f}s —— 说明又变成攒完再发了",
        )
        # 核心不变式②：相邻 delta 之间要有**真实间隔**。旧写法这里是 0.0ms。
        check(
            f"相邻 delta 平均间隔 ≈ 片间隔（{mean_gap * 1000:.1f}ms ≥ {args.delay * 500:.0f}ms）",
            mean_gap >= args.delay * 0.5,
            f"平均 {mean_gap * 1000:.1f}ms，期望约 {args.delay * 1000:.0f}ms —— 挤在一起就是又攒批了",
        )
        # 核心不变式③：整段增量的时间跨度要接近生成耗时。
        check(
            f"增量跨度占生成耗时 ≥40%（{spread:.3f}s / {generation:.3f}s）",
            spread >= generation * 0.4,
            f"跨度 {spread:.3f}s，生成 {generation:.3f}s",
        )
        check("答案文本不受影响（与逐片拼接一致）", result.answer == text, repr(result.answer))
        check("stop_reason=final_answer", result.stop_reason == "final_answer", str(result.stop_reason))
        check("事件序列仍以 step 开头、delta 结尾", timed[0][1]["type"] == "step" and timed[-1][1]["type"] == "delta")

        # ------------------------------------------------- 带文字的工具步骤（顺序变化）
        section("带文字的工具步骤：这一步的文字先于它的 tool_call")
        model = SleepingModel(
            [
                ("我先查一下你的薄弱点。", [call("get_weak_points", {"limit": 3}, "c1")]),
                ("你最弱的是索引，建议先补这块。", []),
            ],
            delay=args.delay,
            pieces=4,
        )
        service = LearningPartnerService(db, llm=model, worker_gateway=NoopGateway(), mcp_tools={})
        result, timed = drive(
            service,
            user_id=1,
            session_id=1,
            request_text="我哪里弱",
            load_history=False,
            session_model=0,
        )
        kinds = [event["type"] for _, event in timed]
        print(f"    事件序列：{kinds}")

        # 旧写法下这里一定是 [step, tool_call, tool_result, step, delta...]：
        # 第 1 步的文字要等 tool 跑完才和 delta 一起放出来。
        # 新写法：第 1 步的文字**在 tool_call 之前**到达——这正是"边收边发"的证据。
        first_tool_call = kinds.index("tool_call") if "tool_call" in kinds else -1
        deltas_before_tool = kinds[:first_tool_call].count("delta") if first_tool_call > 0 else 0
        check(
            "工具步骤的文字在 tool_call 之前就已经发出",
            deltas_before_tool > 0,
            f"tool_call 之前只看到 {deltas_before_tool} 个 delta：{kinds}",
        )
        check("工具结果仍然在流结束后才产生（不是边读边执行）", "tool_result" in kinds, str(kinds))
        check("两次模型调用（工具结果回灌后又推了一步）", model.calls == 2, f"calls={model.calls}")
        check(
            "最终答案 = 两步文字拼接（与旧写法一致）",
            result.answer == "我先查一下你的薄弱点。你最弱的是索引，建议先补这块。",
            repr(result.answer),
        )
        check("轨迹里记下了那次工具调用", len(result.tool_calls) == 1, str(result.tool_calls))
    finally:
        db.close()

    # ----------------------------------------------------- 思考内容（reasoning_content）
    section("思考内容：单独成事件、按时间铺开、且不污染答案")
    thinking_pieces = 8
    model = SleepingModel(
        [("这是正文。", [])],
        delay=args.delay,
        pieces=2,
        reasoning_pieces=thinking_pieces,
    )
    service = LearningPartnerService(db, llm=model, worker_gateway=NoopGateway(), mcp_tools={})
    result, timed = drive(
        service,
        user_id=1,
        session_id=1,
        request_text="需要思考的问题",
        load_history=False,
        session_model=0,
    )
    kinds = [event["type"] for _, event in timed]
    reasoning = [(t, event["text"]) for t, event in timed if event["type"] == "reasoning"]
    reasoning_text = "".join(text for _, text in reasoning)
    print(f"    事件序列：{kinds}")

    check(
        f"reasoning 事件数 = 思考片数（{len(reasoning)}）",
        len(reasoning) == thinking_pieces,
        f"{len(reasoning)}",
    )
    check(
        "思考事件全部排在正文 delta 之前",
        kinds.index("delta") > kinds.index("reasoning") if "delta" in kinds else False,
        str(kinds),
    )
    check(
        "思考内容**没有**混进最终答案",
        reasoning_text and reasoning_text not in result.answer,
        f"answer={result.answer!r} reasoning={reasoning_text!r}",
    )
    check("答案仍然只有正文", result.answer == "这是正文。", repr(result.answer))
    if len(reasoning) > 1:
        stamps = [t for t, _ in reasoning]
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        check(
            f"思考增量也被时间铺开（平均 {sum(gaps) / len(gaps) * 1000:.1f}ms ≥ {args.delay * 500:.0f}ms）",
            sum(gaps) / len(gaps) >= args.delay * 0.5,
            f"平均 {sum(gaps) / len(gaps) * 1000:.1f}ms",
        )

    # ---------------------------------------------------- 真模型确实开着流式（不联网）
    section("前置条件：真模型工厂没有关掉流式，且保留了 reasoning_content")
    try:
        from app.agent.llm import REASONING_FIELD, get_chat_model

        live_model = get_chat_model(provider="deepseek")
        # `disable_streaming="tool_calling"` 会让"绑了工具的模型"直接退化成一次性返回，
        # 那样这个改动在 agent loop 里完全无效。这里把它钉住。
        check(
            "ChatOpenAI.disable_streaming 是 False（绑工具后仍然真流式）",
            getattr(live_model, "disable_streaming", None) is False,
            f"disable_streaming={getattr(live_model, 'disable_streaming', None)!r}",
        )
        # 原生 ChatOpenAI 会把 reasoning_content 丢掉，所以这里断言的是**我们的子类**。
        check(
            "模型是保留 reasoning_content 的子类",
            type(live_model).__name__ == "ReasoningAwareChatOpenAI",
            f"实际类型 {type(live_model).__name__}",
        )
        synthesised = {"choices": [{"delta": {REASONING_FIELD: "想一段"}}]}
        check(
            "覆写能从原始 chunk 里提取 reasoning_content",
            live_model._convert_chunk_to_generation_chunk(synthesised, AIMessageChunk, None)
            .message.additional_kwargs.get(REASONING_FIELD)
            == "想一段",
            "提取失败",
        )
        plain = {"choices": [{"delta": {"content": "普通"}}]}
        chunk = live_model._convert_chunk_to_generation_chunk(plain, AIMessageChunk, None)
        check(
            "没有 reasoning_content 时不多写字段（不改变既有语义）",
            REASONING_FIELD not in chunk.message.additional_kwargs
            and chunk.message.content == "普通",
            f"additional_kwargs={chunk.message.additional_kwargs}",
        )
    except Exception as exc:  # 没配 key 时不算失败：这只是一条前置断言
        check("模型工厂前置断言", False, f"构造失败：{exc}")

    print(f"\n流式时序断言：{passed} 通过 / {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

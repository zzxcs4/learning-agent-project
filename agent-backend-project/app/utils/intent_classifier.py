"""通用语义意图识别工具。

调用方只需要传入“标签 -> 示例句”即可完成分类，工具本身不绑定长期记忆业务。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import errno
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

# 跨平台文件锁：Windows 用 msvcrt，Linux/macOS 用 fcntl。
# **不能在模块顶层无条件 import msvcrt**：Linux 没有这个模块，容器里
# `import app.main` 会直接 ModuleNotFoundError，整个服务起不来。
if os.name == "nt":  # pragma: no cover - 分支由运行平台决定
    import msvcrt
else:  # pragma: no cover - 分支由运行平台决定
    import fcntl

import numpy as np
from fastembed import TextEmbedding

from app.core.logging_config import main_logger

DEFAULT_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
FASTEMBED_CACHE_DIR = Path(tempfile.gettempdir()) / "fastembed_cache"
MODEL_CACHE_DIR_NAME = "fast-bge-small-zh-v1.5"
MODEL_LOAD_LOCK_FILE = FASTEMBED_CACHE_DIR / "semantic_intent_model.lock"
LOCK_BYTE_COUNT = 1


@contextmanager
def _model_file_lock():
    """跨进程文件锁，避免多个进程同时下载或整理 fastembed 缓存。

    Windows 用 `msvcrt.locking`，Linux/macOS 用 `fcntl.flock`——两者都是
    **建议性锁且随文件描述符关闭自动释放**，语义一致。

    这里刻意用 `os.open(..., O_CREAT | O_RDWR)` 而不是 `Path.open("a+b")`：
    Windows 上的 `a+b` 会让后续 `msvcrt.locking` 的读写位置停留在文件末尾，
    而 Linux 上以追加模式打开又无法用 `flock` 之外的语义表达"锁首字节"。
    统一用原始 fd 打开可写、seek(0) 再加锁，两个平台行为一致。
    """
    FASTEMBED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    main_logger.info("等待语义意图模型跨进程加载锁...")
    fd = os.open(str(MODEL_LOAD_LOCK_FILE), os.O_CREAT | os.O_RDWR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":  # pragma: no cover - 分支由运行平台决定
            msvcrt.locking(fd, msvcrt.LK_LOCK, LOCK_BYTE_COUNT)
        else:  # pragma: no cover - 分支由运行平台决定
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            # 解锁失败不能让业务异常被掩盖：锁最终会随 fd 关闭释放。
            if os.name == "nt":  # pragma: no cover - 分支由运行平台决定
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, LOCK_BYTE_COUNT)
                except OSError as unlock_exc:  # pragma: no cover
                    main_logger.debug("释放 Windows 模型加载锁失败（忽略）：{}", unlock_exc)
            else:  # pragma: no cover - 分支由运行平台决定
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError as unlock_exc:  # pragma: no cover
                    main_logger.debug("释放 POSIX 模型加载锁失败（忽略）：{}", unlock_exc)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class IntentMatch:
    """语义分类结果。"""

    label: str
    score: float


class SemanticIntentClassifier:
    """轻量 Embedding 意图分类器：文本向量和示例句向量做余弦相似度。

    ⚠️ **模型加载失败是"降级"而不是"未命中"**：加载不了时 `classify()` 一律返回
    `default_label`。调用方（如 `OrchestratorService.route`）据此判"语义没说是这个意图"，
    于是整条链路会**静默退化成纯关键词匹配**。这正是踩过的坑：语义这层从来没生效过，
    但日志里只有一行 warning，看起来像"判定结果是聊天"。

    所以这里做了两件事：① 加载失败**有限重试**（首次失败常见于网络抖动/缓存未就绪，
    原先一次失败就永久放弃）；② 重试耗尽后打一条**说明后果**的 warning，让运维知道
    "现在只剩关键词在兜底"，而不是去怀疑自己的关键词表。
    """

    # 加载失败后的最大尝试次数；用尽即进入冷却（避免每条请求都去撞下载）
    MAX_LOAD_ATTEMPTS = 3
    # 冷却时长（秒）：冷却结束后允许再试一次。**不能永久放弃**——首次加载失败常是
    # 网络抖动或模型还没下完，一旦钉死就要重启进程才能恢复语义路由。
    RETRY_COOLDOWN_SECONDS = 300.0

    _instances: dict[str, "SemanticIntentClassifier"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, model_name: str = DEFAULT_MODEL_NAME, **kwargs):
        self.model_name = model_name
        self.kwargs = kwargs
        self._model: TextEmbedding | None = None
        self._load_attempts = 0
        self._load_failed = False
        self._last_failure_at = 0.0
        self._degrade_warned = False
        self._load_lock = threading.Lock()

    @classmethod
    def get(
        cls,
        model_name: str = DEFAULT_MODEL_NAME,
        **kwargs,
    ) -> "SemanticIntentClassifier":
        """按模型配置复用实例，避免重复加载模型。"""
        key = f"{model_name}:{sorted(kwargs.items())}"
        with cls._instances_lock:
            if key not in cls._instances:
                cls._instances[key] = cls(model_name, **kwargs)
            return cls._instances[key]

    def classify(
        self,
        text: str,
        examples: Mapping[str, Sequence[str]],
        threshold: float = 0.55,
        default_label: str = "chat",
    ) -> IntentMatch:
        """返回最相似的标签；低于阈值或模型不可用时返回 default_label。"""
        query = (text or "").strip()
        sentences, labels = self._flatten_examples(examples)
        if not query or not sentences:
            return IntentMatch(default_label, 0.0)

        model = self._get_model()
        if model is None:
            return IntentMatch(default_label, 0.0)

        try:
            # fastembed.embed a single sentence returns a list with one ndarray
            query_vec = list(model.embed(query))[0]
            example_vecs = np.array(list(model.embed(sentences)))

            # l2 norm
            query_vec /= np.linalg.norm(query_vec)
            example_vecs = example_vecs / np.linalg.norm(example_vecs, axis=1, keepdims=True)

            scores = (query_vec @ example_vecs.T)
            best_index = int(np.argmax(scores))
            score = float(scores[best_index])
        except Exception as exc:
            main_logger.warning("语义意图识别失败：{}", exc)
            return IntentMatch(default_label, 0.0)

        label = labels[best_index] if score >= threshold else default_label
        return IntentMatch(label, score)

    def warmup(self) -> None:
        """提前触发懒加载；失败时沿用分类降级逻辑。"""
        self._get_model()

    @staticmethod
    def _flatten_examples(examples: Mapping[str, Sequence[str]]) -> tuple[list[str], list[str]]:
        """把 {label: [examples]} 展平成两个对齐列表。"""
        sentences: list[str] = []
        labels: list[str] = []
        for label, label_examples in examples.items():
            for sentence in label_examples:
                sentences.append(sentence)
                labels.append(label)
        return sentences, labels

    def _get_model(self) -> TextEmbedding | None:
        """懒加载模型；**有限重试 + 冷却**后仍失败则暂时放弃（并说明后果）。

        冷却期内直接返回 None（低成本），冷却结束后**自动再试一次**：这样模型下载
        中途恢复、或网络恢复后，不需要重启进程。
        """
        if self._model is not None:
            return self._model

        with self._load_lock:
            if self._model is not None:
                return self._model

            if self._load_failed:
                if time.monotonic() - self._last_failure_at < self.RETRY_COOLDOWN_SECONDS:
                    return None
                # 冷却结束：允许新一轮尝试
                self._load_failed = False
                self._load_attempts = 0

            self._load_attempts += 1
            try:
                with _model_file_lock():
                    if self._model is not None:
                        return self._model
                    self._model = self._load_model()
                self._load_attempts = 0
                self._degrade_warned = False
            except Exception as exc:
                main_logger.warning(
                    "语义意图模型加载失败（第 {}/{} 次）：{}",
                    self._load_attempts,
                    self.MAX_LOAD_ATTEMPTS,
                    exc,
                )
                if self._load_attempts >= self.MAX_LOAD_ATTEMPTS:
                    self._load_failed = True
                    self._last_failure_at = time.monotonic()
                    self._warn_degraded()
        return self._model

    def _warn_degraded(self) -> None:
        """模型彻底不可用时，把**后果**说清楚（只打一次，避免刷日志）。"""
        if self._degrade_warned:
            return
        self._degrade_warned = True
        main_logger.warning(
            "语义意图识别不可用：模型 {} 连续 {} 次加载失败，"
            "期间所有语义兜底将退化为默认标签（= 路由只剩关键词快筛在起作用），"
            "{} 秒后会自动重试。若长时间如此，请检查模型缓存 {} 与网络（首次需下载模型）。",
            self.model_name,
            self.MAX_LOAD_ATTEMPTS,
            int(self.RETRY_COOLDOWN_SECONDS),
            FASTEMBED_CACHE_DIR,
        )

    def _load_model(self) -> TextEmbedding:
        """加载模型；遇到 Windows 缓存目录冲突时清理后重试一次。"""
        self._clear_stale_tmp_cache()
        try:
            model = TextEmbedding(self.model_name, **self.kwargs)
        except OSError as exc:
            if not self._is_existing_cache_conflict(exc):
                raise
            main_logger.warning("语义意图模型缓存目录冲突，清理 fastembed 缓存后重试：{}", exc)
            self._clear_model_cache()
            model = TextEmbedding(self.model_name, **self.kwargs)
        main_logger.info("语义意图模型加载完成：{} ({})", self.model_name, self.kwargs)
        return model

    @staticmethod
    def _clear_stale_tmp_cache() -> None:
        """清理上次下载中断留下的 tmp 模型目录。"""
        tmp_model_dir = FASTEMBED_CACHE_DIR / "tmp" / MODEL_CACHE_DIR_NAME
        if tmp_model_dir.exists():
            shutil.rmtree(tmp_model_dir, ignore_errors=True)

    @staticmethod
    def _clear_model_cache() -> None:
        """清理冲突的模型缓存目录和临时目录。"""
        shutil.rmtree(FASTEMBED_CACHE_DIR / MODEL_CACHE_DIR_NAME, ignore_errors=True)
        shutil.rmtree(FASTEMBED_CACHE_DIR / "tmp" / MODEL_CACHE_DIR_NAME, ignore_errors=True)

    @staticmethod
    def _is_existing_cache_conflict(exc: OSError) -> bool:
        """判断是否为 Windows 目标目录已存在导致的 fastembed 缓存冲突。"""
        return exc.errno == errno.EEXIST or getattr(exc, "winerror", None) == 183


def classify_intent(
    text: str,
    examples: Mapping[str, Sequence[str]],
    threshold: float = 0.55,
    default_label: str = "chat",
    model_name: str = DEFAULT_MODEL_NAME,
    **kwargs,
) -> IntentMatch:
    """便捷函数：直接传入文本和示例句，返回最相近的标签。"""
    return SemanticIntentClassifier.get(model_name, **kwargs).classify(
        text=text,
        examples=examples,
        threshold=threshold,
        default_label=default_label,
    )

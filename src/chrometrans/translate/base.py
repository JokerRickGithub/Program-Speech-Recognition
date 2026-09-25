"""翻译抽象与降级链（spec §5.4）。"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Protocol

from chrometrans.config import TranslateConfig


class TranslationError(Exception):
    """确定性失败（4xx 等）—— 应当降级到下一个 provider。"""


class TransientTranslationError(TranslationError):
    """传输层抖动（SSL EOF、连接重置）—— 必须重试，不得降级（C19）。"""


class Translator(Protocol):
    name: str

    async def translate(self, texts: list[str], src: str, tgt: str) -> list[str]: ...


def describe_transport_error(exc: Exception) -> str:
    """把传输层异常压成一句能读的话。

    httpx 的 `ConnectError('')` 的 str() **是空串** —— 2026-09-25 的探针里，走代理
    6 次有 2 次就是这个，于是「翻译失败：」后面什么都没有，报错报了个寂寞。
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def retry_transient(fn: Callable[[], Awaitable], max_retries: int = 3,
                          base_delay: float = 0.5):
    """有界重试 + 指数退避。只重试 TransientTranslationError。"""
    delay = base_delay
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except TransientTranslationError as exc:
            last = exc
            if attempt == max_retries - 1:
                break
            if delay:
                await asyncio.sleep(delay)
            delay *= 2
    raise last


class ChainTranslator:
    """按序尝试各 provider；**全失败时抛 TranslationError**。

    抛而不是返回 [None] * len(texts)：那个返回值让「翻译挂了」和「本句无需翻译」
    长得一模一样，而唯一知道原因的这一层把原因吞了 —— 用户报的「英语课很多话直接
    没有翻译」，在终端上一个字都没有（2026-09-25）。

    调用方（engine）把该 cue 的译文照旧存 None、原文照常落盘，用户看到的东西与
    从前一样，只是这回它会说一声是哪个通道、为什么失败。
    """

    def __init__(self, providers: list[Translator], cfg: TranslateConfig):
        self._providers = list(providers)
        self._cfg = cfg

    async def translate(self, texts: list[str], src: str, tgt: str
                        ) -> list[str | None]:
        if not texts:
            return []

        reasons: list[str] = []
        for provider in self._providers:
            try:
                result = await retry_transient(
                    lambda p=provider: p.translate(texts, src, tgt),
                    max_retries=self._cfg.max_retries,
                    base_delay=self._cfg.retry_base_delay_s,
                )
            except Exception as exc:
                reasons.append(f"{provider.name}（{describe_transport_error(exc)}）")
                continue

            if len(result) != len(texts):
                # 序号对不上，宁可换引擎也不错位
                reasons.append(f"{provider.name}（返回 {len(result)} 条，"
                               f"请求 {len(texts)} 条）")
                continue

            return list(result)

        if not reasons:
            raise TranslationError("没有可用的翻译通道（链里一个 provider 都没有）")
        raise TranslationError("所有翻译通道都失败了：" + "；".join(reasons))

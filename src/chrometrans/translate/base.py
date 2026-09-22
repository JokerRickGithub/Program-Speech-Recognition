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
    """按序尝试各 provider。全部失败时该 cue 的译文为 None，原文照常保存。"""

    def __init__(self, providers: list[Translator], cfg: TranslateConfig):
        self._providers = list(providers)
        self._cfg = cfg

    async def translate(self, texts: list[str], src: str, tgt: str
                        ) -> list[str | None]:
        if not texts:
            return []

        for provider in self._providers:
            try:
                result = await retry_transient(
                    lambda p=provider: p.translate(texts, src, tgt),
                    max_retries=self._cfg.max_retries,
                    base_delay=self._cfg.retry_base_delay_s,
                )
            except Exception:
                continue

            if len(result) != len(texts):
                continue                       # 序号对不上，宁可换引擎也不错位

            return list(result)

        return [None] * len(texts)

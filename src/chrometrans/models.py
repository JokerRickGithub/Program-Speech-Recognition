"""跨模块共享的数据模型。"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Cue:
    """一条字幕。整句识别，一经写出即为终态（spec §5.5）。

    单语会话（中文）下 `tgt_lang` 为 None、`target` 为 None；双语会话下
    `tgt_lang` 有值。于是 `target is None` 的两种成因在 JSONL 里可区分：
    **tgt_lang 有值而 target 为空 = 翻译失败；tgt_lang 本身为 None = 本会话不翻译。**
    这就是 C44 在数据层的表达 —— JSONL 是权威文件，它的 schema 是契约。
    """

    id: int
    start: float          # 秒，相对捕获开始
    end: float
    source: str           # 原文
    target: str | None    # 译文；翻译整链失败时为 None
    src_lang: str         # 实际识别的语言，永不为 None
    tgt_lang: str | None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Cue":
        return cls(**d)


def is_monolingual(cue) -> bool:
    """本 cue 是否属于单语会话（不翻译）。

    接受 `Cue` 或它的 dict 形式：事件、JSONL、GUI 三处流动的都是 dict，而
    GUI 的渲染器收的正是 dict。判据只有这一份，四处渲染器据此保持一致（C43）。

    判据是**显式的 None**，不是「键缺失」。`tgt_lang` 是 cue schema 的必填
    字段，缺了说明喂进来的东西有问题 —— 那时退回双语渲染（显示「（未翻译）」），
    而不是当作「本会话不翻译」把译文悄悄吞掉。不安全的方向不该是兜底方向。
    """
    if isinstance(cue, dict):
        return "tgt_lang" in cue and cue["tgt_lang"] is None
    return cue.tgt_lang is None

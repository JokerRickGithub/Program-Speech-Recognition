"""跨模块共享的数据模型。"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Cue:
    """一条双语字幕。整句识别，一经写出即为终态（spec §5.5）。"""

    id: int
    start: float          # 秒，相对捕获开始
    end: float
    source: str           # 原文
    target: str | None    # 译文；翻译整链失败时为 None
    src_lang: str
    tgt_lang: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Cue":
        return cls(**d)

"""悬浮窗的字幕模型。

**不 import Qt**（C36）：上限与顺序是纯逻辑，离线可测。控件那边只管渲染。
"""
from __future__ import annotations

from collections import deque


class Transcript:
    """保留最近 cap 条字幕。

    有上限是必须的：一节 2 小时的课能累积上千条，全留在控件里就是稳定增长的
    内存。上限同时也决定了悬浮窗能往回翻多远。
    """

    def __init__(self, cap: int = 300) -> None:
        if cap < 1:
            raise ValueError(f"cap 必须为正整数，收到 {cap}")
        self._cues: deque[dict] = deque(maxlen=cap)

    def add(self, cue: dict) -> bool:
        """追加一条，返回**是否挤掉了最旧的那条**。

        返回值不是装饰：控件自己那份文档也要跟着裁，只裁模型等于没裁 ——
        QTextEdit 的文档照样无限长。调用方靠这个信号决定要不要重渲。
        """
        full = len(self._cues) == self._cues.maxlen
        self._cues.append(cue)
        return full

    def cues(self) -> list[dict]:
        return list(self._cues)

    def clear(self) -> None:
        self._cues.clear()

    def __len__(self) -> int:
        return len(self._cues)

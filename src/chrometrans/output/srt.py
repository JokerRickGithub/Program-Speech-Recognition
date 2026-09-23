"""SRT 派生视图。每 ~20 cue 重渲一次（spec §5.5）。

只负责**排版**；原子写入在 output/atomic.py（Markdown 视图共用同一套）。
"""
from __future__ import annotations

from chrometrans.models import Cue


def format_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3600 * 1000)
    minutes, rem = divmod(rem, 60 * 1000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _cue_lines(text: str) -> list[str]:
    """把一段文本摊成 cue 内的行，并丢掉空行。

    多行 cue 本身是合法的；非法的是 cue 内的**空行** —— 空行会终止这个 cue，
    SRT 从此错位（C22）。ASR 文本可能自带换行，也可能整段是空白。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return [line for line in normalized.split("\n") if line.strip()]


def render(cues: list[Cue]) -> str:
    """渲染双语 SRT。cue 内绝不出现空行（C22）。"""
    blocks = []
    for cue in cues:
        lines = [str(cue.id),
                 f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}"]
        lines += _cue_lines(cue.source)
        if cue.target:
            lines += _cue_lines(cue.target)
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n\n"

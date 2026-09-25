"""SRT 派生视图。每 ~20 cue 重渲一次（spec §5.5）。

只负责**排版**；原子写入在 output/atomic.py（Markdown 视图共用同一套）。
"""
from __future__ import annotations

from chrometrans.models import Cue, is_monolingual


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
        # 单语会话（tgt_lang is None）不出译文行。**从 cue 自己派生，不另收一个
        # 会话级参数**：JSONL 是权威源，渲染器必须能只凭它重渲（原规格 §7，
        # test_engine.py 的 "can_be_rerendered_from_jsonl_alone" 锁着这一点），
        # 多一个要与数据保持同步的入参就多一处能不同步。
        if cue.target and not is_monolingual(cue):
            lines += _cue_lines(cue.target)
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n\n"

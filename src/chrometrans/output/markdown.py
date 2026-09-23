"""Markdown 派生视图，供课后复习。

与 captions.srt 的分工：SRT 是给播放器跟读用的（cue 编号、毫秒、空行规则），
Markdown 是给人复读用的 —— 所以这里不要 cue 编号、不要毫秒，时间戳只当
对视频进度的锚点。和 SRT 一样是 JSONL 的纯函数，任何历史会话都能随时补渲。
"""
from __future__ import annotations

from chrometrans.models import Cue


def format_clock(seconds: float) -> str:
    """秒 → HH:MM:SS。复习用不到毫秒。"""
    hours, rem = divmod(int(round(seconds)), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _one_line(text: str) -> str:
    """把一段文本压成单行。

    Markdown 里**空行**是段落分隔符：ASR 文本自带换行或空行时若原样保留，
    一条字幕会被劈成两块，上半截没有时间戳，复习时对不上视频进度。
    """
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def render(cues: list[Cue]) -> str:
    """渲染双语 Markdown。

    原文在上、译文在下 —— 与 captions.srt 和屏幕上的顺序保持一致，
    免得同一份内容在三个地方有三种排法。
    """
    if not cues:
        return ""

    head = [
        "# 网课字幕",
        "",
        f"{cues[0].src_lang} → {cues[0].tgt_lang} ｜ {len(cues)} 条 ｜ "
        f"{format_clock(min(c.start for c in cues))} → "
        f"{format_clock(max(c.end for c in cues))}",
        "",
        "---",
    ]

    blocks = []
    for cue in cues:
        # rstrip：原文整段是空白时不要留一个带尾随空格的裸时间戳
        block = f"**{format_clock(cue.start)}** {_one_line(cue.source)}".rstrip()
        target = _one_line(cue.target) if cue.target else ""
        if target:
            block += f"\n\n{target}"
        blocks.append(block)

    return "\n".join(head) + "\n\n" + "\n\n".join(blocks) + "\n"

"""SRT 派生视图。每 ~20 cue 重渲一次（spec §5.5）。"""
from __future__ import annotations

import os
import time
from pathlib import Path

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


def atomic_write(path: Path, text: str, retries: int = 5,
                 base_delay: float = 0.2) -> bool:
    """原子替换，带退避重试（C23）。

    返回 True 表示写入成功；False 表示重试耗尽 —— 调用方应记日志并继续，
    不能阻塞流水线（spec §6）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")

    delay = base_delay
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            if attempt == retries - 1:
                break
            if delay:
                time.sleep(delay)
            delay *= 2

    try:
        tmp.unlink()
    except OSError:
        pass
    return False


def write_with_fallback(path: Path, text: str, retries: int = 5,
                        base_delay: float = 0.2) -> Path | None:
    """先试主目标；被占用则改投 `captions.next.srt`（spec §5.5 / §6）。

    返回实际写入的路径；两处都失败返回 None，由调用方记日志。
    """
    path = Path(path)
    if atomic_write(path, text, retries=retries, base_delay=base_delay):
        return path

    fallback = path.with_name("captions.next.srt")
    if atomic_write(fallback, text, retries=retries, base_delay=base_delay):
        return fallback
    return None

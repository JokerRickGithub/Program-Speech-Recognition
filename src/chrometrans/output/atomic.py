"""派生视图的原子写入（C23）。

不专属任何一个视图：SRT 和 Markdown 都走这里，所以单独成模块。
"""
from __future__ import annotations

import os
import time
from pathlib import Path


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


def fallback_path(path: Path) -> Path:
    """被占用时的改投目标：captions.srt → captions.next.srt。

    从后缀推出来，不写死 `.srt` —— Markdown 视图被占用时同样需要这条退路，
    写死的话它会去覆盖 SRT 的退路文件。
    """
    path = Path(path)
    return path.with_name(f"{path.stem}.next{path.suffix}")


def write_with_fallback(path: Path, text: str, retries: int = 5,
                        base_delay: float = 0.2) -> Path | None:
    """先试主目标；被占用则改投 `captions.next.<后缀>`（spec §5.5 / §6）。

    返回实际写入的路径；两处都失败返回 None，由调用方记日志。
    """
    path = Path(path)
    if atomic_write(path, text, retries=retries, base_delay=base_delay):
        return path

    fallback = fallback_path(path)
    if atomic_write(fallback, text, retries=retries, base_delay=base_delay):
        return fallback
    return None

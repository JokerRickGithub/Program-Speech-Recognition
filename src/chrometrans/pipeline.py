"""音频 → 语音段。CLI 与 GUI 共用。

单独成模块而不是留在 cli.py：它已经不是 CLI 专属的了，而让 gui/ 反过来
import cli.py 是不该有的依赖方向（CLI 是终端前端，不是共享层）。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterator

from chrometrans.audio.segmenter import Segment, Segmenter
from chrometrans.audio.source import CaptureSource
from chrometrans.config import Config, is_bilingual


def _with_session_mode(emit, cfg):
    """给捕获源发出的 running 事件补上会话模式。

    CaptureSource 只拿到 CaptureConfig（见上面 replace 那一步），够不到
    translate.src，而它也会发一条 running（带 pid）—— 那条发在引擎那条之后，
    会把带模式的那行顶掉。三个显示面都按「键缺失就不追加」处理，于是模式在
    稳定状态下是看不见的，而让用户看得出「本会话不翻译」正是这个字段存在的理由
    （spec §5.3 / C44）。

    在这里补、而不是给 CaptureSource 加参数：捕获层不该知道翻译层的事，
    而这里是唯一同时握着 Config 和会话的地方。
    """
    bilingual = is_bilingual(cfg)

    def wrapped(event):
        data = event.get("data")
        if (event.get("event") == "status"
                and isinstance(data, dict)
                and data.get("state") == "running"
                and "bilingual" not in data):
            event = {**event, "data": {**data, "bilingual": bilingual}}
        emit(event)

    return wrapped


def segment_source(cfg: Config, emit: Callable[[dict], None],
                   process_names: tuple[str, ...],
                   resolve_pid: Callable[[], int] | None = None
                   ) -> Callable[[], Iterator[Segment]]:
    """音频 → 语音段。重连与降级由 CaptureSource 负责（spec §5.1 / §6）。

    resolve_pid 为 None 时按 process_names 猜（终端版走这条，行为与从前一致）；
    GUI 会传一个钉住用户所选 PID 的解析器。
    """
    def factory() -> Iterator[Segment]:
        capture_cfg = replace(cfg.capture, process_names=process_names)
        source = CaptureSource(capture_cfg, _with_session_mode(emit, cfg),
                               resolve_pid=resolve_pid)
        segmenter = Segmenter(cfg.segmenter)
        try:
            for chunk in source.chunks():
                if chunk.size:
                    yield from segmenter.feed(chunk)
        finally:
            # 不能在 finally 里 yield：生成器被关闭时 GeneratorExit 会撞上这里的
            # yield → RuntimeError，且下面的 stop() 会被整个跳过。
            source.stop()
        # 只有正常耗尽才排空尾部；Ctrl+C 提前关闭时不会走到这里，
        # 正在说的那半句丢掉是预期行为（它本来就不完整）。
        yield from segmenter.flush()
    return factory

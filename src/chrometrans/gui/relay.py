"""引擎事件 → Qt 信号。

引擎跑在自己的线程里，而 Qt 控件只能在主线程碰（C34）。信号由引擎线程发出、
在主线程接收时，Qt 的自动连接会走队列投递 —— 这是唯一被允许的跨线程通道
（C35），所以这里只做转发，不做判断。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class EventRelay(QObject):
    cue = Signal(dict)
    status = Signal(dict)
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def emit_event(self, event: dict) -> None:
        """引擎线程调用。只转发，不做判断 —— 判断在别的模块里。"""
        kind = event.get("event")
        data = event.get("data") or {}
        if kind == "cue":
            self.cue.emit(data)
        elif kind == "status":
            self.status.emit(data)
        elif kind == "error":
            # 控件上显示的是文字，在这层就把 dict 拍平
            self.error.emit(data.get("message", ""))

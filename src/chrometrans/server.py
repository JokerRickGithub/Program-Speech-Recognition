"""本地显示层：FastAPI + WebSocket + 静态页（spec §5.7）。"""
from __future__ import annotations

import asyncio
import collections
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

STATIC_DIR = Path(__file__).parent / "static"


class EventBus:
    """把引擎事件扇出给所有订阅者，并留一份近期 cue 供后连入的页面补看。"""

    def __init__(self, backlog: int = 50) -> None:
        self._backlog: collections.deque[dict] = collections.deque(maxlen=backlog)
        self._queues: list[asyncio.Queue] = []
        self._last_status: dict | None = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        for event in self._backlog:
            q.put_nowait(event)
        # 只补「最新一条」状态：迟到/刷新的页面需要知道当前状态，
        # 但不需要状态历史（那才会显示成过期状态）
        if self._last_status is not None:
            q.put_nowait(self._last_status)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    async def publish(self, event: dict) -> None:
        # 只有 cue 进 backlog：status/error 是瞬时信号，重放会让刚刷新的页面
        # 显示过期的运行状态
        if event.get("event") == "cue":
            self._backlog.append(event)
        elif event.get("event") == "status":
            self._last_status = event
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass          # 前端跟不上就丢，绝不阻塞流水线


def create_app(bus: EventBus) -> FastAPI:
    app = FastAPI(title="chrometrans")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = bus.subscribe()
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(queue)

    return app

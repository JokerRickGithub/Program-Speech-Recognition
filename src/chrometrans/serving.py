"""本地网页服务：后台线程跑 uvicorn，并提供一个把事件投进去的函数。

CLI 与 GUI 共用这一份实现。各写一遍的话，「跨线程投递」那几行很容易在其中
一边写错 —— 而它写错的表现是「网页永远没有字幕」，非常难查。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Callable


def start_server(bus, host: str, port: int) -> Callable[[dict], None]:
    """起一个后台线程跑 uvicorn，返回「把事件投进去」的函数。"""
    loop: asyncio.AbstractEventLoop | None = None

    def serve() -> None:
        nonlocal loop
        import uvicorn

        from chrometrans.server import create_app

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # loop="asyncio" 阻止 uvicorn 另起一个循环，否则上面的 loop 指向的是
        # 个空转的循环，所有事件都会被投递到没人消费的地方
        config = uvicorn.Config(create_app(bus), host=host, port=port,
                                log_level="warning", loop="asyncio")
        loop.run_until_complete(uvicorn.Server(config).serve())

    threading.Thread(target=serve, daemon=True, name="chrometrans-web").start()

    def publish(event: dict) -> None:
        """引擎跑在主线程、uvicorn 跑在后台线程，**两者的循环不同** —— 必须用
        run_coroutine_threadsafe 跨线程投递。create_task 在这里会静默丢事件。
        """
        if loop is None or not loop.is_running():
            return                       # 服务还没起来，丢弃即可
        try:
            asyncio.run_coroutine_threadsafe(bus.publish(event), loop)
        except RuntimeError:
            pass                         # 循环正在关闭

    return publish

from chrometrans.server import EventBus


def test_publish_drops_events_before_the_server_is_up():
    """uvicorn 还没起来时直接丢弃，绝不阻塞引擎线程。

    回归：这里曾经用 create_task 而不是 run_coroutine_threadsafe 跨线程投递，
    事件被静默丢掉，网页永远没有字幕。
    """
    from chrometrans.serving import start_server

    bus = EventBus()
    # 端口 0 = 让系统分配一个空闲端口，不占用 8765
    publish = start_server(bus, "127.0.0.1", 0)

    publish({"event": "cue", "data": {}})     # 服务还没起来，不应抛异常

    assert bus.subscribe().empty(), "服务没起来时事件必须被丢弃，不能堆积"

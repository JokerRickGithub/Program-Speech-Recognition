import asyncio

from fastapi.testclient import TestClient

from chrometrans.server import EventBus, create_app


def test_index_page_is_served():
    app = create_app(EventBus())
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_websocket_receives_cue_published_before_connect():
    """页面迟到或中途刷新时，应补看到最近的字幕（spec §5.7）。"""
    bus = EventBus()
    asyncio.run(bus.publish({"event": "cue", "data": {"id": 1, "source": "hi"}}))

    with TestClient(create_app(bus)).websocket_connect("/ws") as ws:
        data = ws.receive_json()

    assert data["event"] == "cue"
    assert data["data"]["id"] == 1


def test_backlog_replays_only_the_latest_status():
    """status 不重放历史，但必须补最新一条 —— 否则后开的页面永远显示不出运行状态。"""
    bus = EventBus()
    asyncio.run(bus.publish({"event": "status", "data": {"state": "running"}}))
    asyncio.run(bus.publish({"event": "status", "data": {"state": "stopped"}}))

    q = bus.subscribe()
    assert q.qsize() == 1, "只补最新一条，不重放状态历史"
    assert q.get_nowait()["data"]["state"] == "stopped"


def test_bus_fans_out_to_multiple_subscribers():
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    asyncio.run(bus.publish({"event": "cue", "data": {"id": 7}}))

    assert q1.get_nowait()["data"]["id"] == 7
    assert q2.get_nowait()["data"]["id"] == 7


def test_publish_drops_instead_of_blocking_when_the_queue_is_full():
    """spec §6：前端跟不上就丢，绝不阻塞流水线。"""
    bus = EventBus()

    async def main():
        q = bus.subscribe()
        for _ in range(q.maxsize):
            q.put_nowait({"event": "cue", "data": {}})
        assert q.full()

        await bus.publish({"event": "cue", "data": {"source": "x"}})  # 不得抛

        assert q.full(), "满了就该丢，队列长度不该变"

    asyncio.run(main())


def test_index_page_handles_the_degraded_state():
    """spec §5.1：降级必须能在网页上显示，不能落进「已停止」。"""
    resp = TestClient(create_app(EventBus())).get("/")
    assert "degraded" in resp.text

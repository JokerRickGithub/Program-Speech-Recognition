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


def test_backlog_does_not_replay_status_events():
    """status 是瞬时信号，重放会让刷新后的页面显示过期的运行状态。"""
    bus = EventBus()
    asyncio.run(bus.publish({"event": "status", "data": {"state": "running"}}))
    assert bus.subscribe().empty()


def test_bus_fans_out_to_multiple_subscribers():
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    asyncio.run(bus.publish({"event": "cue", "data": {"id": 7}}))

    assert q1.get_nowait()["data"]["id"] == 7
    assert q2.get_nowait()["data"]["id"] == 7

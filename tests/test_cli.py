import numpy as np
import pytest

from chrometrans.audio.segmenter import Segment
from chrometrans.cli import build_translator_chain, parse_args
from chrometrans.config import Config, TranslateConfig


def test_parse_args_defaults():
    args = parse_args([])
    assert args.host == "127.0.0.1"
    assert args.no_server is False


def test_parse_args_rejects_non_loopback_host():
    """C25：只允许绑回环地址。"""
    with pytest.raises(SystemExit):
        parse_args(["--host", "0.0.0.0"])


def test_free_tier_only_when_no_keys():
    cfg = TranslateConfig(azure_key=None, google_key=None)
    chain = build_translator_chain(cfg)
    assert len(chain._providers) == 1
    assert chain._providers[0].name == "microsoft-free"


def test_full_chain_when_all_keys_present():
    cfg = TranslateConfig(azure_key="A", google_key="G")
    chain = build_translator_chain(cfg)
    assert [p.name for p in chain._providers] == [
        "microsoft-azure", "google", "microsoft-free"]


def test_publish_drops_events_before_the_server_is_up():
    """uvicorn 还没起来时直接丢弃，绝不阻塞引擎线程。"""
    from chrometrans.cli import publish
    from chrometrans.server import EventBus

    bus = EventBus()
    publish(bus, None, {"event": "cue", "data": {}})   # 不应抛异常

    assert bus.subscribe().empty(), "服务没起来时事件必须被丢弃，不能堆积"


def test_segment_source_stops_capture_when_the_generator_is_closed(monkeypatch):
    """Ctrl+C 提前关闭生成器时：不得抛 RuntimeError，且必须 stop() 采集源。"""
    import chrometrans.cli as cli

    stopped = {"v": False}

    class FakeSource:
        def __init__(self, cfg, emit): pass

        def chunks(self):
            while True:
                yield np.full(512, 0.5, dtype=np.float32)

        def stop(self):
            stopped["v"] = True

    class FakeSegmenter:
        def __init__(self, cfg): pass

        def feed(self, chunk):
            return [Segment(1, 0.0, 1.0, np.zeros(16000, dtype=np.float32))]

        def flush(self):
            return [Segment(2, 1.0, 2.0, np.zeros(16000, dtype=np.float32))]

    monkeypatch.setattr(cli, "CaptureSource", FakeSource)
    monkeypatch.setattr(cli, "Segmenter", FakeSegmenter)

    gen = cli.segment_source(Config(), lambda e: None, ("chrome.exe",))()
    next(gen)      # 拿到第一段，生成器挂起
    gen.close()    # 模拟 Ctrl+C / GC 时的提前关闭

    assert stopped["v"], "提前关闭也必须 stop() 采集源"

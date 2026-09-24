import numpy as np

from chrometrans.audio.segmenter import Segment
from chrometrans.config import Config


class FakeSource:
    """记录 CaptureSource 实际收到的构造参数。"""

    last = {}

    def __init__(self, cfg, emit, open_process_stream=None, resolve_pid=None,
                 open_loopback=None):
        FakeSource.last = {"cfg": cfg, "resolve_pid": resolve_pid}

    def chunks(self):
        while True:
            yield np.full(512, 0.5, dtype=np.float32)

    def stop(self):
        FakeSource.last["stopped"] = True


class FakeSegmenter:
    def __init__(self, cfg):
        pass

    def feed(self, chunk):
        return [Segment(1, 0.0, 1.0, np.zeros(16000, dtype=np.float32))]

    def flush(self):
        return [Segment(2, 1.0, 2.0, np.zeros(16000, dtype=np.float32))]


def _patch(monkeypatch):
    import chrometrans.pipeline as pipeline

    monkeypatch.setattr(pipeline, "CaptureSource", FakeSource)
    monkeypatch.setattr(pipeline, "Segmenter", FakeSegmenter)
    FakeSource.last = {}


def test_segment_source_stops_capture_when_the_generator_is_closed(monkeypatch):
    """Ctrl+C 提前关闭生成器时：不得抛 RuntimeError，且必须 stop() 采集源。

    回归：曾经在 finally 里 yield，GeneratorExit 撞上 yield 会抛
    RuntimeError，且 stop() 被整个跳过。
    """
    _patch(monkeypatch)

    from chrometrans.pipeline import segment_source

    gen = segment_source(Config(), lambda e: None, ("chrome.exe",))()
    next(gen)      # 拿到第一段，生成器挂起
    gen.close()    # 模拟 Ctrl+C / GC 时的提前关闭

    assert FakeSource.last.get("stopped"), "提前关闭也必须 stop() 采集源"


def test_process_names_reach_the_capture_config(monkeypatch):
    _patch(monkeypatch)

    from chrometrans.pipeline import segment_source

    gen = segment_source(Config(), lambda e: None, ("wemeetapp.exe",))()
    next(gen)

    assert FakeSource.last["cfg"].process_names == ("wemeetapp.exe",)


def test_resolve_pid_is_passed_through_to_capture_source(monkeypatch):
    """GUI 钉住用户选的 PID 就靠这条通路（C33 的入口）。"""
    _patch(monkeypatch)

    from chrometrans.pipeline import segment_source

    pinned = lambda: 4321                                  # noqa: E731
    gen = segment_source(Config(), lambda e: None, ("chrome.exe",),
                         resolve_pid=pinned)()
    next(gen)

    assert FakeSource.last["resolve_pid"] is pinned


def test_resolve_pid_defaults_to_none_so_cli_behaviour_is_unchanged(monkeypatch):
    """CLI 不传这个参数，行为必须与改动前逐字一致。"""
    _patch(monkeypatch)

    from chrometrans.pipeline import segment_source

    gen = segment_source(Config(), lambda e: None, ("chrome.exe",))()
    next(gen)

    assert FakeSource.last["resolve_pid"] is None

import numpy as np
import pytest

from chrometrans.audio.segmenter import Segment
from chrometrans.cli import build_translator_chain, parse_args, print_event
from chrometrans.config import Config, TranslateConfig


def test_parse_args_defaults():
    args = parse_args([])
    assert args.host == "127.0.0.1"
    assert args.no_server is False


def test_parse_args_rejects_non_loopback_host():
    """C25：只允许绑回环地址。"""
    with pytest.raises(SystemExit):
        parse_args(["--host", "0.0.0.0"])


def test_only_free_tiers_when_no_keys():
    """没 key 也必须有能用的兜底：免 key 谷歌排在前，微软 Edge 那条已下线排最后。"""
    cfg = TranslateConfig(azure_key=None, google_key=None)
    chain = build_translator_chain(cfg)
    assert [p.name for p in chain._providers] == ["google-free", "microsoft-free"]


def test_full_chain_when_all_keys_present():
    cfg = TranslateConfig(azure_key="A", google_key="G")
    chain = build_translator_chain(cfg)
    assert [p.name for p in chain._providers] == [
        "microsoft-azure", "google", "google-free", "microsoft-free"]


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


def test_print_event_reports_every_status_transition(capsys):
    """四个状态都要在终端可见。

    回归：以前只认 error/degraded，于是「启动自检读到静音」（warning）和
    「已停止」在终端上是彻底静默的 —— 用户分不清"在正常工作"和"根本没在录"。
    """
    print_event({"event": "status", "data": {
        "state": "running", "model": "large-v3-turbo", "device": "cuda"}})
    print_event({"event": "status", "data": {
        "state": "running", "capture": "process", "pid": 4321}})
    print_event({"event": "status", "data": {
        "state": "warning", "message": "启动自检未通过（读到的是静音）"}})
    print_event({"event": "status", "data": {
        "state": "degraded", "message": "已降级到系统级捕获：会混入背景音乐"}})
    print_event({"event": "status", "data": {"state": "stopped"}})

    err = capsys.readouterr().err
    assert "运行中" in err and "large-v3-turbo" in err
    assert "PID 4321" in err, "第二种 running 事件要报按进程捕获的 PID"
    assert "启动自检未通过" in err, "warning 以前是静默的"
    assert "已降级到系统级捕获" in err
    assert "已停止" in err


def test_print_event_keeps_stdout_clean_for_cues(capsys):
    """字幕走 stdout、状态走 stderr，好让 `> cues.txt` 拿到干净的流。"""
    print_event({"event": "cue", "data": {
        "start": 1.5, "source": "hello", "target": "你好"}})
    print_event({"event": "status", "data": {"state": "stopped"}})

    out, err = capsys.readouterr()
    assert "hello" in out and "你好" in out
    assert "已停止" not in out, "状态不能污染 stdout"
    assert "已停止" in err

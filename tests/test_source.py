import numpy as np
import pytest

from chrometrans.audio.capture import CaptureError
from chrometrans.audio.source import CaptureSource
from chrometrans.config import CaptureConfig


class FakeStream:
    """可脚本化的假捕获流。"""

    def __init__(self, chunks=(), fail_after=None, raise_on_start=None,
                 raise_on_read=None):
        self._chunks = list(chunks)
        self._fail_after = fail_after
        self._raise_on_start = raise_on_start
        self._raise_on_read = raise_on_read
        self._n = 0
        self.stopped = False

    def start(self):
        if self._raise_on_start:
            raise CaptureError(self._raise_on_start)

    def read(self):
        self._n += 1
        if self._fail_after is not None and self._n > self._fail_after:
            raise CaptureError("pipe broken")
        if self._raise_on_read:
            raise CaptureError(self._raise_on_read)
        return self._chunks[(self._n - 1) % len(self._chunks)]

    def stop(self):
        self.stopped = True


def _source(cfg, events, **kw):
    kw.setdefault("resolve_pid", lambda: 1234)
    kw.setdefault("sleep", lambda _s: None)
    return CaptureSource(cfg, events.append, **kw)


def _take(source, n):
    got = []
    for chunk in source.chunks():
        got.append(chunk)
        if len(got) >= n:
            source.stop()
    return got


def test_check_signal_classifies():
    """spec §9 的启动自检判定。"""
    from chrometrans.audio.source import check_signal

    assert check_signal([], 1e-4) == "no_data"
    assert check_signal([np.zeros(512, dtype=np.float32)], 1e-4) == "silent"
    assert check_signal([np.full(512, 0.05, dtype=np.float32)], 1e-4) == "ok"


def test_self_check_warns_when_the_capture_is_silent():
    """解析到 PID 后先测 RMS，持续静音即报警（spec §9）。

    这是最隐蔽的失败：PID 选错时管道照常工作，只是永远读不到声音。
    """
    events = []
    silent = [np.zeros(512, dtype=np.float32)] * 3
    source = _source(CaptureConfig(self_check_chunks=3), events,
                     open_process_stream=lambda pid: FakeStream(chunks=silent))
    _take(source, 3)

    warned = [e for e in events if e["data"].get("state") == "warning"]
    assert warned, "静音必须告警"
    assert "启动自检未通过" in warned[0]["data"]["message"]


def test_self_check_stays_quiet_on_real_audio():
    events = []
    loud = [np.full(512, 0.05, dtype=np.float32)] * 3
    source = _source(CaptureConfig(self_check_chunks=3), events,
                     open_process_stream=lambda pid: FakeStream(chunks=loud))
    _take(source, 3)

    assert not [e for e in events if e["data"].get("state") == "warning"]


def test_reconnects_when_the_stream_breaks():
    """Chrome 退出 / 管道断开 → 重建流，而不是崩溃（spec §7）。"""
    attempts = {"n": 0}

    def open_stream(pid):
        attempts["n"] += 1
        return FakeStream(chunks=[np.ones(512, dtype=np.float32)],
                          fail_after=2)

    events = []
    source = _source(CaptureConfig(max_reconnect_attempts=5,
                                   self_check_chunks=1), events,
                     open_process_stream=open_stream)
    got = _take(source, 4)

    assert len(got) == 4
    assert attempts["n"] >= 2, "断流后必须重连"
    assert source.mode == "process", "还没到降级阈值，不该降级"


def test_reconnect_backoff_grows_and_is_capped():
    delays = []
    source = _source(CaptureConfig(max_reconnect_attempts=5,
                                   reconnect_base_delay_s=1.0,
                                   reconnect_max_delay_s=4.0),
                     [],
                     open_process_stream=lambda pid: FakeStream(
                         raise_on_start="nope"),
                     open_loopback=lambda: FakeStream(
                         chunks=[np.full(512, 0.5, dtype=np.float32)]),
                     sleep=delays.append)
    _take(source, 1)

    assert delays[:3] == [1.0, 2.0, 4.0]
    assert max(delays) <= 4.0
    assert source.mode == "loopback"


def test_degrades_to_loopback_after_repeated_failures():
    """连续重连失败 → 降级，并显式告知隔离已失效（spec §5.1）。"""
    events = []
    loopback = FakeStream(chunks=[np.full(512, 0.5, dtype=np.float32)])
    source = _source(CaptureConfig(max_reconnect_attempts=2), events,
                     open_process_stream=lambda pid: FakeStream(
                         raise_on_start="nope"),
                     open_loopback=lambda: loopback)
    got = _take(source, 1)

    assert source.mode == "loopback"
    assert got[0][0] == pytest.approx(0.5)

    degraded = [e for e in events if e["data"].get("state") == "degraded"]
    assert degraded, "降级必须发 status 事件"
    assert "降级" in degraded[0]["data"]["message"]


def test_degrades_when_no_process_is_rendering_audio():
    def no_process():
        raise CaptureError("没有进程在渲染音频")

    events = []
    source = _source(CaptureConfig(), events, resolve_pid=no_process,
                     open_loopback=lambda: FakeStream(
                         chunks=[np.full(512, 0.5, dtype=np.float32)]),
                     open_process_stream=lambda pid: FakeStream())
    got = _take(source, 1)

    assert source.mode == "loopback"
    assert got


def test_loopback_fills_silence_instead_of_stalling():
    """C1 / spec §6：底层停流时补静音帧，切句器时序不卡死。"""
    never = lambda: FakeStream(raise_on_read="no data")
    events = []
    source = _source(CaptureConfig(max_reconnect_attempts=1), events,
                     open_process_stream=lambda pid: FakeStream(
                         raise_on_read="no data"),
                     open_loopback=never)
    got = _take(source, 3)

    assert len(got) == 3
    assert all(np.array_equal(c, np.zeros(512, dtype=np.float32)) for c in got)


def test_stop_is_idempotent():
    source = _source(CaptureConfig(), [], open_process_stream=lambda pid: FakeStream())
    source.stop()
    source.stop()          # 不应抛异常

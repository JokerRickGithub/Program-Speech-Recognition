import threading
import time
from concurrent.futures import ThreadPoolExecutor

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


def test_loopback_start_failure_releases_the_stream():
    """启动失败也必须 stop()，否则每次重试泄漏一个 PortAudio 实例。"""
    def no_process():
        raise CaptureError("没有进程在渲染音频")

    broken = FakeStream(raise_on_start="设备被占用")
    calls = {"n": 0}

    def open_loopback():
        calls["n"] += 1
        if calls["n"] == 1:
            return broken
        return FakeStream(chunks=[np.full(512, 0.5, dtype=np.float32)])

    source = _source(CaptureConfig(), [], resolve_pid=no_process,
                     open_loopback=open_loopback,
                     open_process_stream=lambda pid: FakeStream())
    got = _take(source, 1)

    assert calls["n"] >= 2, "启动失败后必须重试"
    assert broken.stopped, "启动失败的那条流必须被 stop()，否则句柄泄漏"
    assert got


def test_pid_is_re_resolved_on_every_reconnect():
    """Chrome 重启会换 PID —— 每次重连都必须重新解析（C26）。"""
    calls = []

    def resolve():
        calls.append(1)
        return 1234 if len(calls) == 1 else 5678

    events = []
    source = _source(
        CaptureConfig(max_reconnect_attempts=5, self_check_chunks=1), events,
        resolve_pid=resolve,
        open_process_stream=lambda pid: FakeStream(
            chunks=[np.full(512, 0.5, dtype=np.float32)], fail_after=2),
        open_loopback=lambda: FakeStream(
            chunks=[np.full(512, 0.5, dtype=np.float32)]))
    _take(source, 3)

    running = [e["data"]["pid"] for e in events
               if e["data"].get("state") == "running"]
    assert running == [1234, 5678], "重连后必须按新 PID 重新解析"


def test_loopback_read_timeout_raises_so_the_caller_can_fill_silence():
    """C1：PortAudio 永久阻塞必须有超时，把控制权拿回来才能补静音。"""
    import threading
    from chrometrans.audio.capture import AudioFormat, PcmConverter
    from chrometrans.audio.source import SystemLoopbackStream

    release = threading.Event()

    class BlockingPortAudioStream:
        def read(self, n, exception_on_overflow=False):
            release.wait(5)                 # 模拟 PortAudio 永久阻塞
            return b"\x00" * 4096

    s = SystemLoopbackStream(CaptureConfig(loopback_read_timeout_s=0.05))
    s._stream = BlockingPortAudioStream()
    s._converter = PcmConverter(AudioFormat(2, 48000, 32), 16000)
    s._pool = ThreadPoolExecutor(max_workers=1)     # 或调用 s.start() 的分支

    try:
        with pytest.raises(CaptureError):
            s.read()
    finally:
        release.set()


def test_loopback_read_returns_each_chunk_exactly_once():
    """回归：成功取回后必须清 _pending，否则同一块数据会被返回两次。

    旧实现只在「上一块还没读完」的分支里清 _pending；成功路径不清，于是第二次
    read() 会命中 `fut is not None and fut.done()`，把 fut.result() 再交一遍。
    降级路上的音频流会变成 A, A, B, B…，识别文本随之整段重复。
    """
    from chrometrans.audio.capture import AudioFormat, PcmConverter
    from chrometrans.audio.source import SystemLoopbackStream

    class CountingStream:
        def __init__(self):
            self.n = 0

        def read(self, n, exception_on_overflow=False):
            self.n += 1
            # 每次给可区分的电平，好让"返回了同一块"这件事在数据上也看得出来
            return np.full(2048, float(self.n), dtype=np.float32).tobytes()

    s = SystemLoopbackStream(CaptureConfig(loopback_read_timeout_s=5.0))
    s._stream = CountingStream()
    s._converter = PcmConverter(AudioFormat(2, 48000, 32), 16000)
    s._pool = ThreadPoolExecutor(max_workers=1)

    try:
        first = s.read()
        second = s.read()
        assert s._stream.n == 2, "第二次 read() 必须真的再读一次，不能复用上一块"
        assert not np.array_equal(first, second), "两次读回的数据不应相同"
    finally:
        s._pool.shutdown(wait=False)


def test_loopback_read_normalizes_converter_failure_to_capture_error():
    """read() 对外只承诺抛 CaptureError，不能漏出 soxr/numpy 的原始异常。"""
    from chrometrans.audio.source import SystemLoopbackStream

    class ExplodingConverter:
        def convert(self, data):
            raise ValueError("soxr 参数不合法")

    class OneChunkStream:
        def read(self, n, exception_on_overflow=False):
            return np.zeros(2048, dtype=np.float32).tobytes()

    s = SystemLoopbackStream(CaptureConfig(loopback_read_timeout_s=5.0))
    s._stream = OneChunkStream()
    s._converter = ExplodingConverter()
    s._pool = ThreadPoolExecutor(max_workers=1)

    try:
        with pytest.raises(CaptureError):
            s.read()
    finally:
        s._pool.shutdown(wait=False)


# ---- 排空层：消费者卡住时不得把音频憋在管道里（2026-09-25 实测的丢音频根因）----


class PacedStream:
    """按自己的节奏产块的假流，并记下底层被读了多少次。"""

    def __init__(self, delay=0.002, samples=160):
        self.reads = 0
        self._delay = delay
        self._samples = samples
        self.stopped = False

    def start(self):
        pass

    def read(self):
        self.reads += 1
        time.sleep(self._delay)
        return np.full(self._samples, 0.05, dtype=np.float32)

    def stop(self):
        self.stopped = True


def test_a_stalled_consumer_leaves_the_pipe_drained():
    """回归：消费者卡住时底层流必须仍在被读，否则管道填满就丢音频。

    整条链是拉取式的单线程生成器（engine.run → Segmenter → chunks →
    stream.read）。翻译卡顿实测 40 次里 16 次超过 1 秒、最坏 17.16 秒，而命名
    管道缓冲 1 MB 只装得下 16 kHz×float32 的 16.4 秒音频 —— 越过这条线 DLL 只能
    丢样本，「翻译慢」于是升级成「整句话没了」。
    """
    stream = PacedStream()
    source = _source(CaptureConfig(self_check_chunks=1), [],
                     open_process_stream=lambda pid: stream)
    it = source.chunks()
    next(it)                      # 自检窗口，先拿到第一块
    before = stream.reads

    time.sleep(0.3)               # 模拟翻译卡住
    during_stall = stream.reads - before
    source.stop()
    it.close()

    assert during_stall > 20, (
        f"消费者卡住的 0.3 秒里底层只被读了 {during_stall} 次 —— 管道没人在排空")


def test_buffer_overflow_drops_the_oldest_and_reports_it():
    """缓冲必须有界，溢出时丢最旧的并**说出来**。

    丢最旧是为了让用户跟得上「现在」这节课；而丢这件事必须可见 —— 与 C45 要求
    丢弃幻觉可见同一个取向：静默少掉几句字幕，事后无从查起。
    """
    events = []

    class Firehose:
        def __init__(self):
            self.n = 0

        def start(self):
            pass

        def read(self):
            self.n += 1
            return np.full(160, float(self.n), dtype=np.float32)

        def stop(self):
            pass

    fire = Firehose()
    # 容量 0.1 秒 = 1600 样本 = 10 块，而生产者随手就能产几百块
    source = _source(CaptureConfig(self_check_chunks=1,
                                   drain_buffer_seconds=0.1), events,
                     open_process_stream=lambda pid: fire)
    it = source.chunks()
    next(it)
    time.sleep(0.2)
    next(it)                      # 恢复消费：此刻才报出溢出

    assert fire.n > 10, "生产必须超过容量，否则这条测试没测到溢出"
    lost = [e for e in events if e["data"].get("state") == "audio_dropped"]
    assert lost, "丢音频必须发 status 事件"
    assert "丢弃" in lost[0]["data"]["message"]

    source.stop()
    it.close()


def test_stop_unblocks_a_consumer_waiting_on_a_starved_stream():
    """静音期底层不产数据时，停止必须立刻生效。

    这就是实测的「停止挂起」：消费者等在阻塞读里，Ctrl+C 之后没有任何东西去
    打断那次读，进程只能干等下一次读到东西。而且停止不是断流 —— 不许报成
    「捕获流中断，正在重连」。
    """
    gate = threading.Event()
    events = []

    class Starved:
        def __init__(self):
            self.n = 0

        def start(self):
            pass

        def read(self):
            self.n += 1
            if self.n == 1:
                return np.full(160, 0.05, dtype=np.float32)
            gate.wait(10)         # 一直没数据
            return np.zeros(160, dtype=np.float32)

        def stop(self):
            gate.set()            # 等价于 CancelIoEx：把阻塞中的读放掉

    source = _source(CaptureConfig(self_check_chunks=1), events,
                     open_process_stream=lambda pid: Starved())
    done = threading.Event()

    def consume():
        for _ in source.chunks():
            pass
        done.set()

    threading.Thread(target=consume, daemon=True).start()
    time.sleep(0.2)
    assert not done.is_set(), "前提：消费者此刻正卡在阻塞读里"

    source.stop()
    assert done.wait(3.0), "stop() 之后消费者必须马上退出"
    assert not [e for e in events if e["event"] == "error"], \
        "用户主动停止不该报成捕获失败"


def test_a_clean_stop_does_not_back_off():
    """正常停止不是断流：不该为它睡退避。

    旧行为是停止后仍走一次 _maybe_degrade()：failures=0 时它算出的 1 秒退避
    照睡不误 —— Ctrl+C 之后进程还要多赖一秒。
    """
    delays = []
    source = _source(CaptureConfig(self_check_chunks=1), [],
                     open_process_stream=lambda pid: FakeStream(
                         chunks=[np.full(512, 0.05, dtype=np.float32)]),
                     sleep=delays.append)
    _take(source, 2)

    assert delays == [], f"正常停止不该退避，实际睡了 {delays}"


def test_loopback_path_drains_too():
    """降级路径同样要排空 —— 它照样是拉取式的，消费者卡住一样丢音频。"""

    def no_process():
        raise CaptureError("没有进程在渲染音频")

    loop = PacedStream()
    source = _source(CaptureConfig(self_check_chunks=1), [],
                     resolve_pid=no_process,
                     open_loopback=lambda: loop,
                     open_process_stream=lambda pid: FakeStream())
    it = source.chunks()
    next(it)
    before = loop.reads

    time.sleep(0.3)
    during_stall = loop.reads - before
    source.stop()
    it.close()

    assert during_stall > 20, (
        f"降级路径在消费者卡住的 0.3 秒里只被读了 {during_stall} 次")

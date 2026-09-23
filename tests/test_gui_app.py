import os
import threading
import time

import pytest

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def relay(qapp):
    from chrometrans.gui.relay import EventRelay

    return EventRelay()


class FakeEngine:
    """记录 run/stop 的调用，可控制 run 什么时候返回。"""

    def __init__(self, release: threading.Event | None = None):
        self.started = threading.Event()
        self.stopped = False
        self.run_thread: str | None = None
        self._release = release

    def run(self):
        self.run_thread = threading.current_thread().name
        self.started.set()
        if self._release is not None:
            self._release.wait(5)

    def stop(self):
        self.stopped = True


def test_build_engine_pins_the_chosen_pid(monkeypatch):
    """GUI 存在的全部理由：把用户选的那个 PID 钉进去，而不是退回去按名字猜。"""
    import chrometrans.pipeline as pipeline
    from chrometrans.config import Config
    from chrometrans.gui.app import build_engine
    from chrometrans.gui.processes import AudioProcess

    seen = {}

    def fake_segment_source(cfg, emit, process_names, resolve_pid=None):
        seen["names"] = process_names
        seen["resolve"] = resolve_pid
        return lambda: iter(())

    monkeypatch.setattr(pipeline, "segment_source", fake_segment_source)

    build_engine(Config(), lambda e: None, AudioProcess(4321, "chrome.exe"),
                 asr=object(), translator=object())

    assert seen["names"] == ("chrome.exe",)
    assert seen["resolve"] is not None, \
        "没注入 resolve_pid 就只是又按名字猜了一遍 —— 这正是 GUI 要消灭的行为"


def test_the_chosen_process_reaches_the_engine_factory(qapp, relay):
    from chrometrans.gui.app import CaptureController
    from chrometrans.gui.processes import AudioProcess

    engine = FakeEngine()
    seen = {}

    def factory(cfg, emit, proc):
        seen["proc"] = proc
        return engine

    c = CaptureController(relay, engine_factory=factory)
    chosen = AudioProcess(4321, "chrome.exe")
    c.start(chosen)
    assert engine.started.wait(2)

    assert seen["proc"] == chosen
    c.stop_and_wait(timeout_s=2)


def test_capture_runs_the_engine_off_the_main_thread(qapp, relay):
    """C34：engine.run() 是阻塞的，放主线程界面必然卡死。"""
    from chrometrans.gui.app import CaptureController

    engine = FakeEngine()
    main_thread = threading.current_thread().name

    c = CaptureController(relay, engine_factory=lambda cfg, emit, proc: engine)
    c.start(object())
    assert engine.started.wait(2), "引擎线程没跑起来"
    c.stop_and_wait(timeout_s=2)

    assert engine.run_thread != main_thread, "engine.run() 必须跑在主线程之外"


def test_stop_signals_the_engine_and_reports_back(qapp, relay):
    from PySide6.QtWidgets import QApplication
    from chrometrans.gui.app import CaptureController

    engine = FakeEngine()
    c = CaptureController(relay, engine_factory=lambda cfg, emit, proc: engine)
    finished = []
    c.finished.connect(lambda: finished.append(True))

    c.start(object())
    c.stop_and_wait(timeout_s=5)
    # finished 由引擎线程发出、队列投递到主线程（C34/C35）—— 事件循环转一圈才送达
    QApplication.processEvents()

    assert engine.stopped
    assert finished, "停止后必须发 finished，界面才能切回未捕获状态"


def test_a_hung_engine_does_not_block_stop_forever(qapp, relay):
    """引擎不收敛时放弃等待并提示，不能让界面永久卡在「正在停止」。"""
    from chrometrans.gui.app import CaptureController

    stuck = threading.Event()          # 永远不 set
    engine = FakeEngine(release=stuck)
    c = CaptureController(relay, engine_factory=lambda cfg, emit, proc: engine)
    errors = []
    relay.error.connect(errors.append)

    c.start(object())
    assert engine.started.wait(2)

    began = time.monotonic()
    c.stop_and_wait(timeout_s=0.3)
    elapsed = time.monotonic() - began

    assert elapsed < 3, "等待必须有上限"
    assert any("没有停下" in e or "放弃等待" in e for e in errors), \
        "要明确告知没收敛"

    stuck.set()                      # 放掉，免得线程泄漏到别的用例
    time.sleep(0.05)


def test_starting_twice_is_refused_rather_than_spawning_a_second_engine(qapp, relay):
    """两份实例会撞在同一条命名管道上。"""
    from chrometrans.gui.app import CaptureController

    # 必须把第一台引擎**按住在运行中**。FakeEngine 默认 run() 立刻返回，
    # 那样第一次捕获可能已经结束，running() 就是 False，第二次 start()
    # 自然不会被拒绝 —— 这个用例会变成看运气的。
    hold = threading.Event()
    engine = FakeEngine(release=hold)
    c = CaptureController(relay, engine_factory=lambda cfg, emit, proc: engine)
    errors = []
    relay.error.connect(errors.append)

    c.start(object())
    assert engine.started.wait(2)
    c.start(object())

    assert errors, "第二次启动要被拒绝并说明原因"
    hold.set()
    c.stop_and_wait(timeout_s=2)


def test_loopback_hosts_are_still_the_only_allowed_ones():
    """C25：GUI 不得放宽回环限制 —— 直接复用 cli 的校验。"""
    from chrometrans.cli import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--host", "0.0.0.0"])

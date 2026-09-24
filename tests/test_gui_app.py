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


def test_a_timed_out_stop_returns_the_ui_to_uncaptured_but_keeps_the_engine_visible(
        qapp, relay):
    """放弃等待不等于假装它不存在：界面切回未捕获，但 running() 仍是 True ——
    两份实例会撞在同一条命名管道上。"""
    from chrometrans.gui.app import CaptureController

    stuck = threading.Event()           # 永远不 set
    engine = FakeEngine(release=stuck)
    c = CaptureController(relay, engine_factory=lambda cfg, emit, proc: engine)
    finished = []
    errors = []
    c.finished.connect(lambda: finished.append(True))
    relay.error.connect(errors.append)

    c.start(object())
    assert engine.started.wait(2)

    c.stop_and_wait(timeout_s=0.3)

    # 这个 emit 在主线程上（stop_and_wait 就在主线程里跑），所以是直连、立即到达 ——
    # 不用 processEvents()。若谁把它挪进引擎线程，这里就会失败，那正是要拦的。
    assert finished, "超时放弃后界面必须能切回未捕获状态"
    assert c.running(), "线程还活着，running() 不能撒谎"

    before = len(errors)
    c.start(object())
    assert len(errors) > before, "上一台还在跑，第二次启动要被拒绝"

    # 别把卡住的线程留给别的用例
    stuck.set()
    c.stop_and_wait(timeout_s=2)        # 已经放弃过，这次立刻返回


def test_a_second_stop_after_a_timeout_does_not_wait_again(qapp, relay):
    """放弃过的引擎不必再等一遍 —— 那只会把界面再冻满一个超时。

    这条在修复前也会通过（引用已经被清成 None，第二次直接返回）。它是为了锁住
    修复后的行为：谁要是保留了引用却忘了短路，界面就会再冻一次。
    """
    from chrometrans.gui.app import CaptureController

    stuck = threading.Event()
    engine = FakeEngine(release=stuck)
    c = CaptureController(relay, engine_factory=lambda cfg, emit, proc: engine)

    c.start(object())
    assert engine.started.wait(2)

    c.stop_and_wait(timeout_s=0.3)
    began = time.monotonic()
    c.stop_and_wait(timeout_s=0.3)
    elapsed = time.monotonic() - began

    assert elapsed < 0.15, "第二次不该再等一个 0.3 秒的超时"

    stuck.set()
    time.sleep(0.05)


def test_the_page_server_is_started_at_most_once():
    """端口被第一份实例一直占着（它没有停止接口），再起一份只会得到绑不上端口的
    空壳，事件全被它吞掉 —— 页面看着在，却永远不更新，还不报错。"""
    from chrometrans.gui.app import should_start_page_server

    assert should_start_page_server(no_server=False, enabled=True, started=False)
    assert not should_start_page_server(no_server=False, enabled=True, started=True)
    assert not should_start_page_server(no_server=False, enabled=False, started=False)
    assert not should_start_page_server(no_server=True, enabled=True, started=False)


def test_startup_path_runs_to_completion(qapp, tmp_path, monkeypatch):
    """main() 的整条启动路径：建窗口、建托盘、接信号、起事件循环，正常返回 0。

    这一行之前没有任何测试覆盖，`_screens()` 里 QScreen 没有 x()/y()/宽高
    的错误才会一路活到合并、在用户第一次真跑时崩掉。重复定时器退循环而不是
    singleShot(0)：后者万一在 exec() 前就被某次 processEvents 消化，main()
    后面的 app.exec() 会永远阻塞，测试就挂死在那儿。
    """
    from PySide6.QtCore import QTimer
    from chrometrans.gui import app as gui_app
    from chrometrans.gui import settings as gui_settings

    # 别去读写这台机器上真实的 %APPDATA%/chrometrans/gui.json
    monkeypatch.setattr(gui_settings, "settings_path",
                        lambda: tmp_path / "gui.json")

    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(qapp.quit)
    timer.start()
    try:
        assert gui_app.main([]) == 0
    finally:
        timer.stop()


def test_dropped_status_reaches_the_caption_window(qapp):
    """C45 的最后一公里：事件产生在 engine 里，而用户看的是字幕窗。

    断在这一行，前面所有用例照样全绿，而用户什么也看不到。
    """
    from chrometrans.gui.app import dispatch_status
    from chrometrans.gui.caption_window import CaptionWindow
    from chrometrans.gui.launcher import LauncherWindow

    caption = CaptionWindow()
    launcher = LauncherWindow()

    dispatch_status({"state": "dropped", "message": "丢弃疑似幻觉：谢谢观看"},
                    launcher=launcher, caption=caption)

    assert "丢弃疑似幻觉：谢谢观看" in caption.toPlainText()
    assert "丢弃疑似幻觉：谢谢观看" in launcher.status_text()
    caption.close()
    launcher.close()


def test_ordinary_statuses_still_reach_the_launcher(qapp):
    """回归：提到模块级不能把原有那五个状态接丢。"""
    from chrometrans.gui.app import dispatch_status
    from chrometrans.gui.caption_window import CaptionWindow
    from chrometrans.gui.launcher import LauncherWindow

    caption = CaptionWindow()
    launcher = LauncherWindow()

    dispatch_status({"state": "loading", "message": "正在载入"},
                    launcher=launcher, caption=caption)
    assert launcher.status_text() == "正在载入"

    dispatch_status({"state": "running", "model": "large-v3-turbo",
                     "device": "cuda"}, launcher=launcher, caption=caption)
    assert launcher.status_text() == "运行中 · large-v3-turbo · cuda"

    dispatch_status({"state": "warning", "message": "内存告急"},
                    launcher=launcher, caption=caption)
    assert launcher.status_text() == "内存告急"

    dispatch_status({"state": "degraded", "message": "降级了"},
                    launcher=launcher, caption=caption)
    assert launcher.status_text() == "降级了"

    dispatch_status({"state": "stopped"}, launcher=launcher, caption=caption)
    assert launcher.status_text() == "已停止"

    caption.close()
    launcher.close()


def test_gui_running_line_locks_the_session_mode_suffix(qapp):
    """C43：bilingual 派生出的后缀 —— 锁定 GUI 的整行。

    断言整行相等而非子串 —— 中间点、空格、字符错一个都是 C43 违约，
    子串检查根本发现不了。CLI 的等价整行锁在 tests/test_cli.py 的
    test_running_line_reports_the_session_mode，网页的锁在
    tests/test_static_index.py 的 test_the_running_line_reports_the_session_mode；
    此处只锁 GUI 这一行。
    """
    from chrometrans.gui.app import dispatch_status
    from chrometrans.gui.caption_window import CaptionWindow
    from chrometrans.gui.launcher import LauncherWindow

    caption = CaptionWindow()
    launcher = LauncherWindow()

    dispatch_status({"state": "running", "model": "large-v3-turbo",
                     "device": "cuda", "bilingual": True},
                    launcher=launcher, caption=caption)
    assert launcher.status_text() == "运行中 · large-v3-turbo · cuda · 翻译中"

    dispatch_status({"state": "running", "model": "large-v3-turbo",
                     "device": "cuda", "bilingual": False},
                    launcher=launcher, caption=caption)
    assert launcher.status_text() == "运行中 · large-v3-turbo · cuda · 不翻译"

    dispatch_status({"state": "running", "model": "large-v3-turbo",
                     "device": "cuda"},
                    launcher=launcher, caption=caption)
    assert launcher.status_text() == "运行中 · large-v3-turbo · cuda"

    dispatch_status({"state": "running", "pid": 123, "bilingual": False},
                    launcher=launcher, caption=caption)
    assert launcher.status_text() == "运行中 · 按进程捕获（PID 123） · 不翻译"

    caption.close()
    launcher.close()


def test_startup_restores_the_saved_language(qapp, tmp_path, monkeypatch):
    """持久化的语言要真的回到下拉里 —— 存了不读等于没存。

    照 test_startup_path_runs_to_completion 的写法驱动整条启动路径（同一个
    settings_path 补丁、同一个「用重复定时器退循环」的退出口）。
    """
    import json

    from PySide6.QtCore import QTimer
    from chrometrans.gui import app as gui_app
    from chrometrans.gui import settings as gui_settings
    from chrometrans.gui.launcher import LauncherWindow

    path = tmp_path / "gui.json"
    path.write_text(json.dumps({"language": "zh"}), encoding="utf-8")
    monkeypatch.setattr(gui_settings, "settings_path", lambda: path)

    # 只验「main 把存下来的语言交给了下拉」，所以把 set_language 换成探针；
    # 下拉本身选不选得中由 test_gui_launcher.py 那几条管。
    seen = {}
    monkeypatch.setattr(LauncherWindow, "set_language",
                        lambda self, code: seen.update(code=code))

    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(qapp.quit)
    timer.start()
    try:
        assert gui_app.main([]) == 0
    finally:
        timer.stop()

    assert seen.get("code") == "zh", "启动时必须把存下来的语言放回下拉"


def test_persist_settings_writes_the_selected_language(monkeypatch, tmp_path):
    """「存」的那一端 —— 「读」的那端由 test_startup_restores_the_saved_language 管。

    两端分开测，因为整条路径在测试里跑不到 persist()：退循环靠 qapp.quit()，
    而 quit 不走 on_stop/on_quit。
    """
    import json

    from chrometrans.gui import app as gui_app
    from chrometrans.gui import settings as gui_settings
    from chrometrans.gui.settings import GuiSettings

    path = tmp_path / "gui.json"
    monkeypatch.setattr(gui_settings, "settings_path", lambda: path)

    class FakeCaption:
        def current_settings(self):
            return GuiSettings(width=1234)

    class FakeLauncher:
        def selected_language(self):
            return "zh"

    gui_app.persist_settings(FakeCaption(), FakeLauncher())

    assert json.loads(path.read_text(encoding="utf-8"))["language"] == "zh"

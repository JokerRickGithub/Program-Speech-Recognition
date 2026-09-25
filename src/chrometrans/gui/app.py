"""图形界面入口：接线、托盘、引擎线程。

这里只做接线（C36）—— 判断都在别的模块里。
"""
from __future__ import annotations

import sys
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from chrometrans.cli import keyless_notice, parse_args
from chrometrans.config import load_config
from chrometrans.gui.caption_window import CaptionWindow
from chrometrans.gui.launcher import LauncherWindow
from chrometrans.gui.processes import make_resolver
from chrometrans.gui.relay import EventRelay
from chrometrans.gui.settings import load as load_settings
from chrometrans.gui.settings import restore_position, save as save_settings
from chrometrans.gui.settings import with_language

STOP_TIMEOUT_S = 10.0


def build_engine(cfg, emit, proc, *, asr, translator):
    """按用户选中的进程装配一台 Engine。

    proc 必须一路走到 resolve_pid —— 这是 GUI 存在的全部理由：钉住用户选的
    那个 PID，而不是让 find_target_pid 在同名进程里试听挑最响的去猜。
    """
    from chrometrans.engine import Engine
    from chrometrans.pipeline import segment_source

    return Engine(
        cfg=cfg, on_event=emit, asr=asr, translator=translator, segments=(),
        segment_iter_factory=segment_source(
            cfg, emit, (proc.name,), resolve_pid=make_resolver(proc)))


class CaptureController(QObject):
    """管住那条引擎线程。一次只允许一个。"""

    finished = Signal()

    def __init__(self, relay: EventRelay, engine_factory=None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._relay = relay
        self._engine_factory = engine_factory or self._default_factory
        self._thread: threading.Thread | None = None
        self._engine = None
        self._abandoned = False

    def _default_factory(self, cfg, emit, proc):
        # 延迟 import：torch 那一串只在真要跑的时候才拉进来，否则光是打开
        # 启动器窗口就要等十几秒
        from chrometrans.asr.whisper_engine import WhisperEngine
        from chrometrans.cli import build_translator_chain

        return build_engine(cfg, emit, proc, asr=WhisperEngine(cfg.asr),
                            translator=build_translator_chain(cfg.translate))

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, proc, cfg=None, emit=None) -> None:
        if self.running():
            # 两份实例会撞在同一条命名管道上（pipe_name 是写死的）
            self._relay.error.emit("已经在捕获了，不能重复启动")
            return
        cfg = cfg or load_config()
        engine = self._engine_factory(cfg, emit or self._relay.emit_event, proc)

        def body() -> None:
            try:
                engine.run()
            except Exception as exc:
                self._relay.error.emit(f"捕获异常结束：{exc}")
            finally:
                self._abandoned = False
                self.finished.emit()

        self._engine = engine
        self._thread = threading.Thread(target=body, name="chrometrans-engine",
                                        daemon=True)
        self._thread.start()

    def stop_and_wait(self, timeout_s: float = STOP_TIMEOUT_S) -> None:
        """stop() 之后等待有上限。

        引擎卡住时不能让界面永久停在「正在停止」。线程是守护线程，进程退出
        不会被它挂住，所以放弃等待是安全的。
        """
        if self._abandoned:
            # 已经放弃过的引擎不必再等一遍 —— 那只会把界面再冻满一个超时
            return
        engine, thread = self._engine, self._thread
        if engine is None or thread is None:
            return
        engine.stop()
        thread.join(timeout_s)
        if thread.is_alive():
            # 放弃等待，但不能假装这台引擎不存在：线程还活着，running() 就得
            # 照实说是 True，否则第二台引擎会撞在同一条命名管道上。界面先切回
            # 未捕获，让用户看清出了什么事。
            self._abandoned = True
            self._relay.error.emit(
                f"引擎在 {timeout_s:.0f} 秒内没有停下，已放弃等待；"
                f"它不会再写入字幕，进程退出时会被一并结束")
            self.finished.emit()
            return
        self._engine = None
        self._thread = None


def _tray_icon() -> QIcon:
    """画一个图标，免得为了一个托盘图标去打包资源文件。"""
    pix = QPixmap(32, 32)
    pix.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#3b6fd4"))
    painter.drawRoundedRect(3, 9, 26, 14, 4, 4)
    painter.end()
    return QIcon(pix)


def _screens() -> list[tuple[int, int, int, int]]:
    from PySide6.QtGui import QGuiApplication

    out = []
    for screen in QGuiApplication.screens():
        r = screen.geometry()
        out.append((r.x(), r.y(), r.width(), r.height()))
    return out


def should_start_page_server(*, no_server: bool, enabled: bool,
                             started: bool) -> bool:
    """网页服务最多起一次。

    端口被第一份实例一直占着（R17：它没有停止接口），再起一份只会得到一个
    绑不上端口的空壳，事件全被它吞掉 —— 页面看着在，却永远不更新，还不报错。
    """
    return not no_server and enabled and not started


def dispatch_status(data: dict, *, launcher, caption) -> None:
    """把一条 status 事件分发给界面。

    提到模块级只有一个理由：它是「事件 → 界面」的唯一映射，原来埋在 main() 的
    闭包里，测试够不到 —— 新增的 dropped 分支尤其需要被测到（C45 要的就是它
    可见）。
    """
    state = data.get("state")
    if state == "loading":
        launcher.set_status(data.get("message", "正在加载模型…"))
    elif state == "running":
        # 模式由会话级的 bilingual 派生（Task 6）。与 cli.print_event、
        # index.html 的 setStatus() 逐字一致（C43）。键缺失时什么也不追加。
        mode = data.get("bilingual")
        suffix = "" if mode is None else (" · 翻译中" if mode else " · 不翻译")
        if data.get("model"):
            launcher.set_status(
                f"运行中 · {data['model']} · {data['device']}{suffix}")
        else:
            launcher.set_status(
                f"运行中 · 按进程捕获（PID {data.get('pid')}）{suffix}")
    elif state == "warning":
        launcher.set_status(data.get("message", ""))
    elif state == "degraded":
        # 原文照搬并标红 —— 降级意味着声音隔离已经失效（原规格 §5.1）
        launcher.set_status(data.get("message", ""), degraded=True)
    elif state == "dropped":
        # C45：这张窗口是捕获期间唯一看得见的东西（启动器在 on_start 里已
        # hide），所以提示要落在这里，而不是只写进启动器那个看不见的状态行。
        message = data.get("message", "")
        caption.add_notice(message)
        launcher.set_status(message)
    elif state == "stopped":
        launcher.set_status("已停止")


def persist_settings(caption, launcher) -> None:
    """把语言与窗口几何一起写回 gui.json。

    模块级而不是 main 里的闭包：闭包只能靠「点一次停止再重启程序」验，
    而那条路径在测试里够不着 —— 测试里退循环靠 qapp.quit()，它不走
    on_stop/on_quit。与 dispatch_status 同理。
    """
    save_settings(with_language(caption.current_settings(),
                                launcher.selected_language()))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    # 关掉最后一个窗口不退出 —— 悬浮窗是可隐藏的，程序活在托盘里
    app.setQuitOnLastWindowClosed(False)

    relay = EventRelay()
    caption = CaptionWindow()
    launcher = LauncherWindow()
    controller = CaptureController(relay)

    if args.no_server:
        launcher.set_page_option_enabled(False)

    # 恢复上次的位置与语言；窗口落在已拔掉的显示器上就居中回来
    settings = load_settings()
    caption.apply_settings(restore_position(settings, _screens()))
    launcher.set_language(settings.language)

    # 网页是可选的。要在没开网页时也能原样跑，publish 得有个空实现。
    # 复选框只在「开始」时被读一次（on_start 里的 should_start_page_server）：
    # 它决定这一次开始要不要起服务。服务最多起一次（server_started 一旦为真
    # 就不再回假）、跑在守护线程上、随进程退出。中途取消勾选没有任何效果 ——
    # 取消勾选不是关服务的办法，这里有意不给拆除 API，代价是服务关不掉。
    publish = lambda event: None                                  # noqa: E731
    server_started = False

    def emit(event: dict) -> None:
        """引擎的事件同时给界面和网页 —— 与 cli.py 走的是同一条路。"""
        relay.emit_event(event)
        publish(event)

    def persist() -> None:
        persist_settings(caption, launcher)

    def on_finished() -> None:
        launcher.set_capturing(False)
        launcher.show()
        launcher.raise_()
        # 规格 §5.6：停止后把两个窗口反过来 —— 置顶窗留着会让人以为还在捕获
        caption.hide()

    relay.status.connect(
        lambda data: dispatch_status(data, launcher=launcher, caption=caption))
    relay.cue.connect(caption.add_cue)
    relay.error.connect(lambda m: launcher.set_status(m, degraded=True))
    controller.finished.connect(on_finished)

    def on_start(proc) -> None:
        nonlocal publish, server_started
        if controller.running():
            # 上一次的捕获还没退干净（多半是停止时超时放弃的那台引擎）。
            # 这时不能收窗口、不能把按钮切成「停止」—— 什么都还没开始跑。
            relay.error.emit("上一次的捕获还没停下来，请退出程序后重开")
            return
        # 语言在启动器里随时可能被改，所以这里现算，不能用启动时那份。
        # C38 的强制点在 --language 的 argparse choices 与下拉由 LANGUAGES 生成处；
        # gui.json 里被手改的未知值在 settings-load 边界被 coerce 回默认（Task 9），
        # 且这份语言在按「开始」前就显示在下拉里 —— 不会静默用别的语言去听。
        session_cfg = load_config(launcher.selected_language())
        # 没配 key 的提示必须用会话自己这份 cfg：文案点名的语言是这次真正会跑的
        # 语言，而不是启动时保存/默认的 en —— 用户可能改了下拉再开始。
        if (notice := keyless_notice(session_cfg.translate)):
            relay.error.emit(notice)
        if should_start_page_server(no_server=args.no_server,
                                    enabled=launcher.open_page_enabled(),
                                    started=server_started):
            from chrometrans.server import EventBus
            from chrometrans.serving import start_server

            publish = start_server(EventBus(), args.host, args.port)
            server_started = True
            print(f"字幕页： http://{args.host}:{args.port}/")

        caption.clear_cues()
        launcher.set_capturing(True)
        launcher.hide()
        caption.show()
        caption.raise_()
        controller.start(proc, cfg=session_cfg, emit=emit)

    def on_stop() -> None:
        controller.stop_and_wait()
        persist()

    def on_quit() -> None:
        controller.stop_and_wait()
        persist()
        tray.hide()
        app.quit()

    launcher.start_requested.connect(on_start)
    launcher.stop_requested.connect(on_stop)

    tray = QSystemTrayIcon(_tray_icon(), app)
    menu = QMenu()
    for label, slot in (("显示字幕窗", lambda: (caption.show(), caption.raise_())),
                        ("隐藏字幕窗", caption.hide),
                        ("打开启动器", lambda: (launcher.show(),
                                            launcher.raise_()))):
        action = QAction(label, menu)
        action.triggered.connect(slot)
        menu.addAction(action)
    menu.addSeparator()
    for label, slot in (("停止捕获", on_stop), ("退出", on_quit)):
        action = QAction(label, menu)
        action.triggered.connect(slot)
        menu.addAction(action)
    tray.setContextMenu(menu)
    tray.setToolTip("chrometrans")
    tray.show()

    launcher.show()
    return app.exec()

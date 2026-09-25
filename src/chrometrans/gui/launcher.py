"""启动器窗口：选程序、开始、停止。

只做摆放（C36）：枚举与标签在 gui/processes.py 里。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from chrometrans.config import DEFAULT_LANGUAGE, LANGUAGES
from chrometrans.gui.processes import AudioProcess, list_audio_processes

REFRESH_MS = 2000
EMPTY_HINT = "没有检测到正在发声的程序 —— 先让网课开始播放，再点刷新"
ERROR_HINT_PREFIX = "枚举进程失败："


class LauncherWindow(QWidget):
    start_requested = Signal(AudioProcess)
    stop_requested = Signal()
    open_page_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("chrometrans")
        self.resize(520, 220)
        self._capturing = False
        self._hint = ""
        self._status = ""

        self._combo = QComboBox(self)
        self._hint_label = QLabel(EMPTY_HINT, self)
        self._hint_label.setWordWrap(True)
        self._hint_label.setStyleSheet("color:#9aa0a6;")
        self._status_label = QLabel("", self)
        self._status_label.setWordWrap(True)
        self._refresh_btn = QPushButton("刷新", self)
        self._start_btn = QPushButton("开始", self)
        self._page_check = QCheckBox("同时开启网页", self)
        self._page_check.setChecked(False)
        self._lang_combo = QComboBox(self)
        # 项与顺序都来自 LANGUAGES：加语言时这里不用跟着改
        for code, profile in LANGUAGES.items():
            self._lang_combo.addItem(profile.label, code)
        self._lang_combo.setCurrentIndex(list(LANGUAGES).index(DEFAULT_LANGUAGE))

        buttons = QHBoxLayout()
        buttons.addWidget(self._refresh_btn)
        buttons.addWidget(self._start_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("选择要捕获的程序：", self))
        layout.addWidget(self._combo)
        layout.addWidget(QLabel("音频语言：", self))
        layout.addWidget(self._lang_combo)
        layout.addWidget(self._hint_label)
        layout.addLayout(buttons)
        layout.addWidget(self._page_check)
        layout.addWidget(self._status_label)
        layout.addStretch(1)

        self._refresh_btn.clicked.connect(self.refresh)
        self._start_btn.clicked.connect(self._on_start_or_stop)
        self._page_check.toggled.connect(self.open_page_toggled.emit)

        # 目标程序要先出声才会进列表，所以列表是会变的。只在没在捕获时才轮询。
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

        self.refresh()

    # ---- 音频语言 ----

    def selected_language(self) -> str:
        return self._lang_combo.currentData()

    def set_language(self, code: str) -> None:
        index = self._lang_combo.findData(code)
        if index >= 0:
            self._lang_combo.setCurrentIndex(index)

    def language_count(self) -> int:
        """给测试用：下拉项数。"""
        return self._lang_combo.count()

    def language_at(self, index: int) -> str:
        """给测试用：第 index 项的 code。"""
        return self._lang_combo.itemData(index)

    def language_editable(self) -> bool:
        """给测试用：下拉是否可用。"""
        return self._lang_combo.isEnabled()

    # ---- 列表 ----

    def refresh(self) -> None:
        try:
            procs = list_audio_processes()
            hint = "" if procs else EMPTY_HINT
        except Exception as exc:
            # 枚举失败与「没有程序在发声」必须给两句不同的话 —— 前者说明
            # 枚举本身坏了，后者是正常的、可以引导用户先播放。
            procs, hint = [], f"{ERROR_HINT_PREFIX}{exc}"

        previous = self._selected_pid()
        self._combo.clear()
        for proc in procs:
            self._combo.addItem(proc.label, proc)
        if previous is not None:
            self.select_pid(previous)

        self._hint = hint
        self._hint_label.setText(hint)
        self._sync_buttons()

    def labels(self) -> list[str]:
        return [self._combo.itemText(i) for i in range(self._combo.count())]

    def hint_text(self) -> str:
        return self._hint

    def _selected_pid(self) -> int | None:
        proc = self.selected()
        return proc.pid if proc else None

    def selected(self) -> AudioProcess | None:
        data = self._combo.currentData()
        return data if isinstance(data, AudioProcess) else None

    def select_pid(self, pid: int) -> None:
        for i in range(self._combo.count()):
            if self._combo.itemData(i).pid == pid:
                self._combo.setCurrentIndex(i)
                return

    # ---- 按钮 ----

    def start_enabled(self) -> bool:
        return not self._capturing and self.selected() is not None

    def _sync_buttons(self) -> None:
        self._start_btn.setText("停止" if self._capturing else "开始")
        self._start_btn.setEnabled(
            self._capturing or self.selected() is not None)

    def _on_start_or_stop(self) -> None:
        if self._capturing:
            self.stop_requested.emit()
            return
        proc = self.selected()
        if proc is None:
            return
        self.start_requested.emit(proc)

    def click_start(self) -> None:
        """给测试用：等价于用户点了那个按钮。"""
        self._on_start_or_stop()

    def set_capturing(self, capturing: bool) -> None:
        self._capturing = capturing
        # 换语言要重建 engine（换阈值集、换解码配置），而捕获中禁止重入 ——
        # 所以这里禁用，不是「点了没反应」。
        self._lang_combo.setEnabled(not capturing)
        # 已经在捕获就不必再轮询列表了
        if capturing:
            self._timer.stop()
        elif not self._timer.isActive():
            self._timer.start()
        self._sync_buttons()

    # ---- 状态行 ----

    def set_status(self, text: str, degraded: bool = False) -> None:
        """degraded=True 时把文案标红。

        降级意味着声音隔离已经失效（原规格 §5.1），在 GUI 里不能比在终端里
        说得更轻。
        """
        self._status = text
        color = "#f87171" if degraded else "#9aa0a6"
        weight = "600" if degraded else "400"
        prefix = "⚠ " if degraded else ""
        self._status_label.setStyleSheet(f"color:{color};font-weight:{weight};")
        self._status_label.setText(f"{prefix}{text}")

    def status_text(self) -> str:
        return self._status

    def open_page_enabled(self) -> bool:
        return self._page_check.isChecked()

    def set_page_option_enabled(self, enabled: bool) -> None:
        """`--no-server` 时用它把复选框禁掉 —— 勾了也不会启服务。"""
        self._page_check.setChecked(False)
        self._page_check.setEnabled(enabled)

    def page_option_is_enabled(self) -> bool:
        return self._page_check.isEnabled()

"""置顶悬浮字幕窗，像 Chrome 自带的实时字幕。

只做摆放（C36）：内容的组织规则是模块级的纯函数 render_cue_html，
行为规则在 gui/transcript.py 与 gui/settings.py 里。
"""
from __future__ import annotations

import html as html_escape

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QFrame,
    QMenu,
    QSizeGrip,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from chrometrans.gui.settings import GuiSettings
from chrometrans.gui.transcript import Transcript
from chrometrans.models import is_monolingual

MIN_FONT_PX = 10
MAX_FONT_PX = 72
MIN_OPACITY = 0.2
MAX_OPACITY = 1.0
_BG = "#16181d"


_NOTICE_COLOR = "#8a7a4a"


def render_cue_html(cue: dict, font_px: int) -> str:
    """一条字幕 → 一段 HTML。

    顺序与 captions.srt 和网页一致 —— 同一份内容在三个地方不该有三种排法。
    文本必须转义：ASR 输出里出现 `<` 或 `&` 时，当 HTML 解释会吃掉半句话。
    """
    notice = cue.get("notice")
    if notice:
        return (f'<div style="color:{_NOTICE_COLOR};font-style:italic;'
                f'font-size:{max(MIN_FONT_PX, font_px - 6)}px;'
                f'line-height:1.35">⊘ {html_escape.escape(notice)}</div>')

    source = html_escape.escape(cue.get("source") or "")
    target = cue.get("target")
    # C44：故意不翻译（单语会话）不得表现为翻译失败。判据是 tgt_lang，
    # 与 output/srt.py、output/markdown.py 用的是同一个 —— 四处不一致是
    # C43 明令禁止的。
    if is_monolingual(cue):
        return (f'<div style="color:#f5f6f8;font-size:{font_px + 8}px;'
                f'line-height:1.35">{source}</div>')

    parts = [
        f'<div style="color:#9aa0a6;font-size:{font_px}px;'
        f'line-height:1.35">{source}</div>'
    ]
    if target:
        parts.append(
            f'<div style="color:#f5f6f8;font-size:{font_px + 8}px;'
            f'line-height:1.35;margin-top:2px">{html_escape.escape(target)}</div>')
    else:
        parts.append(
            f'<div style="color:#6b7280;font-style:italic;'
            f'font-size:{font_px}px;margin-top:2px">（未翻译）</div>')
    return "".join(parts)


class CaptionWindow(QWidget):
    """无边框、置顶、可拖动、可缩放的悬浮字幕窗。"""

    def __init__(self, cap: int = 300, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font_px = 22
        self._opacity = 0.9
        self._drag_offset: QPoint | None = None
        # 自己留一份：改字号时要照着重新渲一遍，而不是等下一句才生效。
        # 用 Transcript 而不是裸 list —— 裸 list 无上限，2 小时的课能涨到上千条。
        self._transcript = Transcript(cap)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            # C37：不抢键盘焦点。用户看网课时正是要在别处打字。
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet(
            f"QWidget {{ background:{_BG}; }}"
            f"QTextEdit {{ background:{_BG}; border:1px solid #2c3038;"
            f"color:#e8eaed; padding:6px; }}")
        self.resize(900, 220)

        self._view = QTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setFrameShape(QFrame.Shape.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)
        layout.addWidget(QSizeGrip(self),
                         alignment=Qt.AlignmentFlag.AlignBottom
                         | Qt.AlignmentFlag.AlignRight)

    # ---- 内容 ----

    def add_cue(self, cue: dict) -> None:
        at_bottom = self._is_scrolled_to_bottom()
        if self._transcript.add(cue):
            # 满了，最旧那条被挤掉 —— 控件自己那份文档也要跟着裁。
            # 光裁模型的话，QTextEdit 的文档照样无限长。
            self._rerender()
        else:
            self._view.append(render_cue_html(cue, self._font_px))
        # 用户往回翻看前文时不要把他拽回底部；滚回底部后自动跟随恢复
        if at_bottom:
            self._scroll_to_bottom()

    def clear_cues(self) -> None:
        self._transcript.clear()
        self._view.clear()

    def add_notice(self, text: str) -> None:
        """插一条提示行（C45 的丢弃提示走这里）。

        进的是同一个 Transcript 模型，不是直接 append 到控件：改字号或淘汰最旧
        一条都会触发 _rerender 重建整份文档，只 append 的写法会在那一刻凭空
        消失 —— 而「凭空消失」正是 C45 要消灭的东西。

        空文本不是提示：render_cue_html 里 `if notice:` 对空串为假，会一路落回
        双语分支，把空 source + target=None 渲染成「（未翻译）」—— C44 要消灭的
        「翻译失败」标记，出现在根本没尝试翻译的语境里。
        """
        text = (text or "").strip()
        if not text:
            return
        self.add_cue({"notice": text})

    def _rerender(self) -> None:
        """照着留下的字幕重渲一遍，重建前后保住滚动位置。

        改字号与淘汰最旧一条都走这条。clear + 重新 append 会把滚动条拽走，
        所以重建前先记下位置：本来就在底部就跟着新字幕回到最底部；用户自己
        翻上去了就停在原地 —— 否则满到上限后每来一句，都会把正在回看历史
        的人拽下去。
        """
        bar = self._view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        offset = bar.value()
        self._view.clear()
        for cue in self._transcript.cues():
            self._view.append(render_cue_html(cue, self._font_px))
        if at_bottom:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(offset)

    def toPlainText(self) -> str:
        """给测试用：把显示内容当纯文本取出来。"""
        return self._view.toPlainText()

    def toHtml(self) -> str:
        """给测试用：取渲染后的 HTML。

        「改字号要立刻重渲」这条只能靠 HTML 验证 —— 纯文本在字号变化前后
        逐字相同，拿它断言等于什么都没验。
        """
        return self._view.toHtml()

    def _is_scrolled_to_bottom(self) -> bool:
        bar = self._view.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _scroll_to_bottom(self) -> None:
        bar = self._view.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ---- 显示偏好 ----

    def font_px(self) -> int:
        return self._font_px

    def set_font_px(self, px: int) -> None:
        self._font_px = max(MIN_FONT_PX, min(MAX_FONT_PX, int(px)))
        at_bottom = self._is_scrolled_to_bottom()
        self._rerender()
        if at_bottom:
            self._scroll_to_bottom()

    def opacity(self) -> float:
        return self._opacity

    def set_opacity(self, value: float) -> None:
        self._opacity = max(MIN_OPACITY, min(MAX_OPACITY, float(value)))
        self.setWindowOpacity(self._opacity)

    # ---- 设置 ----

    def apply_settings(self, s: GuiSettings) -> None:
        self.resize(s.width, s.height)
        if s.x is not None and s.y is not None:
            self.move(s.x, s.y)
        self.set_font_px(s.font_px)
        self.set_opacity(s.opacity)

    def current_settings(self) -> GuiSettings:
        return GuiSettings(x=self.x(), y=self.y(),
                           width=self.width(), height=self.height(),
                           font_px=self._font_px, opacity=self._opacity)

    # ---- 交互 ----

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (event.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.addAction("字号 +", lambda: self._nudge_font(2))
        menu.addAction("字号 −", lambda: self._nudge_font(-2))
        menu.addSeparator()
        menu.addAction("不透明度 +", lambda: self._nudge_opacity(0.1))
        menu.addAction("不透明度 −", lambda: self._nudge_opacity(-0.1))
        menu.addSeparator()
        menu.addAction("隐藏窗口", self.hide)
        menu.exec(event.globalPos())

    def _nudge_font(self, delta: int) -> None:
        self.set_font_px(self._font_px + delta)

    def _nudge_opacity(self, delta: float) -> None:
        self.set_opacity(self._opacity + delta)

    def closeEvent(self, event) -> None:
        # 关闭按钮只隐藏窗口，不退出程序 —— 从托盘还能叫回来
        event.ignore()
        self.hide()

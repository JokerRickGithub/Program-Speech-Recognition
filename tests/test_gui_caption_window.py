import os

import pytest

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp):
    from chrometrans.gui.caption_window import CaptionWindow

    w = CaptionWindow()
    yield w
    w.close()


def test_window_does_not_steal_focus(qapp):
    """C37：置顶但不抢焦点。

    用户看网课时正是要在别处打字 —— 悬浮窗把键盘焦点抢走就没法打。
    """
    from PySide6.QtCore import Qt

    from chrometrans.gui.caption_window import CaptionWindow

    flags = CaptionWindow().windowFlags()
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus


def test_window_is_frameless_and_always_on_top(qapp):
    from PySide6.QtCore import Qt

    from chrometrans.gui.caption_window import CaptionWindow

    flags = CaptionWindow().windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.Tool, "Tool 让它不占任务栏、不进 Alt+Tab"


def test_render_cue_html_puts_the_source_above_the_target():
    from chrometrans.gui.caption_window import render_cue_html

    html = render_cue_html({"source": "hello", "target": "你好"}, 22)

    assert html.index("hello") < html.index("你好"), \
        "原文在上、译文在下，与 SRT 和网页保持一致"


def test_render_cue_html_invents_nothing_when_translation_is_missing():
    from chrometrans.gui.caption_window import render_cue_html

    html = render_cue_html({"source": "hello", "target": None}, 22)

    assert "hello" in html
    assert "未翻译" in html, "要明确说没翻出来，而不是留一片空白"


def test_render_cue_html_escapes_markup_in_the_text():
    """ASR 输出会带 < 和 & —— 当成 HTML 解释会吃掉半句话。"""
    from chrometrans.gui.caption_window import render_cue_html

    html = render_cue_html({"source": "a < b & c", "target": None}, 22)

    assert "a &lt; b &amp; c" in html


def test_add_cue_shows_both_languages(window):
    window.add_cue({"source": "hello", "target": "你好"})

    text = window.toPlainText()
    assert "hello" in text and "你好" in text


def test_font_size_round_trips_and_is_bounded(window):
    window.set_font_px(30)
    assert window.font_px() == 30

    window.set_font_px(1)
    assert window.font_px() == 10, "太小就没法读，下限 10"

    window.set_font_px(999)
    assert window.font_px() == 72, "太大就盖满屏幕，上限 72"


def test_changing_the_font_size_re_renders_what_is_already_on_screen(window):
    """改了字号却要等下一句才生效，右键菜单看起来就像坏的。"""
    window.add_cue({"source": "hello", "target": "你好"})
    before = window.toHtml()

    window.set_font_px(40)

    assert window.toHtml() != before
    assert window.font_px() == 40


def test_opacity_round_trips_and_is_bounded(window):
    window.set_opacity(0.5)
    assert window.opacity() == pytest.approx(0.5)

    window.set_opacity(0.0)
    assert window.opacity() == pytest.approx(0.2), "全透明等于窗口消失"

    window.set_opacity(5)
    assert window.opacity() == pytest.approx(1.0)


def test_settings_round_trip_through_the_window(window):
    from chrometrans.gui.settings import GuiSettings

    window.apply_settings(GuiSettings(x=30, y=40, width=500, height=160,
                                      font_px=26, opacity=0.7))
    got = window.current_settings()

    assert (got.width, got.height) == (500, 160)
    assert got.font_px == 26
    assert got.opacity == pytest.approx(0.7)


def test_rebuilding_from_the_transcript_restores_the_content(qapp):
    """窗口藏起来再显示时，内容要能从模型重建回来。"""
    from chrometrans.gui.caption_window import CaptionWindow

    w = CaptionWindow()
    w.add_cue({"source": "one", "target": "一"})
    w.add_cue({"source": "two", "target": "二"})
    w.clear_cues()
    assert w.toPlainText().strip() == ""

    w.add_cue({"source": "one", "target": "一"})
    assert "one" in w.toPlainText()
    w.close()


def test_the_view_itself_is_trimmed_not_just_the_model(qapp):
    """只裁模型不裁控件，QTextEdit 的文档照样无限长。"""
    from chrometrans.gui.caption_window import CaptionWindow

    w = CaptionWindow(cap=2)
    for i in range(5):
        w.add_cue({"source": f"s{i}", "target": f"t{i}"})

    text = w.toPlainText()
    assert "s3" in text and "s4" in text, "最新的两条要留着"
    assert "s0" not in text and "s1" not in text, "超出上限的要真的从文档里消失"
    w.close()


def _fill_window(w, n):
    for i in range(n):
        w.add_cue({"source": f"source line {i}", "target": f"译文 {i}"})


def test_rerender_keeps_scroll_position_when_user_scrolled_up(qapp):
    """淘汰触发重渲时，用户往回翻的历史位置不能被拽走。

    满到上限后每来一句都会挤掉最旧一条、都会走 _rerender —— 若重渲把滚动条
    复位，用户往上翻着看历史时会被下一句字幕拽走。
    """
    from chrometrans.gui.caption_window import CaptionWindow

    w = CaptionWindow(cap=50)
    w.show()
    qapp.processEvents()
    _fill_window(w, 50)

    bar = w._view.verticalScrollBar()
    assert bar.maximum() > 0, \
        "offscreen 下滚动条要有有效范围，否则这条断言等于什么都没测"
    mid = bar.maximum() // 2
    bar.setValue(mid)
    qapp.processEvents()
    assert bar.value() == mid, "先把滚动条停到中间位置"

    w.add_cue({"source": "newest", "target": "最新"})  # 触发淘汰 → _rerender
    qapp.processEvents()

    assert abs(bar.value() - mid) <= 2, "重渲不得把用户正在回看的位置拽走"
    w.close()


def test_rerender_follows_new_cue_when_at_bottom(qapp):
    """停在底部时，淘汰重渲后仍粘在底部（跟随新字幕）。"""
    from chrometrans.gui.caption_window import CaptionWindow

    w = CaptionWindow(cap=50)
    w.show()
    qapp.processEvents()
    _fill_window(w, 50)

    bar = w._view.verticalScrollBar()
    bar.setValue(bar.maximum())
    qapp.processEvents()
    assert bar.value() >= bar.maximum() - 4, "先把滚动条拉到底部"

    w.add_cue({"source": "newest", "target": "最新"})  # 触发淘汰 → _rerender
    qapp.processEvents()

    assert bar.value() >= bar.maximum() - 4, "在底部就该一直粘住底部"
    w.close()

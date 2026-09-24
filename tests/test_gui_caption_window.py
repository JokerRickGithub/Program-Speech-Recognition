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


def test_monolingual_cue_has_no_placeholder():
    """C44：中文模式下翻译是故意不做的，复用「翻译失败」那条状态会让每句话
    底下挂一条错误提示。"""
    from chrometrans.gui.caption_window import render_cue_html

    html = render_cue_html({"source": "你好", "target": None, "tgt_lang": None}, 22)

    assert "你好" in html
    assert "未翻译" not in html


def test_failed_translation_still_shows_the_placeholder():
    """反向也要锁：双语会话里翻译真挂了，那条提示必须还在。

    只测上一条的话，把 `if target:` 直接删掉也能通过。
    """
    from chrometrans.gui.caption_window import render_cue_html

    html = render_cue_html({"source": "hello", "target": None,
                            "tgt_lang": "zh-Hans"}, 22)

    assert "未翻译" in html


def test_a_cue_without_the_key_renders_like_a_bilingual_one():
    """喂进来的 dict 缺 tgt_lang 时，退回双语渲染 —— 不能把译文吞掉。

    该文件里原有的三条纯函数用例构造的正是这种 dict，它们必须照旧通过，
    这条是它们的显式化：

        test_render_cue_html_puts_the_source_above_the_target
        test_render_cue_html_invents_nothing_when_translation_is_missing
        test_render_cue_html_escapes_markup_in_the_text
    """
    from chrometrans.gui.caption_window import render_cue_html

    html = render_cue_html({"source": "hello", "target": "你好"}, 22)

    assert "你好" in html
    assert html.index("hello") < html.index("你好")


def test_monolingual_cue_uses_the_large_font_for_the_source():
    """单语时原文就是主角，不该还是那个灰色小字。"""
    from chrometrans.gui.caption_window import render_cue_html

    html = render_cue_html({"source": "你好", "target": None, "tgt_lang": None}, 22)

    assert "font-size:30px" in html, "单语下原文用译文的字号"


def test_notice_rows_render_and_survive_a_rerender(window):
    """C45：丢弃提示必须看得见，而且不能在改字号时凭空消失。

    提示行走的是同一个 Transcript 模型，所以 _rerender 重建时它还在 ——
    只 append 到控件上的写法会在改一次字号之后把它抹掉。
    """
    window.add_cue({"source": "第一句", "target": "first", "tgt_lang": "en"})
    window.add_notice("丢弃疑似幻觉：谢谢观看")
    assert "丢弃疑似幻觉" in window.toPlainText()

    window.set_font_px(28)

    assert "丢弃疑似幻觉" in window.toPlainText()
    assert "第一句" in window.toPlainText()


def test_clear_cues_also_clears_notices(window):
    """新会话开始时上一次的丢弃提示不该留着。"""
    window.add_notice("丢弃疑似幻觉：旧会话的")
    window.clear_cues()

    assert "丢弃疑似幻觉" not in window.toPlainText()

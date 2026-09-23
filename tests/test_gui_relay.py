import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    """Qt 信号需要事件循环。offscreen 平台让它在无显示器的环境里也能跑。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def relay(qapp):
    from chrometrans.gui.relay import EventRelay

    return EventRelay()


@pytest.mark.gui
def test_cue_event_reaches_the_cue_signal(relay):
    got = []
    relay.cue.connect(got.append)

    relay.emit_event({"event": "cue", "data": {"id": 1, "source": "hello"}})

    assert got == [{"id": 1, "source": "hello"}]


@pytest.mark.gui
def test_status_event_reaches_the_status_signal(relay):
    got = []
    relay.status.connect(got.append)

    relay.emit_event({"event": "status",
                      "data": {"state": "loading", "message": "正在加载模型"}})

    assert got == [{"state": "loading", "message": "正在加载模型"}]


@pytest.mark.gui
def test_error_event_reaches_the_error_signal_with_a_plain_string(relay):
    """控件上显示的是文字，不是 dict —— 在这层就把它拍平。"""
    got = []
    relay.error.connect(got.append)

    relay.emit_event({"event": "error", "data": {"message": "翻译失败：超时"}})

    assert got == ["翻译失败：超时"]


@pytest.mark.gui
def test_an_unknown_event_kind_is_ignored_rather_than_crashing(relay):
    relay.emit_event({"event": "未来才有的事件", "data": {}})   # 不应抛异常


@pytest.mark.gui
def test_a_cue_without_data_does_not_crash(relay):
    """引擎的 error 事件没有 data 时也走过这条路，别在取字段时炸掉。"""
    relay.emit_event({"event": "error"})

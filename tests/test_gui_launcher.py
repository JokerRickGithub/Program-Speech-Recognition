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
def launcher(qapp, monkeypatch):
    import chrometrans.gui.launcher as launcher_mod

    monkeypatch.setattr(launcher_mod, "list_audio_processes", lambda: [])
    w = launcher_mod.LauncherWindow()
    yield w
    w.close()


def test_empty_list_says_what_to_do_about_it(launcher):
    """C32：目标没出声时它不在列表里。不说清楚，用户会以为程序坏了。"""
    launcher.refresh()

    assert "先让网课开始播放" in launcher.hint_text()


def test_enumeration_failure_says_something_different(qapp, monkeypatch):
    """「枚举失败」与「没有程序在发声」是两件事，不能长得一模一样。"""
    import chrometrans.gui.launcher as launcher_mod

    def boom():
        raise OSError("音频会话枚举失败")

    monkeypatch.setattr(launcher_mod, "list_audio_processes", boom)
    w = launcher_mod.LauncherWindow()
    w.refresh()

    assert "枚举进程失败" in w.hint_text()
    assert "先让网课开始播放" not in w.hint_text()
    w.close()


def test_processes_are_listed_with_their_labels(qapp, monkeypatch):
    import chrometrans.gui.launcher as launcher_mod
    from chrometrans.gui.processes import AudioProcess

    monkeypatch.setattr(launcher_mod, "list_audio_processes", lambda: [
        AudioProcess(100, "chrome.exe"), AudioProcess(200, "wemeetapp.exe")])
    w = launcher_mod.LauncherWindow()
    w.refresh()

    assert w.selected() == AudioProcess(100, "chrome.exe")
    assert "wemeetapp.exe (PID 200)" in w.labels()
    w.close()


def test_selection_survives_a_refresh(qapp, monkeypatch):
    """2 秒自动刷新一次，用户选中的那项不能被刷掉。"""
    import chrometrans.gui.launcher as launcher_mod
    from chrometrans.gui.processes import AudioProcess

    procs = [AudioProcess(100, "chrome.exe"), AudioProcess(200, "wemeetapp.exe")]
    monkeypatch.setattr(launcher_mod, "list_audio_processes", lambda: list(procs))
    w = launcher_mod.LauncherWindow()
    w.refresh()
    w.select_pid(200)
    w.refresh()

    assert w.selected() == AudioProcess(200, "wemeetapp.exe")
    w.close()


def test_a_disappeared_selection_falls_back_to_the_first_entry(qapp, monkeypatch):
    import chrometrans.gui.launcher as launcher_mod
    from chrometrans.gui.processes import AudioProcess

    procs = [AudioProcess(100, "chrome.exe"), AudioProcess(200, "wemeetapp.exe")]
    monkeypatch.setattr(launcher_mod, "list_audio_processes", lambda: list(procs))
    w = launcher_mod.LauncherWindow()
    w.refresh()
    w.select_pid(200)

    del procs[1]
    w.refresh()

    assert w.selected() == AudioProcess(100, "chrome.exe")
    w.close()


def test_start_button_is_disabled_while_the_list_is_empty(launcher):
    launcher.refresh()

    assert not launcher.start_enabled()


def test_status_line_shows_the_degradation_warning_verbatim(launcher):
    """原文照搬 —— 降级意味着声音隔离已经失效，不能在 GUI 里被淡化。"""
    launcher.set_status("已降级到系统级捕获：会混入 Edge、背景音乐等声音。", True)

    assert "已降级到系统级捕获" in launcher.status_text()


def test_start_is_not_emitted_when_nothing_is_selected(launcher):
    got = []
    launcher.start_requested.connect(got.append)

    launcher.refresh()
    launcher.click_start()

    assert got == []


def test_start_emits_the_selected_process(qapp, monkeypatch):
    import chrometrans.gui.launcher as launcher_mod
    from chrometrans.gui.processes import AudioProcess

    monkeypatch.setattr(launcher_mod, "list_audio_processes", lambda: [
        AudioProcess(100, "chrome.exe")])
    w = launcher_mod.LauncherWindow()
    got = []
    w.start_requested.connect(got.append)
    w.refresh()
    w.click_start()

    assert got == [AudioProcess(100, "chrome.exe")]
    w.close()


def test_web_page_checkbox_is_off_by_default(launcher):
    """网页版是可选项，不该默认多开一个端口。"""
    assert launcher.open_page_enabled() is False


def test_page_option_can_be_disabled_when_no_server_was_asked_for(launcher):
    """--no-server 时把它禁掉：勾了也不会启服务，不如不让勾。"""
    launcher.set_page_option_enabled(False)

    assert launcher.page_option_is_enabled() is False
    assert launcher.open_page_enabled() is False


def test_capturing_state_swaps_start_for_stop(launcher):
    launcher.set_capturing(True)
    assert not launcher.start_enabled()

    launcher.set_capturing(False)
    assert launcher.start_enabled() is False, "列表空时开始仍然不可点"


def test_language_dropdown_lists_every_language_in_table_order(launcher):
    """下拉项与顺序都由 LANGUAGES 生成 —— 加语言时这里不该还要单独改一处。"""
    from chrometrans.config import LANGUAGES

    got = [launcher.language_at(i) for i in range(launcher.language_count())]

    assert got == list(LANGUAGES)


def test_selected_language_defaults_to_english(launcher):
    from chrometrans.config import DEFAULT_LANGUAGE

    assert launcher.selected_language() == DEFAULT_LANGUAGE


def test_set_language_selects_that_code(launcher):
    launcher.set_language("zh")

    assert launcher.selected_language() == "zh"


def test_set_language_ignores_an_unknown_code(launcher):
    """界面只可能产出白名单里的值；给个野值不该把当前选择弄丢。"""
    launcher.set_language("zh")
    launcher.set_language("klingon")

    assert launcher.selected_language() == "zh"


def test_language_dropdown_is_disabled_while_capturing(launcher):
    """换语言要重建 engine，而捕获中本来就禁止重入 —— 所以下拉要禁用，
    不能只是「点了没反应」。"""
    assert launcher.language_editable()

    launcher.set_capturing(True)
    assert not launcher.language_editable()

    launcher.set_capturing(False)
    assert launcher.language_editable()

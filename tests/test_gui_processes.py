from dataclasses import dataclass

import pytest

from chrometrans.audio.capture import CaptureError


@dataclass
class LibProcess:
    """假装是 process_audio_capture 那个三字段的 AudioProcess。"""
    pid: int
    name: str
    window_title: str = ""


def test_label_has_both_name_and_pid():
    from chrometrans.gui.processes import AudioProcess

    assert AudioProcess(1234, "chrome.exe").label == "chrome.exe (PID 1234)"


def test_audio_process_is_hashable_and_frozen():
    from chrometrans.gui.processes import AudioProcess

    p = AudioProcess(1, "chrome.exe")
    assert {p} == {AudioProcess(1, "chrome.exe")}
    with pytest.raises(Exception):
        p.pid = 2


def test_list_maps_library_processes_to_our_two_field_form(monkeypatch):
    import chrometrans.gui.processes as processes

    monkeypatch.setattr(
        processes.ProcessAudioCapture, "enumerate_audio_processes",
        classmethod(lambda cls, dll_path=None: [
            LibProcess(100, "chrome.exe"),
            LibProcess(200, "wemeetapp.exe"),
        ]))

    got = processes.list_audio_processes()

    assert [(p.pid, p.name) for p in got] == [
        (100, "chrome.exe"), (200, "wemeetapp.exe")]


def test_enumeration_failure_propagates(monkeypatch):
    """枚举失败不能吞 —— 界面上它和「没有程序在发声」必须给两句不同的话。"""
    import chrometrans.gui.processes as processes

    def boom(cls, dll_path=None):
        raise OSError("音频会话枚举失败")

    monkeypatch.setattr(processes.ProcessAudioCapture,
                        "enumerate_audio_processes", classmethod(boom))

    with pytest.raises(OSError):
        processes.list_audio_processes()


def test_resolver_returns_the_pinned_pid_while_it_lives():
    """用户选的就是它，不许再猜。"""
    from chrometrans.gui.processes import AudioProcess, make_resolver

    calls = []

    def find(names, probe_seconds=1.0):
        calls.append(names)
        raise AssertionError("PID 还活着时不该去按名字重新解析")

    resolve = make_resolver(AudioProcess(4321, "chrome.exe"),
                            alive=lambda pid: True, find=find)

    assert resolve() == 4321
    assert calls == []


def test_resolver_falls_back_to_the_name_when_the_pinned_pid_died():
    """C33 的核心：目标程序重启过之后，不能直接降级到系统级捕获。

    降级会把 Edge、背景音乐全混进来。多花一次按名字解析的代价远小于此。
    """
    from chrometrans.gui.processes import AudioProcess, make_resolver

    resolve = make_resolver(AudioProcess(4321, "chrome.exe"),
                            alive=lambda pid: False,
                            find=lambda names, probe_seconds=1.0: 9999)

    assert resolve() == 9999


def test_resolver_re_resolves_by_the_pinned_process_name():
    from chrometrans.gui.processes import AudioProcess, make_resolver

    seen = []

    def find(names, probe_seconds=1.0):
        seen.append(names)
        return 1

    make_resolver(AudioProcess(4321, "wemeetapp.exe"),
                  alive=lambda pid: False, find=find)()

    assert seen == [("wemeetapp.exe",)]


def test_resolver_lets_capture_error_escape_when_the_name_is_gone_too():
    """名字也找不到才允许走重连与降级 —— 这是 CaptureSource 约定的信号。"""
    from chrometrans.gui.processes import AudioProcess, make_resolver

    def find(names, probe_seconds=1.0):
        raise CaptureError("没有进程在渲染音频")

    resolve = make_resolver(AudioProcess(4321, "chrome.exe"),
                            alive=lambda pid: False, find=find)

    with pytest.raises(CaptureError):
        resolve()

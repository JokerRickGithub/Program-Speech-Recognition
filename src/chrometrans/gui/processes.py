"""音频进程的枚举、标签与 PID 回退解析。

**不 import Qt**（C36）：这里的规则是纯逻辑，必须能在无 Qt、无音频设备的
环境里离线单测。
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Callable

from process_audio_capture import ProcessAudioCapture

from chrometrans.audio.capture import find_target_pid

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


@dataclass(frozen=True)
class AudioProcess:
    """一个正在渲染音频的进程。

    只留 pid 与 name：库自带的那个 AudioProcess 还有 window_title，但我们
    按 C31 只认「是否在渲染音频」，窗口标题与这个判断无关。
    """

    pid: int
    name: str

    @property
    def label(self) -> str:
        """下拉框里显示的文字。"""
        return f"{self.name} (PID {self.pid})"


def list_audio_processes() -> list[AudioProcess]:
    """枚举此刻正在渲染音频的进程（C31 / C32）。

    **异常直接抛出，不吞。** 「枚举失败」和「没有程序在发声」是两件事，界面上
    必须给出两句不同的话：前者说明枚举本身坏了，后者是正常的、可以引导用户
    先让网课开始播放。吞掉异常会让这两件事长得一模一样。
    """
    return [AudioProcess(pid=p.pid, name=p.name)
            for p in ProcessAudioCapture.enumerate_audio_processes()]


def pid_alive(pid: int) -> bool:
    """进程是否还活着。

    用 GetExitCodeProcess 而不是只看 OpenProcess 成不成功：一个已经退出、
    PID 又被回收给新进程的句柄照样能打开。这里能排除的是「PID 不存在」和
    「进程确实已退出」两种。
    """
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def make_resolver(pinned: AudioProcess, *,
                  alive: Callable[[int], bool] = pid_alive,
                  find: Callable[..., int] = find_target_pid) -> Callable[[], int]:
    """钉住用户选中的 PID；它没了就按名字重新解析（C33）。

    不直接降级到系统级捕获：那会把所有程序的声音都混进来，而用户选 PID 的
    全部意义就是隔离。目标程序重启一次就要重新解析一次，这是可接受的代价。

    名字也解析不出来时，让 find_target_pid 的 CaptureError 逃出去 ——
    那是 CaptureSource 约定的信号，由它决定重连还是降级。
    """
    def resolve() -> int:
        if alive(pinned.pid):
            return pinned.pid
        return find((pinned.name,))

    return resolve

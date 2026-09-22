"""按进程捕获 Chrome 音频（spec §5.1）。

为什么这么写（全部有实测依据，见 spec §3.1）：
- C26: 库不覆盖进程树，必须抓「正在渲染音频」的那个 PID
- C28: 库没有内存流接口，且输出文件被独占 → 不能 tail WAV
- C29: 把命名管道当输出路径传入，可得 10ms 粒度的流式 PCM
"""
from __future__ import annotations

import ctypes
import struct
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

import numpy as np
import soxr
from process_audio_capture import ProcessAudioCapture

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_k32.CreateNamedPipeW.restype = wintypes.HANDLE
_k32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
_k32.ConnectNamedPipe.restype = wintypes.BOOL
_k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_k32.ReadFile.restype = wintypes.BOOL
_k32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CancelIoEx.restype = wintypes.BOOL
_k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]

PIPE_ACCESS_INBOUND = 0x00000001
PIPE_TYPE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255

EXPECTED_BITS = 32          # float32；实测 DLL 固定输出此格式


def _close_pipe(handle: int) -> None:
    """先取消挂起 I/O 再关句柄 —— 否则同步句柄的 CloseHandle 会永久阻塞。

    同步命名管道句柄上若仍有挂起的 ConnectNamedPipe / ReadFile，
    CloseHandle 会一直等到该 I/O 完成才返回（而它可能永远等不到）。
    CancelIoEx 会取消任何线程发起的挂起 I/O；返回 ERROR_NOT_FOUND
    （没有挂起 I/O）属正常情况，忽略即可。
    """
    _k32.CancelIoEx(wintypes.HANDLE(handle), None)
    _k32.CloseHandle(wintypes.HANDLE(handle))


class CaptureError(RuntimeError):
    """捕获失败。调用方应降级到系统级 loopback（spec §5.1 降级路径）。"""


@dataclass(frozen=True)
class AudioFormat:
    channels: int
    sample_rate: int
    bits: int


def parse_wav_header(buf: bytes) -> tuple[int, AudioFormat] | None:
    """扫描 RIFF 块，返回 (payload_offset, AudioFormat)；未收全返回 None。

    不依赖 data 块的长度字段 —— 流式写入时总长度未知（spec §5.1）。
    不用 wave 模块：该头是 WAVE_FORMAT_EXTENSIBLE，wave.open() 会抛
    "unknown extended format"。
    """
    if len(buf) < 12 or buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        return None
    off = 12
    fmt_body: bytes | None = None
    while off + 8 <= len(buf):
        chunk_id = buf[off:off + 4]
        size = struct.unpack_from("<I", buf, off + 4)[0]
        if chunk_id == b"fmt ":
            if off + 8 + size > len(buf):
                return None                      # 头还没收全
            fmt_body = buf[off + 8:off + 8 + size]
        elif chunk_id == b"data":
            if fmt_body is None or len(fmt_body) < 16:
                return None
            return off + 8, AudioFormat(
                channels=struct.unpack_from("<H", fmt_body, 2)[0],
                sample_rate=struct.unpack_from("<I", fmt_body, 4)[0],
                bits=struct.unpack_from("<H", fmt_body, 14)[0],
            )
        off += 8 + size + (size & 1)
    return None


class PcmConverter:
    """交错 float32 → 16kHz 单声道 float32。重采样器长驻，块间绝不 clear()（C3）。"""

    def __init__(self, fmt: AudioFormat, target_rate: int = 16000):
        if fmt.bits != EXPECTED_BITS:
            raise CaptureError(
                f"预期 float32（{EXPECTED_BITS} bit），实际 {fmt.bits} bit")
        self._channels = fmt.channels
        # 跨块原始字节余量（未成帧/未成样本的尾部，见 convert()）。
        # 注意与 ProcessAudioStream._pending（WAV 头缓冲）同名但职责不同。
        self._pending = b""
        self._resampler = soxr.ResampleStream(
            fmt.sample_rate, target_rate, 1, dtype="float32")

    def convert(self, data: bytes) -> np.ndarray:
        buf = self._pending + data
        n_samples = len(buf) // 4              # sizeof(float32)
        usable = n_samples * 4
        samples = np.frombuffer(buf[:usable], dtype=np.float32)
        frames = len(samples) // self._channels
        # 未成帧/未成样本的尾部留到下一块（byte-mode 管道读数是任意字节数）
        self._pending = buf[frames * self._channels * 4:]
        if frames == 0:
            return np.zeros(0, dtype=np.float32)
        mono = samples[:frames * self._channels].reshape(frames, self._channels).mean(axis=1)
        return self._resampler.resample_chunk(mono)


def _pick_loudest(cands, probe_seconds: float):
    """多候选时逐个试听，取电平最高的那个（spec §7）。"""
    best, best_db = None, float("-inf")
    for proc in cands:
        cap = ProcessAudioCapture(pid=proc.pid)      # output_path=None = 仅监控
        try:
            cap.start()
            time.sleep(probe_seconds)
            level = cap.level_db
        except Exception:
            continue
        finally:
            try:
                cap.stop()
            except Exception:
                pass
        if level > best_db:
            best, best_db = proc, level
    return best


def find_target_pid(process_names: tuple[str, ...] = ("chrome.exe",),
                    probe_seconds: float = 1.0) -> int:
    """找到正在渲染音频的目标 PID。

    C26：不能抓主进程。Chrome 主进程与音频服务子进程同名，唯一可靠的区分方式
    就是「是否出现在 enumerate_audio_processes() 里」。
    """
    procs = ProcessAudioCapture.enumerate_audio_processes()
    names = {n.lower() for n in process_names}
    cands = [p for p in procs if p.name.lower() in names]
    if not cands:
        raise CaptureError(
            f"没有进程在渲染音频：{sorted(names)}。"
            f"当前列表：{[(p.pid, p.name) for p in procs]}"
        )
    if len(cands) == 1:
        return cands[0].pid
    chosen = _pick_loudest(cands, probe_seconds)
    if chosen is None:
        # 全部候选都试听失败，退回第一个，由启动自检验证（spec §9）
        return cands[0].pid
    return chosen.pid


class ProcessAudioStream:
    """从指定进程捕获音频，输出 16kHz 单声道 float32 块。"""

    def __init__(self, pid: int, pipe_name: str = r"\\.\pipe\chrometrans",
                 buffer_bytes: int = 1 << 20, target_rate: int = 16000):
        self._pid = pid
        self._pipe_name = pipe_name
        self._buffer_bytes = buffer_bytes
        self._target_rate = target_rate
        self._handle: int | None = None
        self._cap: ProcessAudioCapture | None = None
        self._converter: PcmConverter | None = None
        self._pending = bytearray()
        self._buf = ctypes.create_string_buffer(buffer_bytes)
        self._got = ctypes.c_ulong(0)

    @property
    def is_started(self) -> bool:
        return self._handle is not None

    def start(self) -> None:
        handle = _k32.CreateNamedPipeW(
            self._pipe_name, PIPE_ACCESS_INBOUND,
            PIPE_TYPE_BYTE | PIPE_WAIT, PIPE_UNLIMITED_INSTANCES,
            self._buffer_bytes, self._buffer_bytes, 0, None)
        if not handle or handle == wintypes.HANDLE(-1).value:
            raise CaptureError(
                f"CreateNamedPipeW 失败：{ctypes.get_last_error()}")
        self._handle = handle

        try:
            # 先构造（构造不触碰管道），再挂起 ConnectNamedPipe。若构造抛异常，
            # 此时尚无挂起的 ConnectNamedPipe，stop() 的 CloseHandle 不会卡死。
            self._cap = ProcessAudioCapture(pid=self._pid, output_path=self._pipe_name)

            # 必须先挂起一个 ConnectNamedPipe，DLL 才连得上（spec §5.1）
            threading.Thread(
                target=_k32.ConnectNamedPipe,
                args=(wintypes.HANDLE(handle), None),
                daemon=True,
            ).start()

            self._cap.start()
        except Exception as exc:
            self.stop()
            raise CaptureError(f"启动捕获失败（DLL 可能拒绝了管道路径）：{exc}") from exc

    def read(self) -> np.ndarray:
        """阻塞读到一块音频，返回 16kHz 单声道 float32。可能是空数组（头部未收全时）。"""
        if self._handle is None:
            raise CaptureError("流未启动")
        ok = _k32.ReadFile(
            wintypes.HANDLE(self._handle), self._buf, self._buffer_bytes,
            ctypes.byref(self._got), None)
        if not ok:
            raise CaptureError(f"ReadFile 失败：{ctypes.get_last_error()}")
        data = self._buf.raw[:self._got.value]

        if self._converter is None:
            self._pending += data
            parsed = parse_wav_header(bytes(self._pending))
            if parsed is None:
                return np.zeros(0, dtype=np.float32)
            offset, fmt = parsed
            self._converter = PcmConverter(fmt, self._target_rate)
            data = bytes(self._pending)[offset:]
            self._pending = bytearray()
            if not data:
                return np.zeros(0, dtype=np.float32)

        return self._converter.convert(data)

    def stop(self) -> None:
        if self._cap is not None:
            try:
                self._cap.stop()
            except Exception:
                pass
            self._cap = None
        if self._handle is not None:
            _close_pipe(self._handle)
            self._handle = None

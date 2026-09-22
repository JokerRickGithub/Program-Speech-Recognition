"""捕获韧性：PID 重解析、管道重建、降级到系统级 loopback（spec §5.1 / §6 / §7）。"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Callable, Iterator

import numpy as np

from chrometrans.audio.capture import (
    AudioFormat,
    CaptureError,
    PcmConverter,
    ProcessAudioStream,
    find_target_pid,
)
from chrometrans.config import CaptureConfig


class SystemLoopbackStream:
    """系统级 WASAPI loopback —— 降级路径（spec §5.1）。

    **会混入其他程序的声音**。走 PortAudio，有正常的流式读取接口，但 C1 的
    静音期停流分歧在此路径上并未解决，故调用方必须对读取失败补静音。
    """

    def __init__(self, cfg: CaptureConfig):
        self._cfg = cfg
        self._pa = None
        self._stream = None
        self._converter: PcmConverter | None = None
        self._frames_per_buffer = 0
        self._pool = None            # 延迟到 start() 再建，避免空对象持有线程
        self._pending = None

    def start(self) -> None:
        import pyaudiowpatch as pyaudio

        self._pa = pyaudio.PyAudio()
        device = self._pa.get_default_wasapi_loopback()
        channels = min(2, int(device["maxInputChannels"]))
        rate = int(device["defaultSampleRate"])
        self._stream = self._pa.open(
            format=pyaudio.paFloat32, channels=channels, rate=rate,
            input=True, input_device_index=device["index"],
            frames_per_buffer=1024)
        self._frames_per_buffer = 1024
        self._converter = PcmConverter(AudioFormat(channels, rate, 32),
                                       self._cfg.target_sample_rate)
        self._pool = ThreadPoolExecutor(max_workers=1,
                                        thread_name_prefix="loopback-read")

    def read(self) -> np.ndarray:
        if self._stream is None or self._pool is None:
            raise CaptureError("流未启动")
        fut = self._pending
        if fut is not None and not fut.done():
            # 上一次读取还卡在 PortAudio 里：不能再派一个（线程数必须界住），
            # 直接当超时处理，让上层补静音
            raise CaptureError("降级路径读取超时（上一次仍未返回）")
        if fut is not None:
            self._pending = None
            try:
                return self._converter.convert(fut.result())
            except Exception as exc:
                raise CaptureError(f"降级路径读取失败：{exc}") from exc
        self._pending = self._pool.submit(
            self._stream.read, self._frames_per_buffer, False)
        try:
            data = self._pending.result(
                timeout=self._cfg.loopback_read_timeout_s)
        except FuturesTimeout as exc:
            # 故意保留 _pending：这次读取还卡在 PortAudio 里，下次进来要靠它认出
            # "上一次仍未返回"，否则会再派一个读，线程数就不止一个了
            raise CaptureError("降级路径读取超时") from exc
        except Exception as exc:
            self._pending = None        # 已完成且失败：清掉，让下次重新读
            raise CaptureError(f"降级路径读取失败：{exc}") from exc
        # 成功取回后必须清掉。留着它，下一次 read() 会走进上面 fut is not None
        # 的分支，把**同一块**数据再返回一遍 —— 输出会变成 A, A, B, B…
        self._pending = None
        return self._converter.convert(data)

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None


def check_signal(chunks: list[np.ndarray], min_rms: float = 1e-4) -> str:
    """启动自检：判断自检窗口里是否真的读到了声音（spec §9）。

    PID 选错时管道照常工作、只是永远读到零 —— 这是最隐蔽的失败模式，
    所以「没报错」不等于「抓对了」。
    """
    parts = [c for c in chunks if c.size]
    if not parts:
        return "no_data"
    audio = np.concatenate(parts)
    rms = float(np.sqrt(np.mean(audio ** 2)))
    return "ok" if rms >= min_rms else "silent"


class CaptureSource:
    """统一的音频来源：把「捕获失败」变成「重试或降级」，而不是崩溃。"""

    def __init__(self, cfg: CaptureConfig, on_event: Callable[[dict], None],
                 open_process_stream: Callable[[int], object] | None = None,
                 resolve_pid: Callable[[], int] | None = None,
                 open_loopback: Callable[[], object] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._cfg = cfg
        self._emit = on_event
        self._open_process = open_process_stream or self._default_open_process
        self._resolve_pid = resolve_pid or (
            lambda: find_target_pid(cfg.process_names, cfg.probe_seconds))
        self._open_loopback = open_loopback or (lambda: SystemLoopbackStream(cfg))
        self._sleep = sleep
        self._stopped = False
        self._failures = 0
        self.mode = "process"

    def stop(self) -> None:
        self._stopped = True

    # ---- 对外 ----

    def chunks(self) -> Iterator[np.ndarray]:
        while not self._stopped:
            if self.mode == "process":
                yield from self._process_chunks()
            else:
                yield from self._loopback_chunks()

    # ---- 按进程 ----

    def _process_chunks(self) -> Iterator[np.ndarray]:
        try:
            pid = self._resolve_pid()          # 每次重连都重新解析：PID 会变
        except CaptureError as exc:
            self._enter_loopback(f"找不到目标进程：{exc}")
            return

        stream = None
        broke_midway = False
        try:
            stream = self._open_process(pid)
            stream.start()
            self._failures = 0

            # 启动自检（spec §9）：先确认真的读到了有声音的音频，再进常规循环
            head: list[np.ndarray] = []
            try:
                while len(head) < self._cfg.self_check_chunks:
                    head.append(stream.read())
            except CaptureError:
                broke_midway = True

            if not broke_midway:
                verdict = check_signal(head, self._cfg.self_check_min_rms)
                if verdict != "ok":
                    what = "静音" if verdict == "silent" else "空数据"
                    self._emit({"event": "status", "data": {
                        "state": "warning",
                        "message": f"启动自检未通过（读到的是{what}）—— "
                                   f"确认 Chrome 正在播放？PID={pid}"}})
                self._emit({"event": "status", "data": {
                    "state": "running", "capture": "process", "pid": pid}})
                for chunk in head:
                    yield chunk
                while not self._stopped:
                    try:
                        yield stream.read()
                    except CaptureError:
                        broke_midway = True
                        break
        except Exception as exc:
            self._failures += 1
            self._emit({"event": "error", "data": {
                "message": f"按进程捕获失败（第 {self._failures} 次）：{exc}"}})
            self._maybe_degrade()
            return
        finally:
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass

        if broke_midway:
            self._failures += 1
            self._emit({"event": "error", "data": {
                "message": f"捕获流中断（第 {self._failures} 次），正在重连…"}})
        self._maybe_degrade()

    def _maybe_degrade(self) -> None:
        if self._failures >= self._cfg.max_reconnect_attempts:
            self._enter_loopback(
                f"连续 {self._failures} 次重连失败，降级到系统级捕获")
            return
        delay = min(
            self._cfg.reconnect_base_delay_s * (2 ** max(0, self._failures - 1)),
            self._cfg.reconnect_max_delay_s)
        self._sleep(delay)

    def _enter_loopback(self, reason: str) -> None:
        if self.mode == "loopback":
            return
        self.mode = "loopback"
        self._failures = 0
        # §5.1：降级后隔离失效。必须显式告知，否则用户以为隔离还在生效 ——
        # 这是整个设计里最危险的失败方式
        self._emit({"event": "status", "data": {
            "state": "degraded", "capture": "loopback",
            "message": f"已降级到系统级捕获：会混入 Edge、背景音乐等声音。"
                       f"原因：{reason}"}})

    def _default_open_process(self, pid: int) -> ProcessAudioStream:
        return ProcessAudioStream(
            pid=pid, pipe_name=self._cfg.pipe_name,
            buffer_bytes=self._cfg.read_buffer_bytes,
            target_rate=self._cfg.target_sample_rate)

    # ---- 降级路径 ----

    def _loopback_chunks(self) -> Iterator[np.ndarray]:
        stream = None
        try:
            stream = self._open_loopback()
            stream.start()
        except Exception as exc:
            self._emit({"event": "error", "data": {
                "message": f"降级路径也启动失败：{exc}"}})
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
            self._sleep(self._cfg.loopback_retry_delay_s)
            return

        try:
            while not self._stopped:
                try:
                    yield stream.read()
                except Exception:
                    # C1：底层无数据时补静音，别让切句器的时序卡死（spec §6）
                    yield np.zeros(self._cfg.silence_fill_samples,
                                   dtype=np.float32)
        finally:
            try:
                stream.stop()
            except Exception:
                pass

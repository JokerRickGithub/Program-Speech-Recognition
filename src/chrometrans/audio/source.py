"""捕获韧性：PID 重解析、管道重建、降级到系统级 loopback（spec §5.1 / §6 / §7）。"""
from __future__ import annotations

import threading
import time
from collections import deque
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

# 排空层里消费者等新块的轮询间隔。每次状态变化都会 notify，这只是防止漏通知
# 把消费者永久挂住的兜底，不是节拍。
_DRAIN_WAIT_S = 0.2


class DrainingStream:
    """在独立线程里把底层流持续读空，消费者按自己的节奏取。

    为什么必须有这一层（2026-09-25 实测）：整条流水线是**拉取式的单线程生成器链**
    —— engine.run → Segmenter → CaptureSource.chunks() → stream.read()。翻译一旦
    卡住（真实链路 40 次里 16 次超过 1 秒，最坏 17.16 秒），就没人读管道了，而命名
    管道缓冲 1 MB 只装得下 16 kHz×float32 的 **16.4 秒**音频：越过这条线 DLL 只能
    丢样本，「翻译慢」于是升级成「整句话没了」（2026-09-25 那两场会话里 17.7 / 18.7
    秒的空档就是这么来的；同一时段不翻译的中文会话一个空档都没有）。

    加了这一层，卡顿的代价从「音频丢失」变成「字幕迟到」。

    错误语义分两种，因为两条底流的可恢复性本来就不同：
    - 按进程（`silence_fill_samples=None`）：读到错就是致命的（管道关了），原样抛给
      消费者去走重连；
    - 降级路径（传了 `silence_fill_samples`）：读超时是可恢复的，补一块静音接着读
      —— 与 C1 原有的「底层停流就补静音，别让切句器时序卡死」逐字一致。
    """

    def __init__(self, stream, capacity_samples: int,
                 silence_fill_samples: int | None = None,
                 join_timeout_s: float = 2.0):
        self._inner = stream
        self._capacity = max(1, capacity_samples)
        self._silence_fill = silence_fill_samples
        self._join_timeout_s = join_timeout_s
        self._buf: deque[np.ndarray] = deque()
        self._samples = 0
        self._dropped = 0
        self._error: BaseException | None = None
        self._closed = False
        self._ready = threading.Condition(threading.Lock())
        # 收尾串行化：CaptureSource.stop() 与生成器的 finally 都会调 stop()。
        # 两次 CloseHandle 在句柄恰被复用时关掉的是别人的对象 —— 概率极低，
        # 代价极高，所以底层只许被停一次。
        self._teardown = threading.Lock()
        self._inner_stopped = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._inner.start()          # 启动失败照旧抛给调用方，由它决定降不降级
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name="chrometrans-drain")
        self._thread.start()

    def read(self) -> np.ndarray:
        """取一块。缓冲空则等到有块、出错或停止为止。"""
        with self._ready:
            while True:
                if self._buf:
                    # 先交已缓冲的块，再报错：底层断掉时手上这块音频不该跟着丢
                    chunk = self._buf.popleft()
                    self._samples -= chunk.size
                    return chunk
                if self._error is not None:
                    raise self._error
                if self._closed:
                    raise CaptureError("已停止")
                self._ready.wait(_DRAIN_WAIT_S)

    def take_dropped_samples(self) -> int:
        """取走并清零「因溢出丢掉的样本数」。

        由**消费者线程**调用并发事件：事件要往外发（stdout / WebSocket），从排空
        线程里发会让两块输出交错。
        """
        with self._ready:
            dropped, self._dropped = self._dropped, 0
        return dropped

    def stop(self) -> None:
        """可重入：CaptureSource.stop() 与生成器的 finally 都会调它。"""
        with self._ready:
            self._closed = True
            self._ready.notify_all()
        with self._teardown:
            if self._inner_stopped:
                return
            self._inner_stopped = True
            try:
                # CancelIoEx 会把阻塞在 ReadFile 里的那次读放掉 —— 没有这一步，
                # 排空线程醒了、消费线程却还卡在原来的阻塞读上，停止就还是挂起。
                self._inner.stop()
            except Exception:
                pass
            thread = self._thread
            if thread is not None:
                self._thread = None
                thread.join(timeout=self._join_timeout_s)

    # ---- 排空线程 ----

    def _pump(self) -> None:
        while True:
            with self._ready:
                if self._closed:
                    return
            try:
                chunk = self._inner.read()
            except Exception as exc:
                if self._silence_fill is not None and not self._closed:
                    self._put(np.zeros(self._silence_fill, dtype=np.float32))
                    continue
                with self._ready:
                    if not self._closed:
                        self._error = exc
                    self._ready.notify_all()
                return
            self._put(chunk)

    def _put(self, chunk: np.ndarray) -> None:
        with self._ready:
            if self._closed:
                return
            self._buf.append(chunk)
            self._samples += chunk.size
            # 溢出丢**最旧**的：用户要跟的是「现在」这节课，保最新的一段
            while self._samples > self._capacity and len(self._buf) > 1:
                old = self._buf.popleft()
                self._samples -= old.size
                self._dropped += old.size
            self._ready.notify_all()


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
        try:
            # 与上面那条分支同样地把转换失败归一成 CaptureError：read() 对外
            # 只承诺抛 CaptureError，漏出 soxr/numpy 的原始异常会让调用方的
            # 契约形同虚设
            return self._converter.convert(data)
        except Exception as exc:
            raise CaptureError(f"降级路径读取失败：{exc}") from exc

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
        self._drain: DrainingStream | None = None
        self.mode = "process"

    def stop(self) -> None:
        # 先置停止位再停排空层：反过来的话，消费者会先拿到「已停止」而此刻
        # _stopped 还是 False，于是这一次正常停止被当成断流报错并重连。
        self._stopped = True
        drain = self._drain
        if drain is not None:
            drain.stop()

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
            stream = self._wrap_drain(self._open_process(pid))
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
                        chunk = stream.read()
                    except CaptureError:
                        if self._stopped:
                            return          # 用户停的，不是断流（见 stop()）
                        broke_midway = True
                        break
                    self._report_audio_loss(stream)
                    yield chunk
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
            self._drain = None

        if self._stopped:
            return                      # 正常停止不该退避，也不该报成断流
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

    def _wrap_drain(self, stream, *,
                    silence_fill: int | None = None) -> DrainingStream:
        """给底层流套上排空层，并记下来给 stop() 用。

        容量按**秒**换算成样本：块大小并不固定（管道块约 10ms，补的静音是
        silence_fill_samples），按块数算容量会在两种块混流时失准。
        """
        drain = DrainingStream(
            stream,
            capacity_samples=int(self._cfg.drain_buffer_seconds
                                 * self._cfg.target_sample_rate),
            silence_fill_samples=silence_fill,
            join_timeout_s=self._cfg.drain_join_timeout_s)
        self._drain = drain
        return drain

    def _report_audio_loss(self, stream: DrainingStream) -> None:
        """缓冲溢出丢掉的音频必须可见。

        与 C45 要求「丢弃幻觉要报出来」同一个取向：不说的话，用户只看到字幕少了
        几句，而原因（翻译卡顿 / 机器跟不上）无处可查。只在**消费者线程**里发，
        事件要往外写 stdout 与 WebSocket，从排空线程发会让两块输出交错。
        """
        lost = stream.take_dropped_samples()
        if not lost:
            return
        self._emit({"event": "status", "data": {
            "state": "audio_dropped",
            "message": f"处理跟不上，已丢弃约 "
                       f"{lost / self._cfg.target_sample_rate:.1f} 秒音频"
                       f"（这几句字幕会缺）"}})

    # ---- 降级路径 ----

    def _loopback_chunks(self) -> Iterator[np.ndarray]:
        stream = None
        try:
            # 降级路径同样要排空：它也是拉取式的，消费者一卡住 PortAudio 的
            # 缓冲照样溢满丢音频。区别只在读超时可恢复，所以补静音接着读。
            stream = self._wrap_drain(self._open_loopback(),
                                      silence_fill=self._cfg.silence_fill_samples)
            stream.start()
        except Exception as exc:
            self._emit({"event": "error", "data": {
                "message": f"降级路径也启动失败：{exc}"}})
            self._drain = None
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
                    chunk = stream.read()
                except Exception:
                    if self._stopped:
                        return
                    # C1：底层无数据时补静音，别让切句器的时序卡死（spec §6）。
                    # 正常情况下排空层已经补过了，这里是它也没辙时的兜底。
                    chunk = np.zeros(self._cfg.silence_fill_samples,
                                     dtype=np.float32)
                self._report_audio_loss(stream)
                yield chunk
        finally:
            try:
                stream.stop()
            except Exception:
                pass
            self._drain = None

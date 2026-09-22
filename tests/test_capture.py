import struct
import threading
import time
from ctypes import wintypes

import numpy as np
import pytest

from chrometrans.audio.capture import (
    AudioFormat,
    CaptureError,
    PcmConverter,
    _close_pipe,
    parse_wav_header,
)

# IEEE float 子格式 GUID 00000003-0000-0010-8000-00AA00389B71
_FLOAT_GUID = struct.pack("<IHH", 3, 0, 0x0010) + bytes(
    [0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71])


def make_extensible_wav(channels=2, rate=48000, bits=32, payload=b"\x00" * 40):
    """构造 WAVE_FORMAT_EXTENSIBLE 头。实测 DLL 写出的头正是 68 字节。"""
    fmt_body = struct.pack(
        "<HHIIHH", 0xFFFE, channels, rate,
        rate * channels * bits // 8, channels * bits // 8, bits,
    ) + struct.pack("<H", 22) + struct.pack("<HI", bits, 0x3) + _FLOAT_GUID
    assert len(fmt_body) == 40
    body = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
    body += b"data" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body


def test_parse_wav_header_extracts_format():
    buf = make_extensible_wav()
    offset, fmt = parse_wav_header(buf)
    assert offset == 68                      # 12 RIFF + 48 fmt + 8 data
    assert fmt == AudioFormat(channels=2, sample_rate=48000, bits=32)


def test_parse_wav_header_returns_none_until_complete():
    """流式写入时头可能分多次到达，未收全必须返回 None 而不是抛异常。"""
    buf = make_extensible_wav()
    assert parse_wav_header(buf[:30]) is None
    assert parse_wav_header(buf[:60]) is None


def test_parse_wav_header_ignores_data_length_field():
    """data 长度字段在流式写入时不可信，解析只认偏移（spec §5.1）。"""
    buf = make_extensible_wav(payload=b"\x00" * 40)
    tampered = bytearray(buf)
    # 把 data 的长度字段改成 0xFFFFFFFF
    struct.pack_into("<I", tampered, 64, 0xFFFFFFFF)
    offset, fmt = parse_wav_header(bytes(tampered))
    assert offset == 68


def test_parse_wav_header_rejects_non_riff():
    assert parse_wav_header(b"not a wav file at all") is None


def test_pcm_converter_downmixes_stereo_to_mono():
    fmt = AudioFormat(channels=2, sample_rate=16000, bits=32)
    conv = PcmConverter(fmt, target_rate=16000)
    left = np.full(100, 0.5, dtype=np.float32)
    right = np.full(100, -0.5, dtype=np.float32)
    interleaved = np.empty(200, dtype=np.float32)
    interleaved[0::2] = left
    interleaved[1::2] = right
    out = conv.convert(interleaved.tobytes())
    assert out.dtype == np.float32
    assert np.allclose(out, 0.0, atol=1e-6)          # 左右反相 → 抵消


def test_pcm_converter_resamples_48k_to_16k():
    fmt = AudioFormat(channels=1, sample_rate=48000, bits=32)
    conv = PcmConverter(fmt, target_rate=16000)
    one_second = np.zeros(48000, dtype=np.float32)
    out = conv.convert(one_second.tobytes())
    # soxr 流式重采样器（HQ）有 ~341 样本的滤波器延迟（48k→16k 实测），块间
    # 不清空（C3），首块输出略短于 16000；这里只断言 3:1 比例正确。
    assert abs(len(out) - 16000) < 500


def test_pcm_converter_rejects_non_float32():
    fmt = AudioFormat(channels=2, sample_rate=48000, bits=16)
    with pytest.raises(CaptureError, match="float32"):
        PcmConverter(fmt, target_rate=16000)


def test_find_target_pid_raises_when_nothing_playing(monkeypatch):
    import chrometrans.audio.capture as cap

    # 真实 API 是 @classmethod enumerate_audio_processes(cls, dll_path=None)，
    # 故按 classmethod 打桩以匹配真实调用约定（brief 里误写为 staticmethod）。
    monkeypatch.setattr(
        cap.ProcessAudioCapture, "enumerate_audio_processes",
        classmethod(lambda cls, dll_path=None: []))
    with pytest.raises(CaptureError, match="没有进程在渲染音频"):
        cap.find_target_pid(("chrome.exe",))


def _make_stereo_interleaved(n_frames=48000) -> bytes:
    """构造左右声道取值不同且随时间变化的交错 float32 缓冲。

    左右用不同的时变信号，这样一旦跨块边界发生声道错位/丢样本，
    降混结果就会变，从而被 allclose 抓住（常量值会把错位掩盖成同一个均值）。
    """
    t = np.arange(n_frames, dtype=np.float32)
    left = np.sin(t * 0.1).astype(np.float32)
    right = np.cos(t * 0.1).astype(np.float32)
    interleaved = np.empty(n_frames * 2, dtype=np.float32)
    interleaved[0::2] = left
    interleaved[1::2] = right
    return interleaved.tobytes()


def test_pcm_converter_preserves_mid_frame_split():
    fmt = AudioFormat(channels=2, sample_rate=48000, bits=32)
    raw = _make_stereo_interleaved()

    whole = PcmConverter(fmt, target_rate=16000).convert(raw)

    conv = PcmConverter(fmt, target_rate=16000)
    # 12 字节 = 3 个 float32 样本，落在某一帧内部（跨帧边界切分）
    parts = np.concatenate([conv.convert(raw[:12]), conv.convert(raw[12:])])

    assert len(parts) == len(whole)
    assert np.allclose(parts, whole, atol=1e-6)


def test_pcm_converter_preserves_sub_sample_split():
    fmt = AudioFormat(channels=2, sample_rate=48000, bits=32)
    raw = _make_stereo_interleaved()

    whole = PcmConverter(fmt, target_rate=16000).convert(raw)

    conv = PcmConverter(fmt, target_rate=16000)
    # 14 字节不是 4 的倍数（半个样本），修复前 frombuffer 会直接 ValueError
    parts = np.concatenate([conv.convert(raw[:14]), conv.convert(raw[14:])])

    assert len(parts) == len(whole)
    assert np.allclose(parts, whole, atol=1e-6)


def test_invalid_pipe_name_rejected():
    import chrometrans.audio.capture as cap

    # 非 \\.\pipe\... 形式的名称会让 CreateNamedPipeW 返回 INVALID_HANDLE_VALUE
    stream = cap.ProcessAudioStream(pid=12345, pipe_name="not a pipe name")
    with pytest.raises(CaptureError):
        stream.start()
    assert stream.is_started is False


def test_constructor_failure_leaves_clean_stream(monkeypatch):
    import chrometrans.audio.capture as cap

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(cap, "ProcessAudioCapture", _Boom)
    stream = cap.ProcessAudioStream(
        pid=12345, pipe_name=r"\\.\pipe\chrometrans_boom")
    with pytest.raises(CaptureError):
        stream.start()
    assert stream.is_started is False


def test_close_pipe_does_not_hang_with_pending_connect():
    import chrometrans.audio.capture as cap

    pipe_name = rf"\\.\pipe\chrometrans_close_{time.time_ns()}"
    handle = cap._k32.CreateNamedPipeW(
        pipe_name,
        cap.PIPE_ACCESS_INBOUND,
        cap.PIPE_TYPE_BYTE | cap.PIPE_WAIT,
        cap.PIPE_UNLIMITED_INSTANCES,
        0, 0, 0, None,
    )
    assert handle != wintypes.HANDLE(-1).value

    # 后台线程发起同步 ConnectNamedPipe：没有客户端连接 → 永久挂起
    t = threading.Thread(
        target=cap._k32.ConnectNamedPipe,
        args=(wintypes.HANDLE(handle), None),
        daemon=True,
    )
    t.start()
    time.sleep(0.2)  # 让 ConnectNamedPipe 进入同步挂起状态

    start = time.monotonic()
    _close_pipe(handle)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"_close_pipe 阻塞了 {elapsed:.2f}s，CancelIoEx 未生效"
    t.join(timeout=2.0)
    assert not t.is_alive(), "挂起的 ConnectNamedPipe 未被取消，线程仍在阻塞"

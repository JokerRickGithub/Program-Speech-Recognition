import struct

import numpy as np
import pytest

from chrometrans.audio.capture import (
    AudioFormat,
    CaptureError,
    PcmConverter,
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

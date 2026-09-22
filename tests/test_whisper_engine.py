from types import SimpleNamespace

import numpy as np
import pytest

from chrometrans.asr.whisper_engine import (
    WhisperEngine,
    is_hallucination,
    to_absolute,
)
from chrometrans.config import AsrConfig


def _seg(start=3.0, end=6.0):
    from chrometrans.audio.segmenter import Segment
    return Segment(index=1, start=start, end=end,
                   audio=np.zeros(16000 * 3, dtype=np.float32))


def test_is_hallucination_flags_repetition():
    """C12：compression_ratio 高 = 重复低熵输出 = 幻觉。方向不能反。"""
    assert is_hallucination(no_speech_prob=0.1, avg_logprob=-0.3,
                            compression_ratio=3.0, cfg=AsrConfig()) is True


def test_is_hallucination_flags_silence():
    assert is_hallucination(no_speech_prob=0.9, avg_logprob=-1.5,
                            compression_ratio=1.0, cfg=AsrConfig()) is True


def test_is_hallucination_keeps_normal_speech():
    assert is_hallucination(no_speech_prob=0.05, avg_logprob=-0.25,
                            compression_ratio=1.4, cfg=AsrConfig()) is False


def test_to_absolute_offsets_relative_timestamps():
    """Whisper 返回段内相对时间，必须加 segment.start 还原。"""
    result = to_absolute(segment_start=3.0, seg_start=0.5, seg_end=2.5)
    assert result == (3.5, 5.5)


def test_transcribe_passes_vad_filter_false(monkeypatch):
    """C9：必须显式传 False，否则默认 True 会拆掉我们切好的边界。"""
    captured = {}

    class FakeModel:
        def transcribe(self, audio, **kwargs):
            captured.update(kwargs)
            info = SimpleNamespace(language="en", language_probability=0.99)
            return iter([]), info

    engine = WhisperEngine(AsrConfig())
    monkeypatch.setattr(engine, "_model", FakeModel())

    engine.transcribe(_seg())

    assert captured["vad_filter"] is False
    assert captured["language"] == "en"
    assert captured["condition_on_previous_text"] is False


def test_transcribe_returns_none_when_filtered(monkeypatch):
    class FakeModel:
        def transcribe(self, audio, **kwargs):
            seg = SimpleNamespace(start=0.0, end=1.0, text="  repeated  ",
                                  no_speech_prob=0.95, avg_logprob=-1.8,
                                  compression_ratio=1.0, words=[])
            info = SimpleNamespace(language="en", language_probability=0.99)
            return iter([seg]), info

    engine = WhisperEngine(AsrConfig())
    monkeypatch.setattr(engine, "_model", FakeModel())

    assert engine.transcribe(_seg()) is None


def test_transcribe_joins_multiple_whisper_segments(monkeypatch):
    class FakeModel:
        def transcribe(self, audio, **kwargs):
            segs = [
                SimpleNamespace(start=0.0, end=1.0, text=" The derivative",
                                no_speech_prob=0.05, avg_logprob=-0.2,
                                compression_ratio=1.2, words=[]),
                SimpleNamespace(start=1.0, end=2.5, text=" is continuous.",
                                no_speech_prob=0.05, avg_logprob=-0.2,
                                compression_ratio=1.2, words=[]),
            ]
            info = SimpleNamespace(language="en", language_probability=0.99)
            return iter(segs), info

    engine = WhisperEngine(AsrConfig())
    monkeypatch.setattr(engine, "_model", FakeModel())

    utt = engine.transcribe(_seg(start=3.0))
    assert utt.text == "The derivative is continuous."
    assert utt.start == 3.0
    assert utt.end == 5.5


def test_transcribe_returns_none_on_empty_text(monkeypatch):
    class FakeModel:
        def transcribe(self, audio, **kwargs):
            info = SimpleNamespace(language="en", language_probability=0.99)
            return iter([]), info

    engine = WhisperEngine(AsrConfig())
    monkeypatch.setattr(engine, "_model", FakeModel())
    assert engine.transcribe(_seg()) is None

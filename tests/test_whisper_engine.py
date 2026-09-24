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

    result = engine.transcribe(_seg())

    assert result.utterance is None
    assert [d.text for d in result.dropped] == ["repeated"]


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

    utt = engine.transcribe(_seg(start=3.0)).utterance
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
    assert engine.transcribe(_seg()).utterance is None


def test_transcribe_keeps_good_segments_when_some_are_hallucinated(monkeypatch):
    """C12：幻觉段跳过，好段保留 —— 不是全有全无。"""
    class FakeModel:
        def transcribe(self, audio, **kwargs):
            segs = [
                SimpleNamespace(start=0.0, end=1.0, text="good line",
                                no_speech_prob=0.1, avg_logprob=-0.2,
                                compression_ratio=1.2, words=[]),
                SimpleNamespace(start=1.0, end=2.0, text="反复反复反复",
                                no_speech_prob=0.1, avg_logprob=-0.2,
                                compression_ratio=3.0, words=[]),
            ]
            info = SimpleNamespace(language="en", language_probability=0.99)
            return iter(segs), info

    engine = WhisperEngine(AsrConfig())
    monkeypatch.setattr(engine, "_model", FakeModel())

    utt = engine.transcribe(_seg(start=3.0)).utterance
    assert utt is not None
    assert utt.text == "good line"
    assert "反复" not in utt.text


def _model_with(segs, captured: dict | None = None):
    """假模型。captured 非空时把 transcribe 收到的 kwargs 记进去。"""
    class FakeModel:
        def transcribe(self, audio, **kwargs):
            if captured is not None:
                captured.update(kwargs)
            info = SimpleNamespace(language="en", language_probability=0.99)
            return iter(segs), info

    return FakeModel()


def _wseg(text, nsp=0.05, alp=-0.2, cr=1.2, start=0.0, end=1.0):
    """一个 faster-whisper 输出段该有的形状。"""
    return SimpleNamespace(start=start, end=end, text=text,
                           no_speech_prob=nsp, avg_logprob=alp,
                           compression_ratio=cr, words=[])


def test_transcribe_reports_dropped_segments_with_their_text(monkeypatch):
    """C45：丢弃必须带着文本回来。

    只说「丢了 1 段」用户判断不了那是垃圾还是真话 —— 而判断正是这个事件的全部
    目的。三个统计量一并带回，未来的界面才不用再改一次签名。
    """
    segs = [_wseg("good line"),
            _wseg("反复反复反复", cr=3.0, start=1.0, end=2.0)]
    engine = WhisperEngine(AsrConfig())
    monkeypatch.setattr(engine, "_model", _model_with(segs))

    result = engine.transcribe(_seg(start=3.0))

    assert result.utterance.text == "good line"
    assert len(result.dropped) == 1
    assert result.dropped[0].text == "反复反复反复"
    assert result.dropped[0].compression_ratio == 3.0


def test_dropped_text_is_never_empty(monkeypatch):
    """空白段不是一种「判断」，不该占一条事件。"""
    segs = [_wseg("   ", nsp=0.95, alp=-1.8)]
    engine = WhisperEngine(AsrConfig())
    monkeypatch.setattr(engine, "_model", _model_with(segs))

    result = engine.transcribe(_seg())

    assert result.utterance is None
    assert result.dropped == ()


def test_initial_prompt_is_passed_through(monkeypatch):
    """C46：zh 靠提示词定向简体；en / ru 传 None。"""
    captured = {}
    engine = WhisperEngine(AsrConfig(initial_prompt="以下是普通话的句子。"))
    monkeypatch.setattr(engine, "_model", _model_with([], captured))

    engine.transcribe(_seg())

    assert captured["initial_prompt"] == "以下是普通话的句子。"

    captured = {}
    engine = WhisperEngine(AsrConfig())
    monkeypatch.setattr(engine, "_model", _model_with([], captured))
    engine.transcribe(_seg())
    assert captured["initial_prompt"] is None


def test_hallucination_thresholds_come_from_cfg_not_from_constants(monkeypatch):
    """同一段输出，严阈值丢、宽阈值留 —— 锁住「分语言阈值确实生效」。

    用两个手工构造的 AsrConfig 而不是两个 profile：这条要验的是 whisper_engine
    没有硬编码阈值（profile 与 cfg 的对应关系在 test_config.py 里锁）。
    """
    segs = [_wseg("也许是人声", nsp=0.7, alp=-1.2, cr=1.5)]

    strict = WhisperEngine(AsrConfig())                       # 0.6 / -1.0
    monkeypatch.setattr(strict, "_model", _model_with(segs))
    assert strict.transcribe(_seg()).utterance is None, "en 阈值下应当被丢弃"

    loose = WhisperEngine(AsrConfig(language="ru",
                                    no_speech_prob_threshold=0.9,
                                    avg_logprob_threshold=-1.6))
    monkeypatch.setattr(loose, "_model", _model_with(segs))
    assert loose.transcribe(_seg()).utterance is not None, "宽阈值下应当保留"

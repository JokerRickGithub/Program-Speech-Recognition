import numpy as np
import pytest

from chrometrans.audio.segmenter import Segmenter, normalize, pick_cut_point
from chrometrans.config import SegmenterConfig

SR = 16000


class FakeVad:
    """可脚本化的假 VAD：按帧给出语音概率。

    真 VAD 行为不可控，切句规则必须能在它之上被确定性地测试。
    """

    def __init__(self, probs):
        self._probs = list(probs)
        self._i = 0

    def __call__(self, frame, sample_rate):
        p = self._probs[self._i] if self._i < len(self._probs) else 0.0
        self._i += 1
        return p


def speech(n):
    return [0.9] * n


def silence(n):
    return [0.05] * n


def _frames(n):
    return np.zeros(n * 512, dtype=np.float32)


def _cut_probs(pattern):
    """构造 pick_cut_point 的输入：[(绝对样本位, 概率), ...]"""
    return [(i * 512, p) for i, p in enumerate(pattern)]


def test_normalize_leaves_silence_untouched():
    """C27 的核心：静音块绝不能被放大，否则底噪满幅会诱发 Whisper 幻觉。"""
    silence = np.zeros(1000, dtype=np.float32)
    out = normalize(silence, target_rms=0.05, max_gain=20.0, floor_rms=1e-5)
    assert np.array_equal(out, silence)


def test_normalize_boosts_quiet_speech_toward_target():
    # 0.005 的 rms 需要 10 倍增益才到 0.05，未触及 max_gain=20
    quiet = np.full(1000, 0.005, dtype=np.float32)
    out = normalize(quiet, target_rms=0.05, max_gain=20.0, floor_rms=1e-5)
    assert float(np.sqrt(np.mean(out ** 2))) == pytest.approx(0.05, rel=1e-3)


def test_normalize_respects_max_gain():
    very_quiet = np.full(1000, 1e-4, dtype=np.float32)
    out = normalize(very_quiet, target_rms=0.05, max_gain=3.0, floor_rms=1e-6)
    assert np.allclose(out, very_quiet * 3.0)


def test_normalize_does_not_attenuate_loud_audio():
    loud = np.full(1000, 0.5, dtype=np.float32)
    out = normalize(loud, target_rms=0.05, max_gain=20.0, floor_rms=1e-5)
    assert np.allclose(out, loud)


def test_pick_cut_point_finds_longest_low_probability_run():
    """spec §5.2：超长段在「最长低概率连续段」的中点切。"""
    pattern = [0.9] * 10 + [0.1] * 10 + [0.9] * 10
    assert pick_cut_point(_cut_probs(pattern), 512, SR) == 15 * 512


def test_pick_cut_point_returns_none_for_unbroken_speech():
    """无可用间隙 → 返回 None，调用方硬切。"""
    assert pick_cut_point(_cut_probs([0.9] * 100), 512, SR) is None


def test_pick_cut_point_ignores_runs_shorter_than_98ms():
    """3 帧 = 96 ms < 98 ms，间隙太短，切了会切在词中间。"""
    pattern = [0.9] * 10 + [0.1] * 3 + [0.9] * 20
    assert pick_cut_point(_cut_probs(pattern), 512, SR) is None


def test_pick_cut_point_prefers_the_longest_run():
    pattern = [0.9] * 5 + [0.1] * 5 + [0.9] * 5 + [0.1] * 9 + [0.9] * 5
    assert pick_cut_point(_cut_probs(pattern), 512, SR) == 19 * 512


def test_drops_segment_shorter_than_min_speech():
    """350ms 以下的语音段丢弃（C5：库无此参数，必须自己判）。"""
    cfg = SegmenterConfig(min_speech_ms=350)
    # 5 帧语音 = 2560 样本 = 160ms < 350ms
    seg = Segmenter(cfg, vad=FakeVad(speech(5) + silence(35)))
    assert seg.feed(_frames(40)) == []


def test_emits_segment_after_silence_break():
    cfg = SegmenterConfig(min_speech_ms=350, silence_break_ms=600)
    # 20 帧语音（640ms）后接 30 帧静音（960ms > 600ms）
    seg = Segmenter(cfg, vad=FakeVad(speech(20) + silence(30)))
    out = seg.feed(_frames(50))

    assert len(out) == 1
    assert out[0].index == 1
    assert out[0].start == 0.0
    assert out[0].end == pytest.approx(0.64)      # 10240 样本 / 16000
    assert out[0].audio.size == 10240
    assert out[0].audio.dtype == np.float32


def test_trailing_silence_is_not_included_in_the_segment():
    """段的音频止于最后一个语音帧，不带 600ms 静音尾巴 —— 喂给 Whisper 越干净越好。"""
    cfg = SegmenterConfig(min_speech_ms=350, silence_break_ms=600)
    seg = Segmenter(cfg, vad=FakeVad(speech(20) + silence(30)))
    out = seg.feed(_frames(50))
    assert out[0].audio.size == out[0].end * SR


def test_hysteresis_keeps_speech_through_a_probability_dip():
    """C7 迟滞：已进入语音后，概率掉到 0.4（> neg_threshold）不应断句。"""
    cfg = SegmenterConfig(min_speech_ms=350, silence_break_ms=600)
    seg = Segmenter(cfg, vad=FakeVad(speech(20) + [0.4] * 20 + silence(20)))
    out = seg.feed(_frames(60))
    assert len(out) == 1
    assert out[0].end == pytest.approx(20480 / SR)


def test_forced_break_before_thirty_seconds():
    """C6：段长必须 < 30s，本设计强制 15s。"""
    cfg = SegmenterConfig(max_segment_s=15.0, silence_break_ms=600)
    total_frames = int(20 * SR / 512)              # 20 秒，全程语音
    seg = Segmenter(cfg, vad=FakeVad(speech(total_frames)))
    out = seg.feed(_frames(total_frames))

    assert out, "长语音必须被强断，不能憋着"
    for s in out:
        assert s.audio.size <= cfg.max_segment_s * SR, \
            f"段长超限：{s.audio.size / SR:.2f}s"


def test_forced_break_uses_low_probability_gap_when_available():
    """有低概率间隙时，切点应落在间隙里而不是硬切在 15s 整。"""
    cfg = SegmenterConfig(max_segment_s=15.0, silence_break_ms=3600)
    frames = int(20 * SR / 512)
    pattern = speech(frames)
    for i in range(400, 410):                      # 400..409 帧处开一个 320ms 缺口
        pattern[i] = 0.1                             # 400*512/16000 = 12.8s
    seg = Segmenter(cfg, vad=FakeVad(pattern))
    out = seg.feed(_frames(frames))

    assert out
    assert out[0].end == pytest.approx(405 * 512 / SR), "切点应落在缺口中点（12.96s）"


def test_flush_emits_trailing_speech():
    cfg = SegmenterConfig(min_speech_ms=350)
    seg = Segmenter(cfg, vad=FakeVad(speech(20)))  # 语音后直接结束，没有静音
    seg.feed(_frames(20))
    assert len(seg.flush()) == 1


def test_flush_on_empty_stream_is_safe():
    seg = Segmenter(SegmenterConfig(), vad=FakeVad([]))
    assert seg.flush() == []


def test_chunk_boundaries_do_not_lose_samples():
    """分多次喂入的结果必须与一次喂入一致 —— 块边界不能丢样本。"""
    probs = speech(20) + silence(25)
    cfg = SegmenterConfig(min_speech_ms=350, silence_break_ms=600)

    a = Segmenter(cfg, vad=FakeVad(probs)).feed(_frames(45))

    piecewise = Segmenter(cfg, vad=FakeVad(probs))
    b = []
    for _ in range(45):
        b.extend(piecewise.feed(_frames(1)))

    assert len(a) == len(b) == 1
    assert np.allclose(a[0].audio, b[0].audio)
    assert a[0].start == b[0].start and a[0].end == b[0].end

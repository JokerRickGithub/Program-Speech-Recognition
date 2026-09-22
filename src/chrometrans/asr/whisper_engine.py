"""faster-whisper 封装（spec §5.3）。"""
from __future__ import annotations

import os
import site
from dataclasses import dataclass
from pathlib import Path

from chrometrans.audio.segmenter import Segment
from chrometrans.config import AsrConfig


@dataclass(frozen=True)
class Utterance:
    start: float          # 秒，绝对（相对捕获开始）
    end: float
    text: str


def prepare_cuda_paths() -> None:
    """C14：pip 装的 nvidia-cublas-cu12 不自动进 PATH，首次推理会崩。

    os.add_dll_directory 无效，必须前置 PATH。
    """
    candidates = []
    for base in {*site.getsitepackages(), site.getusersitepackages()}:
        p = Path(base) / "nvidia" / "cublas" / "bin"
        if p.is_dir():
            candidates.append(p)
    if not candidates:
        return
    prefix = os.pathsep.join(str(p) for p in candidates)
    os.environ["PATH"] = prefix + os.pathsep + os.environ.get("PATH", "")


def is_hallucination(no_speech_prob: float, avg_logprob: float,
                     compression_ratio: float, cfg: AsrConfig) -> bool:
    """C12：方向不能反。

    - compression_ratio 高 = 重复低熵输出 = 幻觉特征
    - no_speech_prob 高 且 avg_logprob 低 = 静音/噪声上的凭空输出
    """
    if compression_ratio > cfg.compression_ratio_threshold:
        return True
    if (no_speech_prob > cfg.no_speech_prob_threshold
            and avg_logprob < cfg.avg_logprob_threshold):
        return True
    return False


def to_absolute(segment_start: float, seg_start: float,
                seg_end: float) -> tuple[float, float]:
    """Whisper 返回段内相对时间，加 segment.start 还原为绝对时间。"""
    return (segment_start + seg_start, segment_start + seg_end)


class WhisperEngine:
    def __init__(self, cfg: AsrConfig):
        self._cfg = cfg
        self._model = None

    def load(self) -> None:
        from faster_whisper import WhisperModel

        prepare_cuda_paths()
        self._model = WhisperModel(self._cfg.model, device=self._cfg.device,
                                   compute_type=self._cfg.compute_type)

    def transcribe(self, segment: Segment) -> Utterance | None:
        if self._model is None:
            raise RuntimeError("WhisperEngine.load() 尚未调用")

        whisper_segments, _info = self._model.transcribe(
            segment.audio,
            language=self._cfg.language,
            task="transcribe",
            vad_filter=False,                     # ★ C9：必须显式传
            word_timestamps=self._cfg.word_timestamps,
            condition_on_previous_text=False,
            beam_size=self._cfg.beam_size,
        )

        texts: list[str] = []
        first_start: float | None = None
        last_end: float | None = None

        for seg in whisper_segments:
            if is_hallucination(seg.no_speech_prob, seg.avg_logprob,
                                seg.compression_ratio, self._cfg):
                continue                              # C12：跳过幻觉段，不中断整段
            text = seg.text.strip()
            if not text:
                continue
            abs_start, abs_end = to_absolute(segment.start, seg.start, seg.end)
            if first_start is None:
                first_start = abs_start
            last_end = abs_end
            texts.append(text)

        if not texts or first_start is None or last_end is None:
            return None

        return Utterance(start=first_start, end=last_end,
                         text=" ".join(texts))

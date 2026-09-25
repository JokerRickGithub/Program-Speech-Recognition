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


@dataclass(frozen=True)
class Dropped:
    """一段命中幻觉过滤、被丢弃的 Whisper 输出（C45：丢弃必须可见）。"""
    text: str
    no_speech_prob: float
    avg_logprob: float
    compression_ratio: float


@dataclass(frozen=True)
class TranscribeResult:
    """一次 transcribe 的全部产出。

    被丢弃的段要带回去：判定只有这里做得了（三个统计量在这里），而事件总线在
    Engine 手里（spec §5.4）。ASR 层报事实，Engine 决定发什么事件 —— 维持既有
    的分工，不让 WhisperEngine 长出对事件格式的认知。
    """
    utterance: Utterance | None
    dropped: tuple[Dropped, ...] = ()


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


def transcribe_kwargs(cfg: AsrConfig) -> dict:
    """生产与标定共用的解码参数。

    spec §5.5 要求标定脚本跑的是同一条参数路径 —— 参数不一致时，标出来的阈值
    是给另一个解码配置调的，线上并不适用。
    """
    return {
        "language": cfg.language,
        "task": "transcribe",
        "vad_filter": False,                  # C9：必须显式传
        "word_timestamps": cfg.word_timestamps,
        "condition_on_previous_text": False,
        "beam_size": cfg.beam_size,
        "initial_prompt": cfg.initial_prompt,
    }


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

    def transcribe(self, segment: Segment) -> TranscribeResult:
        if self._model is None:
            raise RuntimeError("WhisperEngine.load() 尚未调用")

        whisper_segments, _info = self._model.transcribe(
            segment.audio, **transcribe_kwargs(self._cfg))

        texts: list[str] = []
        dropped: list[Dropped] = []
        first_start: float | None = None
        last_end: float | None = None

        for seg in whisper_segments:
            text = seg.text.strip()
            if is_hallucination(seg.no_speech_prob, seg.avg_logprob,
                                seg.compression_ratio, self._cfg):
                # C12：跳过幻觉段，不中断整段。C45：但要留痕 —— 空白段不留，
                # 那不是一种「判断」。
                if text:
                    dropped.append(Dropped(
                        text=text,
                        no_speech_prob=seg.no_speech_prob,
                        avg_logprob=seg.avg_logprob,
                        compression_ratio=seg.compression_ratio))
                continue
            if not text:
                continue
            abs_start, abs_end = to_absolute(segment.start, seg.start, seg.end)
            if first_start is None:
                first_start = abs_start
            last_end = abs_end
            texts.append(text)

        if not texts or first_start is None or last_end is None:
            return TranscribeResult(utterance=None, dropped=tuple(dropped))

        return TranscribeResult(
            utterance=Utterance(start=first_start, end=last_end,
                                text=" ".join(texts)),
            dropped=tuple(dropped))

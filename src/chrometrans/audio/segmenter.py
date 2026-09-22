"""在线切句（spec §5.2）。

四条规则：最小语音 350ms / 最大段长 15s / 断句静音 600ms / 帧长 512 样本。

**为什么不直接用 VADIterator**：它不暴露每帧概率，而超长段的切点规则
（「最长低概率连续段」）需要概率。故直接调 silero 模型取概率，自己做迟滞判定。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np

from chrometrans.config import SegmenterConfig


def normalize(chunk: np.ndarray, target_rms: float = 0.05,
              max_gain: float = 20.0, floor_rms: float = 1e-5) -> np.ndarray:
    """缩放到目标电平。

    C27：Chrome 的电平波动实测可达 20 倍，固定增益必然失配 —— 必须自适应。
    但静音块原样返回：把底噪放大到满幅正是历史上诱发 Whisper 幻觉的原因。
    只放大，不衰减。
    """
    chunk = np.asarray(chunk, dtype=np.float32)
    if chunk.size == 0:
        return chunk
    rms = float(np.sqrt(np.mean(chunk ** 2)))
    if rms < floor_rms:
        return chunk
    gain = min(target_rms / rms, max_gain)
    if gain <= 1.0:
        return chunk
    return chunk * gain


def pick_cut_point(probs: list[tuple[int, float]], frame_samples: int,
                   sample_rate: int, threshold: float = 0.35,
                   min_gap_s: float = 0.098) -> int | None:
    """在「最长的低概率连续段」中点挑切点（spec §5.2）。

    probs: [(帧起始绝对样本位, 语音概率), ...]，按时间升序。
    没有长度 > min_gap_s 的低概率段时返回 None —— 调用方回退到硬切。
    """
    min_frames = math.ceil(min_gap_s * sample_rate / frame_samples)
    best_len = 0
    best_cut: int | None = None

    run_start: int | None = None
    for i, (_pos, p) in enumerate(probs):
        if p < threshold:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                length = i - run_start
                if length >= min_frames and length > best_len:
                    best_len = length
                    best_cut = probs[run_start + length // 2][0]
            run_start = None

    if run_start is not None:
        length = len(probs) - run_start
        if length >= min_frames and length > best_len:
            best_cut = probs[run_start + length // 2][0]

    return best_cut


def _load_silero() -> Callable[[np.ndarray, int], float]:
    from silero_vad import load_silero_vad

    return load_silero_vad()


@dataclass
class Segment:
    """一段语音。audio 为 16kHz 单声道 float32，长度 ≤ 15s。"""

    index: int
    start: float          # 秒，相对捕获开始
    end: float
    audio: np.ndarray


class Segmenter:
    """把连续的 16kHz 音频流切成语音段。"""

    def __init__(self, cfg: SegmenterConfig,
                 vad: Callable[[np.ndarray, int], float] | None = None):
        self._cfg = cfg
        self._vad = vad if vad is not None else _load_silero()
        self._sr = cfg.sample_rate
        self._frame = cfg.frame_samples
        self._min_speech = int(cfg.min_speech_ms * cfg.sample_rate / 1000)
        self._silence_break = int(cfg.silence_break_ms * cfg.sample_rate / 1000)
        self._max_segment = int(cfg.max_segment_s * cfg.sample_rate)

        self._vad_rest = np.zeros(0, dtype=np.float32)   # 待切成帧的样本
        self._tape = np.zeros(0, dtype=np.float32)       # 段音频缓冲
        self._tape_origin = 0                            # tape[0] 的绝对样本位
        self._processed = 0                              # 已切成帧的样本总数

        self._in_speech = False
        self._speech_start: int | None = None
        self._last_speech_end: int | None = None
        self._silence_run = 0
        self._probs: list[tuple[int, float]] = []
        self._index = 0

    # ---- 对外接口 ----

    def feed(self, chunk: np.ndarray) -> list[Segment]:
        chunk = np.asarray(chunk, dtype=np.float32)
        if chunk.size == 0:
            return []
        self._tape = np.concatenate([self._tape, chunk])
        self._vad_rest = np.concatenate([self._vad_rest, chunk])

        out: list[Segment] = []
        while self._vad_rest.size >= self._frame:
            frame = self._vad_rest[:self._frame]
            self._vad_rest = self._vad_rest[self._frame:]
            frame_start = self._processed
            self._processed += self._frame
            self._on_frame(frame, frame_start, out)
        return out

    def flush(self) -> list[Segment]:
        """流结束时把最后一段语音吐出来。"""
        out: list[Segment] = []
        if self._in_speech:
            self._close(self._processed, out)
        return out

    # ---- 内部 ----

    def _on_frame(self, frame: np.ndarray, frame_start: int,
                  out: list[Segment]) -> None:
        prob = float(self._vad(frame, self._sr))

        # 迟滞：进入语音用高门限，退出用低门限，避免在门限附近反复开合
        threshold = (self._cfg.neg_threshold if self._in_speech
                     else self._cfg.speech_threshold)
        speech = prob > threshold
        frame_end = frame_start + self._frame

        if not self._in_speech:
            if not speech:
                self._drop_before(frame_start)   # 纯静音：别让 tape 无界增长
                return
            self._in_speech = True
            self._speech_start = frame_start
            self._probs = []

        # 语音段内每一帧都记录概率，供超长段挑切点用
        self._probs.append((frame_start, prob))

        if not speech:
            self._silence_run += self._frame
            if self._silence_run >= self._silence_break:
                self._close(frame_end, out)
            return

        self._last_speech_end = frame_end
        self._silence_run = 0

        # C6：必须在送进 ASR 之前强断，且 < 30s
        if self._last_speech_end - self._speech_start >= self._max_segment:
            self._force_break(out)

    def _force_break(self, out: list[Segment]) -> None:
        """段长触顶。优先切在低概率间隙里，没有间隙才硬切。"""
        start = self._speech_start
        hard = start + self._max_segment
        cut = pick_cut_point(self._probs, self._frame, self._sr,
                             threshold=self._cfg.neg_threshold,
                             min_gap_s=self._cfg.cut_min_gap_ms / 1000)
        end_abs = cut if (cut is not None and start < cut <= hard) else hard
        self._close(end_abs, out, audio_end=end_abs, keep_speech=True)

    def _close(self, end_abs: int, out: list[Segment],
               audio_end: int | None = None, keep_speech: bool = False) -> None:
        """收束当前段并发射。

        `end_abs` 是处理进度（用于丢弃已消费的音频），`audio_end` 是实际发出的
        音频末端 —— 两者不同：静音断句时进度已越过 600ms 静音，但音频止于最后一个
        语音帧（不给 Whisper 喂静音尾巴）。
        """
        start_abs = self._speech_start
        audio_end = self._last_speech_end if audio_end is None else audio_end

        self._in_speech = keep_speech
        self._speech_start = end_abs if keep_speech else None
        self._last_speech_end = end_abs if keep_speech else None
        self._silence_run = 0
        self._probs = []

        if start_abs is not None:
            audio_end = min(audio_end, start_abs + self._max_segment)
            # 最短语音判定针对「语音时长」，不是「段时长」—— 否则长静音尾巴会
            # 让一个 160ms 的噪声片段混过 350ms 门限
            if audio_end - start_abs >= self._min_speech:
                lo = start_abs - self._tape_origin
                hi = audio_end - self._tape_origin
                audio = self._tape[max(0, lo):hi]
                if audio.size:
                    self._index += 1
                    out.append(Segment(
                        index=self._index,
                        start=start_abs / self._sr,
                        end=audio_end / self._sr,
                        audio=normalize(audio, self._cfg.norm_target_rms,
                                        self._cfg.norm_max_gain,
                                        self._cfg.norm_floor_rms),
                    ))

        self._drop_before(end_abs)

    def _drop_before(self, absolute_pos: int) -> None:
        """丢掉已消费的音频，避免长时运行内存无界增长。"""
        keep_from = absolute_pos - self._tape_origin
        if keep_from > 0:
            self._tape = self._tape[keep_from:]
            self._tape_origin = absolute_pos

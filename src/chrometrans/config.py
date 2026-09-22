"""全部可调参数集中于此（spec §5.8）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CaptureConfig:
    process_names: tuple[str, ...] = ("chrome.exe",)
    pipe_name: str = r"\\.\pipe\chrometrans"
    pipe_buffer_bytes: int = 1 << 20
    read_buffer_bytes: int = 1 << 20
    target_sample_rate: int = 16000
    probe_seconds: float = 1.0        # 多候选消歧时每个候选的试听时长
    # 韧性与降级（spec §5.1 / §6，Task 12 使用）
    max_reconnect_attempts: int = 5
    reconnect_base_delay_s: float = 1.0
    reconnect_max_delay_s: float = 30.0
    loopback_retry_delay_s: float = 5.0
    silence_fill_samples: int = 512   # C1：底层停流时补的静音块大小
    # 启动自检（spec §9）：管道块约 10ms，200 块 ≈ 2 秒
    self_check_chunks: int = 200
    self_check_min_rms: float = 1e-4


@dataclass(frozen=True)
class SegmenterConfig:
    sample_rate: int = 16000
    frame_samples: int = 512          # C7：固定，不可变
    min_speech_ms: int = 350
    max_segment_s: float = 15.0       # C6：必须 < 30s
    silence_break_ms: int = 600
    speech_threshold: float = 0.5     # 概率高于此值 → 进入语音
    neg_threshold: float = 0.35       # 低于此值 → 退出语音（迟滞，防边界抖动）
    cut_min_gap_ms: int = 98          # 超长段切点：最短可用的低概率间隙（§5.2）
    # C27：归一化必须带静音门限
    norm_target_rms: float = 0.05
    norm_max_gain: float = 20.0
    norm_floor_rms: float = 1e-5


@dataclass(frozen=True)
class AsrConfig:
    model: str = "large-v3-turbo"
    device: str = "cuda"
    compute_type: str = "int8_float16"
    language: str = "en"
    beam_size: int = 5
    word_timestamps: bool = True
    # C12：幻觉过滤阈值
    no_speech_prob_threshold: float = 0.6
    avg_logprob_threshold: float = -1.0
    compression_ratio_threshold: float = 2.4


@dataclass(frozen=True)
class TranslateConfig:
    src: str = "en"
    tgt: str = "zh-Hans"
    azure_key: str | None = None
    azure_region: str = "global"
    google_key: str | None = None
    timeout_s: float = 10.0
    max_retries: int = 3              # C19：传输错误有界重试
    retry_base_delay_s: float = 0.5


@dataclass(frozen=True)
class OutputConfig:
    output_root: Path = Path("transcripts")
    srt_rerender_every: int = 20
    replace_retries: int = 5          # C23
    replace_base_delay_s: float = 0.2


@dataclass(frozen=True)
class Config:
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    translate: TranslateConfig = field(default_factory=TranslateConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


def load_config() -> Config:
    """从环境变量读取凭据，其余走默认值。"""
    translate = TranslateConfig(
        azure_key=os.environ.get("AZURE_TRANSLATOR_KEY"),
        azure_region=os.environ.get("AZURE_TRANSLATOR_REGION", "global"),
        google_key=os.environ.get("GOOGLE_TRANSLATE_KEY"),
    )
    return Config(translate=translate)

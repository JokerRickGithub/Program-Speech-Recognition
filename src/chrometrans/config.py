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
    loopback_read_timeout_s: float = 2.0   # C1：PortAudio 可能永久阻塞，必须超时
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
class LanguageProfile:
    """一门音频语言的完整画像（spec §5.1）。

    三个语言代码字段刻意不合并：en / ru 上它们碰巧都是同一个字符串，zh 上立刻
    分叉（Whisper 认 "zh"，Azure 的源语言是 "zh-Hans"）。合成一个字段，迟早要
    在某处拆回来。
    """
    code: str                  # 界面与持久化的 id："en" / "ru" / "zh"
    label: str                 # 下拉里显示的名字
    asr_language: str          # -> Whisper 的 language 参数
    translate_src: str | None  # -> 翻译层源语言；None = 该语言不翻译
    initial_prompt: str | None
    no_speech_prob_threshold: float
    avg_logprob_threshold: float
    compression_ratio_threshold: float
    calibrated_on: str         # 这组数字的依据；指向标定报告


LANGUAGES: dict[str, LanguageProfile] = {
    "en": LanguageProfile(
        code="en",
        label="英语",
        asr_language="en",
        translate_src="en",
        initial_prompt=None,
        # faster-whisper 参考实现的默认值（transcribe.py:274-276）。其判定逻辑
        # （transcribe.py:1215-1224）与本项目 is_hallucination 形状一致，差异只在
        # avg_logprob == 门限这一个零测度点上（spec §6.4）。
        #
        # 刻意不重新标定（C40）：把一个建立在评测集上的默认值，换成建立在一个人
        # 8 分钟录音上的自定义值，是降级而非升级。
        no_speech_prob_threshold=0.6,
        avg_logprob_threshold=-1.0,
        compression_ratio_threshold=2.4,
        calibrated_on="faster-whisper 参考实现默认值（transcribe.py:274-276）",
    ),
}

DEFAULT_LANGUAGE = "en"


@dataclass(frozen=True)
class AsrConfig:
    model: str = "large-v3-turbo"
    device: str = "cuda"
    compute_type: str = "int8_float16"
    language: str = "en"
    initial_prompt: str | None = None
    beam_size: int = 5
    word_timestamps: bool = True
    # C12：幻觉过滤阈值
    no_speech_prob_threshold: float = 0.6
    avg_logprob_threshold: float = -1.0
    compression_ratio_threshold: float = 2.4


@dataclass(frozen=True)
class TranslateConfig:
    src: str | None = "en"
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


def load_config(language: str = DEFAULT_LANGUAGE) -> Config:
    """唯一入口：从 profile 同时定下 asr.language 与 translate.src（C39）。

    不变量收在这一个函数上是刻意的。两处各写一遍就有一处会先改；而这两处不同步
    不会报错，只会让 Whisper 用英语模型去听俄语音频，输出一段通顺但完全编造的
    英文 —— 这是本项目里最危险的一类失败。
    """
    try:
        profile = LANGUAGES[language]
    except KeyError:
        raise ValueError(
            f"不认识的音频语言：{language!r}；可用：{sorted(LANGUAGES)}") from None

    translate = TranslateConfig(
        src=profile.translate_src,
        azure_key=os.environ.get("AZURE_TRANSLATOR_KEY"),
        azure_region=os.environ.get("AZURE_TRANSLATOR_REGION", "global"),
        google_key=os.environ.get("GOOGLE_TRANSLATE_KEY"),
    )
    asr = AsrConfig(
        language=profile.asr_language,
        initial_prompt=profile.initial_prompt,
        no_speech_prob_threshold=profile.no_speech_prob_threshold,
        avg_logprob_threshold=profile.avg_logprob_threshold,
        compression_ratio_threshold=profile.compression_ratio_threshold,
    )
    return Config(asr=asr, translate=translate)


def is_bilingual(cfg: Config) -> bool:
    """本会话是否翻译 —— 会话级属性，由 translate.src 是否为 None 派生。

    只在这一处派生：engine 与 pipeline 都要用它。两处各写一遍就有一处会先改，
    而这两处不同步不会报错，只会让某个显示面少说一句「不翻译」（C44）。
    """
    return cfg.translate.src is not None

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


# 2026-09-24 实测的一条**关于这套栈本身**的事实，读过两份标定报告后写在这里：
# ru / zh 三组阈值都沿用上游默认值，不是因为没测，而是因为**测了也动不了**。
#
# is_hallucination（`asr/whisper_engine.py`）的形状是
#     cr > 门限  或  (nsp > 门限 且 alp < 门限)
# 而 no_speech_prob 在这套 CT2 后端上**恒为 bit-exact 0.0**（103 行输出、四种探针
# 配置含数字静音，一次例外都没有）。于是 `nsp > 0.3` 恒假，把整条 AND 支连坐成
# 不可达 —— **avg_logprob 门限无论取什么值都不会生效**。另一条轴也够不着：
# 实测 compression_ratio 最大 1.707，而候选网格下限是 2.0。
#
# 结论：候选网格里 168 组阈值，没有一组能丢弃这 103 行中的任何一行。zh 那 1 条
# 已判定的 nonspeech（见下）就是活证据 —— 漏放率 100%。误杀率 0 是硬判据（C41），
# 它满足；但它的满足有一部分是**空转的**，别把「没误杀」读成「过滤有效」。
# 修这三个轴是独立立项，不在本单元内（spec §6 之外）。
# 若有人真的修好了这三条轴，**删掉本段注释** —— 它描述的是当前实测状态，
# 修好之后继续留着就从「说明」变成了「谎言」。
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
        #
        # 注意 LANGUAGES 上方那段实测结论对 en 同样成立：这三条轴在本项目这套栈上
        # 一条都够不着。不重新标定的理由与它是两回事，两条并存，不冲突。
        no_speech_prob_threshold=0.6,
        avg_logprob_threshold=-1.0,
        compression_ratio_threshold=2.4,
        calibrated_on="faster-whisper 参考实现默认值（transcribe.py:274-276）",
    ),
    "ru": LanguageProfile(
        code="ru",
        label="俄语",
        asr_language="ru",
        translate_src="ru",
        initial_prompt=None,
        # 实测后沿用上游默认值（C40 明文承认的合法产出）：44 行输出逐条对上
        # `sample/俄语字幕(不全.txt`（该字幕只覆盖 0–210s，见 spec §6.2），
        # 没有一条读起来是重复或不着边际的，负样本为空。漏放因此无从度量，
        # 凭正样本单侧收紧是在看不见的那一侧下注，故三个数字一个不动。
        no_speech_prob_threshold=0.6,
        avg_logprob_threshold=-1.0,
        compression_ratio_threshold=2.4,
        calibrated_on="docs/superpowers/calibration/2026-09-24-ru.md"
                      "（sample/俄语音频.MP3，38 段切句器段落，44 行 Whisper 输出"
                      "——6 段各被拆成两行；负样本为空，实测后沿用上游默认值"
                      "。44 行的真值标注由 AI 助手依用户明确委托判定）",
    ),
    "zh": LanguageProfile(
        code="zh",
        label="中文",
        asr_language="zh",
        # None = 中文走单语模式，本次会话不翻译（spec §5.3 / C44）
        translate_src=None,
        initial_prompt="以下是普通话的句子，请用简体中文转写。",
        # 实测后沿用上游默认值。59 行里判出 1 条 nonspeech：164.5s 处的
        # 「请用简体中文字幕提供」—— initial_prompt 自身的回声，中文字幕里没有，
        # 且显然不是课程内容。它没被拦住（漏放率 100%，误杀率 0%）。
        # 为什么拦不住见 LANGUAGES 上方那段：这套栈上过滤器的三条轴都不可达，
        # 换任何一组阈值都一样，所以照抄基准值是照实说而不是没测。
        no_speech_prob_threshold=0.6,
        avg_logprob_threshold=-1.0,
        compression_ratio_threshold=2.4,
        calibrated_on="docs/superpowers/calibration/2026-09-24-zh.md"
                      "（sample/中文音频.mp3，59 段切句器段落，59 行 Whisper 输出；"
                      "1 条 nonspeech 未被拦住。59 行经用户过目，"
                      "其中 index=20 由 AI 助手判定）",
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

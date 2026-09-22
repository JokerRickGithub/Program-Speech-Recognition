# Chrome 网课实时字幕流水线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个本地流水线：只捕获 Chrome 进程的音频 → 本地 ASR 识别英文 → 翻译成中文 → 字幕实时显示并落盘。

**Architecture:** 按进程捕获（WASAPI process loopback，经命名管道取流）→ silero VAD 门控 + 自有规则切句 → faster-whisper 整句识别 → 链式翻译 → JSONL 权威记录 + SRT 派生视图，同时经 WebSocket 推给本地网页。

**Tech Stack:** Python 3.13 / uv / process-audio-capture（原生 DLL）/ numpy / soxr / silero-vad / faster-whisper (CUDA) / FastAPI + WebSocket / pytest

**Spec:** `docs/superpowers/specs/2026-09-22-live-caption-pipeline-design.md`

## Global Constraints

以下取自 spec §3，每个任务都隐含包含本节，实现时不得偏离：

- **Python `>=3.13`**（C15：ctranslate2 自 4.6.0 起提供 cp313 wheel）
- **捕获必须按进程**，目标 `chrome.exe`；**不能传主进程 PID**（C26：库不覆盖进程树）
- **捕获走命名管道**，不用文件（C28：捕获期间输出文件被独占，无法 tail）
- **采样率**：捕获侧 48kHz float32 立体声，软件重采样到 16kHz 单声道（C2：不可向设备请求 16000）
- **VAD 帧长固定 512 样本**（16kHz），其他长度抛 `ValueError`（C7）
- **段长上限 15s**，必须在送进 ASR 前强断（C6：≥30s 会抛 `RuntimeError`）
- **`transcribe()` 必须显式传 `vad_filter=False`**（C9：默认 True 会拆掉我们切好的边界）
- **幻觉过滤方向**：`compression_ratio > 2.4` 丢弃；`no_speech_prob > 0.6 AND avg_logprob < -1.0` 丢弃（C12）
- **翻译绝不能只有免 key 一层**（C17）；**传输层错误必须重试而非降级**（C19）
- **SRT cue 内绝不放空行**（C22）；**原子替换必须有界重试**（C23：播放器占用会抛 `WinError 5`）
- **服务只绑 `127.0.0.1`**（C25）
- **不做无门限的归一化**——静音块绝不能被放大（C27 配合 spec §5.1）

**平台前提：** Windows 10 2004+ / Windows 11（`AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS` 要求）。

---

## 文件结构

```
chrometrans/
├── pyproject.toml              hatchling + src 布局 + chrometrans 入口脚本
├── src/chrometrans/
│   ├── __init__.py
│   ├── models.py               Cue —— 跨模块共享的唯一数据结构
│   ├── config.py               全部可调参数与凭据读取（spec §5.8）
│   ├── audio/
│   │   ├── capture.py          按进程捕获 + 命名管道 + WAV 头解析 + 下混重采样
│   │   ├── source.py           重连、PID 重解析、降级到系统级 loopback
│   │   └── segmenter.py        VAD 门控 + 切句规则 + 归一化
│   ├── asr/whisper_engine.py   faster-whisper 封装 + 幻觉过滤 + 时间归一
│   ├── translate/
│   │   ├── base.py             Translator 协议 + ChainTranslator + 重试语义
│   │   ├── microsoft.py        Tier 0 免 key / Tier 1 Azure
│   │   └── google.py           Tier 2 兜底
│   ├── output/
│   │   ├── jsonl.py            权威记录，append + fsync
│   │   └── srt.py              SRT 渲染 + 带重试的原子替换
│   ├── engine.py               编排，只发 cue / status / error 三类事件
│   ├── server.py               FastAPI + WebSocket + EventBus
│   ├── static/index.html       字幕页（单文件，无构建步骤）
│   └── cli.py                  入口，把各部件接起来
└── tests/                      与被测模块一一对应的单元测试 + 一个集成测试
```

拆分依据：

- **`capture.py` 与 `source.py` 分开** —— 前者是「怎么从管道读一个进程的音频」（已实测的机制），后者是「读不到时怎么办」（重试与降级策略）。两者的失败模式与测试方式完全不同。
- **`output/jsonl.py` 与 `srt.py` 分开** —— 权威源与派生视图，前者只 append，后者每次全量重渲。
- **三个 translate 模块各自独立** —— 加俄语时只改 `config`，加引擎时只加文件，都不触碰链逻辑。
- **`engine.py` 不认识音频设备、GPU、HTTP** —— 全部依赖注入，因此可以在没有显卡的机器上完整单测。

---

## Task 1: 项目骨架与配置

建立 `src/` 布局、可安装的包、pytest，并定义全局配置与数据模型。此任务之后，后续所有模块都有可导入的位置与统一的参数来源。

**Files:**
- Modify: `pyproject.toml`
- Delete: `main.py`, `test.py`
- Create: `src/chrometrans/__init__.py`
- Create: `src/chrometrans/models.py`
- Create: `src/chrometrans/config.py`
- Create: `tests/__init__.py`
- Test: `tests/test_config.py`, `tests/test_models.py`

**Interfaces:**
- Consumes: 无（起始任务）
- Produces:
  - `from chrometrans.models import Cue` — `Cue(id: int, start: float, end: float, source: str, target: str | None, src_lang: str, tgt_lang: str)`，方法 `to_dict() -> dict`、`from_dict(d) -> Cue`
  - `from chrometrans.config import Config, load_config` — `load_config() -> Config`，字段 `capture / segmenter / asr / translate / output`

- [ ] **Step 1: 清理 uv init 的残留**

```bash
cd "E:/self/own/work/ITMO/chrometrans"
git rm --cached main.py test.py 2>/dev/null || true
rm -f main.py test.py
```

- [ ] **Step 2: 改写 pyproject.toml**

```toml
[project]
name = "chrometrans"
version = "0.1.0"
description = "Chrome 网课实时字幕：按进程捕获音频 + 本地 ASR + 翻译，字幕落盘"
readme = "README.md"
requires-python = ">=3.13"
dependencies = [
    "process-audio-capture>=1.0.0",
    "pyaudiowpatch>=0.2.12.8",
    "soxr",
    "numpy",
]

[project.scripts]
chrometrans = "chrometrans.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/chrometrans"]

[dependency-groups]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: 需要真实音频设备与 Chrome 在播放"]
```

> 注意：删掉原来 `[tool.uv] package = false` —— 有了 `src/` 布局就必须作为包安装，否则 `import chrometrans` 失败。

- [ ] **Step 3: 写失败测试**

`tests/test_models.py`：

```python
from chrometrans.models import Cue


def test_cue_roundtrip():
    cue = Cue(id=1, start=3.12, end=6.48,
              source="The derivative is continuous",
              target="这个导数是连续的",
              src_lang="en", tgt_lang="zh-Hans")
    assert Cue.from_dict(cue.to_dict()) == cue


def test_cue_allows_null_target():
    """翻译整链失败时 target 为 None，原文照常保存（spec §6）。"""
    cue = Cue(id=2, start=0.0, end=1.0, source="hello", target=None,
              src_lang="en", tgt_lang="zh-Hans")
    assert Cue.from_dict(cue.to_dict()).target is None
```

`tests/test_config.py`：

```python
from chrometrans.config import load_config


def test_defaults_match_spec():
    cfg = load_config()
    assert cfg.capture.target_sample_rate == 16000
    assert cfg.segmenter.frame_samples == 512          # C7
    assert cfg.segmenter.max_segment_s == 15.0         # C6
    assert cfg.asr.language == "en"
    assert cfg.translate.src == "en"
    assert cfg.translate.tgt == "zh-Hans"


def test_normalization_has_a_silence_floor():
    """C27：归一化必须带静音门限，否则会放大底噪诱发幻觉。"""
    cfg = load_config()
    assert cfg.segmenter.norm_floor_rms > 0
```
- [ ] **Step 4: 跑测试确认失败**

Run: `uv run pytest tests/test_config.py tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans'`

- [ ] **Step 5: 建包骨架并实现**

```bash
mkdir -p src/chrometrans/audio src/chrometrans/asr src/chrometrans/translate src/chrometrans/output tests
```

`src/chrometrans/__init__.py`：

```python
"""Chrome 网课实时字幕流水线。"""

__version__ = "0.1.0"
```

（`audio/`、`asr/`、`translate/`、`output/` 四个子目录在第 3、6、7、4 个任务里再各加 `__init__.py`。）

`src/chrometrans/models.py`：

```python
"""跨模块共享的数据模型。"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Cue:
    """一条双语字幕。整句识别，一经写出即为终态（spec §5.5）。"""

    id: int
    start: float          # 秒，相对捕获开始
    end: float
    source: str           # 原文
    target: str | None    # 译文；翻译整链失败时为 None
    src_lang: str
    tgt_lang: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Cue":
        return cls(**d)
```

`src/chrometrans/config.py`：

```python
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
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_config.py tests/test_models.py -v`
Expected: PASS（4 passed）

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src tests
git commit -m "feat: 项目骨架、全局配置与 Cue 数据模型"
```

---

## Task 2: 按进程捕获（`audio/capture.py`）

**这是全项目风险最高的一环** —— 整个设计压在「把命名管道当输出路径传给 DLL」这个未文档化行为上（spec §9）。本任务结束时应能在真实 Chrome 播放时拿到 16kHz 单声道音频。

**Files:**
- Create: `src/chrometrans/audio/__init__.py`
- Create: `src/chrometrans/audio/capture.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: `chrometrans.config.CaptureConfig`
- Produces:
  - `parse_wav_header(buf: bytes) -> tuple[int, AudioFormat] | None`
  - `AudioFormat(channels: int, sample_rate: int, bits: int)`
  - `PcmConverter(fmt: AudioFormat, target_rate: int)`，方法 `convert(data: bytes) -> np.ndarray`（16kHz 单声道 float32）
  - `find_target_pid(process_names, probe_seconds) -> int`
  - `ProcessAudioStream(pid, pipe_name, buffer_bytes, target_rate)`，方法 `start()` / `read() -> np.ndarray` / `stop()`
  - `CaptureError(RuntimeError)`

- [ ] **Step 1: 写失败测试**

`tests/test_capture.py`：

```python
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
    assert abs(len(out) - 16000) < 100


def test_pcm_converter_rejects_non_float32():
    fmt = AudioFormat(channels=2, sample_rate=48000, bits=16)
    with pytest.raises(CaptureError, match="float32"):
        PcmConverter(fmt, target_rate=16000)


def test_find_target_pid_raises_when_nothing_playing(monkeypatch):
    import chrometrans.audio.capture as cap

    monkeypatch.setattr(
        cap.ProcessAudioCapture, "enumerate_audio_processes",
        staticmethod(lambda dll_path=None: []))
    with pytest.raises(CaptureError, match="没有进程在渲染音频"):
        cap.find_target_pid(("chrome.exe",))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_capture.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans.audio'`

- [ ] **Step 3: 实现**

`src/chrometrans/audio/__init__.py`：

```python
"""音频捕获与切句。"""
```

`src/chrometrans/audio/capture.py`：

```python
"""按进程捕获 Chrome 音频（spec §5.1）。

为什么这么写（全部有实测依据，见 spec §3.1）：
- C26: 库不覆盖进程树，必须抓「正在渲染音频」的那个 PID
- C28: 库没有内存流接口，且输出文件被独占 → 不能 tail WAV
- C29: 把命名管道当输出路径传入，可得 10ms 粒度的流式 PCM
"""
from __future__ import annotations

import ctypes
import struct
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

import numpy as np
import soxr
from process_audio_capture import ProcessAudioCapture

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_k32.CreateNamedPipeW.restype = wintypes.HANDLE
_k32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
_k32.ConnectNamedPipe.restype = wintypes.BOOL
_k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_k32.ReadFile.restype = wintypes.BOOL
_k32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]

PIPE_ACCESS_INBOUND = 0x00000001
PIPE_TYPE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255

EXPECTED_BITS = 32          # float32；实测 DLL 固定输出此格式


class CaptureError(RuntimeError):
    """捕获失败。调用方应降级到系统级 loopback（spec §5.1 降级路径）。"""


@dataclass(frozen=True)
class AudioFormat:
    channels: int
    sample_rate: int
    bits: int


def parse_wav_header(buf: bytes) -> tuple[int, AudioFormat] | None:
    """扫描 RIFF 块，返回 (payload_offset, AudioFormat)；未收全返回 None。

    不依赖 data 块的长度字段 —— 流式写入时总长度未知（spec §5.1）。
    不用 wave 模块：该头是 WAVE_FORMAT_EXTENSIBLE，wave.open() 会抛
    "unknown extended format"。
    """
    if len(buf) < 12 or buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        return None
    off = 12
    fmt_body: bytes | None = None
    while off + 8 <= len(buf):
        chunk_id = buf[off:off + 4]
        size = struct.unpack_from("<I", buf, off + 4)[0]
        if chunk_id == b"fmt ":
            if off + 8 + size > len(buf):
                return None                      # 头还没收全
            fmt_body = buf[off + 8:off + 8 + size]
        elif chunk_id == b"data":
            if fmt_body is None or len(fmt_body) < 16:
                return None
            return off + 8, AudioFormat(
                channels=struct.unpack_from("<H", fmt_body, 2)[0],
                sample_rate=struct.unpack_from("<I", fmt_body, 4)[0],
                bits=struct.unpack_from("<H", fmt_body, 14)[0],
            )
        off += 8 + size + (size & 1)
    return None


class PcmConverter:
    """交错 float32 → 16kHz 单声道 float32。重采样器长驻，块间绝不 clear()（C3）。"""

    def __init__(self, fmt: AudioFormat, target_rate: int = 16000):
        if fmt.bits != EXPECTED_BITS:
            raise CaptureError(
                f"预期 float32（{EXPECTED_BITS} bit），实际 {fmt.bits} bit")
        self._channels = fmt.channels
        self._resampler = soxr.ResampleStream(
            fmt.sample_rate, target_rate, 1, dtype="float32")

    def convert(self, data: bytes) -> np.ndarray:
        samples = np.frombuffer(data, dtype=np.float32)
        frames = len(samples) // self._channels
        if frames == 0:
            return np.zeros(0, dtype=np.float32)
        usable = samples[:frames * self._channels]
        mono = usable.reshape(frames, self._channels).mean(axis=1)
        return self._resampler.resample_chunk(mono)


def _pick_loudest(cands, probe_seconds: float):
    """多候选时逐个试听，取电平最高的那个（spec §7）。"""
    best, best_db = None, float("-inf")
    for proc in cands:
        cap = ProcessAudioCapture(pid=proc.pid)      # output_path=None = 仅监控
        try:
            cap.start()
            time.sleep(probe_seconds)
            level = cap.level_db
        except Exception:
            continue
        finally:
            try:
                cap.stop()
            except Exception:
                pass
        if level > best_db:
            best, best_db = proc, level
    return best


def find_target_pid(process_names: tuple[str, ...] = ("chrome.exe",),
                    probe_seconds: float = 1.0) -> int:
    """找到正在渲染音频的目标 PID。

    C26：不能抓主进程。Chrome 主进程与音频服务子进程同名，唯一可靠的区分方式
    就是「是否出现在 enumerate_audio_processes() 里」。
    """
    procs = ProcessAudioCapture.enumerate_audio_processes()
    names = {n.lower() for n in process_names}
    cands = [p for p in procs if p.name.lower() in names]
    if not cands:
        raise CaptureError(
            f"没有进程在渲染音频：{sorted(names)}。"
            f"当前列表：{[(p.pid, p.name) for p in procs]}"
        )
    if len(cands) == 1:
        return cands[0].pid
    chosen = _pick_loudest(cands, probe_seconds)
    if chosen is None:
        # 全部候选都试听失败，退回第一个，由启动自检验证（spec §9）
        return cands[0].pid
    return chosen.pid


class ProcessAudioStream:
    """从指定进程捕获音频，输出 16kHz 单声道 float32 块。"""

    def __init__(self, pid: int, pipe_name: str = r"\\.\pipe\chrometrans",
                 buffer_bytes: int = 1 << 20, target_rate: int = 16000):
        self._pid = pid
        self._pipe_name = pipe_name
        self._buffer_bytes = buffer_bytes
        self._target_rate = target_rate
        self._handle: int | None = None
        self._cap: ProcessAudioCapture | None = None
        self._converter: PcmConverter | None = None
        self._pending = bytearray()
        self._buf = ctypes.create_string_buffer(buffer_bytes)
        self._got = ctypes.c_ulong(0)

    @property
    def is_started(self) -> bool:
        return self._handle is not None

    def start(self) -> None:
        handle = _k32.CreateNamedPipeW(
            self._pipe_name, PIPE_ACCESS_INBOUND,
            PIPE_TYPE_BYTE | PIPE_WAIT, PIPE_UNLIMITED_INSTANCES,
            self._buffer_bytes, self._buffer_bytes, 0, None)
        if not handle or handle == wintypes.HANDLE(-1).value:
            raise CaptureError(
                f"CreateNamedPipeW 失败：{ctypes.get_last_error()}")
        self._handle = handle

        # 必须先挂起一个 ConnectNamedPipe，DLL 才连得上（spec §5.1）
        threading.Thread(
            target=_k32.ConnectNamedPipe,
            args=(wintypes.HANDLE(handle), None),
            daemon=True,
        ).start()

        self._cap = ProcessAudioCapture(pid=self._pid, output_path=self._pipe_name)
        try:
            self._cap.start()
        except Exception as exc:
            self.stop()
            raise CaptureError(f"启动捕获失败（DLL 可能拒绝了管道路径）：{exc}") from exc

    def read(self) -> np.ndarray:
        """阻塞读到一块音频，返回 16kHz 单声道 float32。可能是空数组（头部未收全时）。"""
        if self._handle is None:
            raise CaptureError("流未启动")
        ok = _k32.ReadFile(
            wintypes.HANDLE(self._handle), self._buf, self._buffer_bytes,
            ctypes.byref(self._got), None)
        if not ok:
            raise CaptureError(f"ReadFile 失败：{ctypes.get_last_error()}")
        data = self._buf.raw[:self._got.value]

        if self._converter is None:
            self._pending += data
            parsed = parse_wav_header(bytes(self._pending))
            if parsed is None:
                return np.zeros(0, dtype=np.float32)
            offset, fmt = parsed
            self._converter = PcmConverter(fmt, self._target_rate)
            data = bytes(self._pending)[offset:]
            self._pending = bytearray()
            if not data:
                return np.zeros(0, dtype=np.float32)

        return self._converter.convert(data)

    def stop(self) -> None:
        if self._cap is not None:
            try:
                self._cap.stop()
            except Exception:
                pass
            self._cap = None
        if self._handle is not None:
            _k32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_capture.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 真实设备集成测试**

新建 `tests/test_capture_integration.py`：

```python
import numpy as np
import pytest

from chrometrans.audio.capture import ProcessAudioStream, find_target_pid


@pytest.mark.integration
def test_captures_real_audio_from_chrome():
    """需要 Chrome 正在播放有声内容。"""
    pid = find_target_pid(("chrome.exe",))
    stream = ProcessAudioStream(pid=pid)
    stream.start()
    try:
        total = []
        for _ in range(40):                 # 约 400ms 的块 ×N
            chunk = stream.read()
            if chunk.size:
                total.append(chunk)
            if sum(len(c) for c in total) > 16000 * 3:
                break
        audio = np.concatenate(total) if total else np.zeros(0, dtype=np.float32)
    finally:
        stream.stop()

    assert audio.size > 16000, "没读到足够的音频"
    rms = float(np.sqrt(np.mean(audio ** 2)))
    assert rms > 1e-4, f"读到的是静音（rms={rms}）—— Chrome 在播放吗？"
```

Run: `uv run pytest tests/test_capture_integration.py -v -m integration`
Expected: PASS。**若 FAIL 且报静音** —— 先确认 Chrome 确实在放有声音的内容，再确认 `find_target_pid` 选中的 PID 正确。

- [ ] **Step 6: Commit**

```bash
git add src/chrometrans/audio tests/test_capture.py tests/test_capture_integration.py
git commit -m "feat(capture): 按进程捕获 Chrome 音频，经命名管道取流式 PCM"
```

---

## Task 3: 权威记录（`output/jsonl.py`）

JSONL 是唯一权威源，SRT 只是它的派生视图。崩溃时最多丢 SRT 视图，JSONL 必须完好。

**Files:**
- Create: `src/chrometrans/output/__init__.py`
- Create: `src/chrometrans/output/jsonl.py`
- Test: `tests/test_jsonl.py`

**Interfaces:**
- Consumes: `chrometrans.models.Cue`
- Produces: `JsonlWriter(path: Path)`，方法 `open()` / `append(cue: Cue)` / `read_all() -> list[Cue]` / `close()`；`session_dir(root: Path, when: datetime) -> Path`

- [ ] **Step 1: 写失败测试**

`tests/test_jsonl.py`：

```python
from datetime import datetime

from chrometrans.models import Cue
from chrometrans.output.jsonl import JsonlWriter, session_dir


def _cue(i):
    return Cue(id=i, start=float(i), end=float(i) + 1.5,
               source=f"line {i}", target=f"第 {i} 行",
               src_lang="en", tgt_lang="zh-Hans")


def test_session_dir_is_timestamped(tmp_path):
    d = session_dir(tmp_path, datetime(2026, 9, 22, 14, 20))
    assert d == tmp_path / "2026-09-22_1420"
    assert d.is_dir()


def test_append_and_read_back(tmp_path):
    path = tmp_path / "captions.jsonl"
    w = JsonlWriter(path)
    w.open()
    for i in (1, 2, 3):
        w.append(_cue(i))
    w.close()

    assert JsonlWriter(path).read_all() == [_cue(1), _cue(2), _cue(3)]


def test_survives_crash_without_close(tmp_path):
    """不调 close() 也必须已落盘 —— 每行 append + flush + fsync。"""
    path = tmp_path / "captions.jsonl"
    w = JsonlWriter(path)
    w.open()
    w.append(_cue(1))
    w.append(_cue(2))
    # 故意不 close()

    assert [c.id for c in JsonlWriter(path).read_all()] == [1, 2]


def test_null_target_roundtrips(tmp_path):
    path = tmp_path / "captions.jsonl"
    w = JsonlWriter(path)
    w.open()
    w.append(Cue(id=1, start=0.0, end=1.0, source="hi", target=None,
                 src_lang="en", tgt_lang="zh-Hans"))
    w.close()
    assert JsonlWriter(path).read_all()[0].target is None


def test_unicode_is_not_escaped(tmp_path):
    path = tmp_path / "captions.jsonl"
    w = JsonlWriter(path)
    w.open()
    w.append(_cue(1))
    w.close()
    assert "第 1 行" in path.read_text(encoding="utf-8")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_jsonl.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans.output'`

- [ ] **Step 3: 实现**

`src/chrometrans/output/__init__.py`：

```python
"""字幕输出：JSONL 权威源 + SRT 派生视图。"""
```

`src/chrometrans/output/jsonl.py`：

```python
"""JSONL 权威记录。每 cue 一行，append + flush + fsync（spec §5.5）。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from chrometrans.models import Cue


def session_dir(root: Path, when: datetime) -> Path:
    """按时间戳建会话目录，如 transcripts/2026-09-22_1420/。"""
    path = root / when.strftime("%Y-%m-%d_%H%M")
    path.mkdir(parents=True, exist_ok=True)
    return path


class JsonlWriter:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._fh = None

    def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a", encoding="utf-8")

    def append(self, cue: Cue) -> None:
        if self._fh is None:
            raise RuntimeError("JsonlWriter 未 open()")
        line = json.dumps(cue.to_dict(), ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def read_all(self) -> list[Cue]:
        if not self._path.exists():
            return []
        cues = []
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    cues.append(Cue.from_dict(json.loads(line)))
        return cues

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_jsonl.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add src/chrometrans/output tests/test_jsonl.py
git commit -m "feat(output): JSONL 权威记录，append + fsync"
```

---

## Task 4: SRT 派生视图（`output/srt.py`）

SRT 每 ~20 cue 重渲一次。两个硬约束：cue 内不能有空行（C22），原子替换遇到文件被占用必须重试（C23）。

**Files:**
- Create: `src/chrometrans/output/srt.py`
- Test: `tests/test_srt.py`

**Interfaces:**
- Consumes: `chrometrans.models.Cue`
- Produces: `format_timestamp(seconds: float) -> str`；`render(cues: list[Cue]) -> str`；`atomic_write(path: Path, text: str, retries: int, base_delay: float) -> bool`；`write_with_fallback(path: Path, text: str, ...) -> Path | None`

- [ ] **Step 1: 写失败测试**

`tests/test_srt.py`：

```python
import os

import pytest

from chrometrans.models import Cue
from chrometrans.output.srt import atomic_write, format_timestamp, render


def _cue(i, start, end, src="hello", tgt="你好"):
    return Cue(id=i, start=start, end=end, source=src, target=tgt,
               src_lang="en", tgt_lang="zh-Hans")


def test_format_timestamp_basic():
    assert format_timestamp(3.12) == "00:00:03,120"
    assert format_timestamp(3661.5) == "01:01:01,500"


def test_format_timestamp_rounds_milliseconds():
    assert format_timestamp(1.2345) == "00:00:01,234"
    assert format_timestamp(1.9996) == "00:00:02,000"


def test_render_bilingual_format():
    text = render([_cue(1, 3.12, 6.48, "The derivative is continuous", "这个导数是连续的")])
    assert text == (
        "1\n"
        "00:00:03,120 --> 00:00:06,480\n"
        "The derivative is continuous\n"
        "这个导数是连续的\n"
        "\n"
    )


def test_render_has_no_blank_line_inside_a_cue():
    """C22：空行会终止 cue，导致整个文件错位。"""
    text = render([_cue(1, 0.0, 1.0), _cue(2, 1.0, 2.0)])
    for block in text.split("\n\n"):
        if block.strip():
            assert all(line.strip() for line in block.splitlines())


def test_render_omits_missing_translation():
    text = render([_cue(1, 0.0, 1.0, tgt=None)])
    assert text == ("1\n00:00:00,000 --> 00:00:01,000\nhello\n\n")


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "captions.srt"
    assert atomic_write(target, "content", retries=3, base_delay=0.0) is True
    assert target.read_text(encoding="utf-8") == "content"


def test_atomic_write_retries_on_permission_error(tmp_path, monkeypatch):
    """C23：播放器占用文件时 os.replace 抛 WinError 5，必须重试。"""
    import chrometrans.output.srt as srt_mod

    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "拒绝访问")
        return real_replace(src, dst)

    monkeypatch.setattr(srt_mod.os, "replace", flaky)
    target = tmp_path / "captions.srt"
    assert atomic_write(target, "content", retries=5, base_delay=0.0) is True
    assert calls["n"] == 3
    assert target.read_text(encoding="utf-8") == "content"


def test_atomic_write_gives_up_after_retries(tmp_path, monkeypatch):
    import chrometrans.output.srt as srt_mod

    def always_fail(src, dst):
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(srt_mod.os, "replace", always_fail)
    target = tmp_path / "captions.srt"
    assert atomic_write(target, "content", retries=2, base_delay=0.0) is False
    assert not target.exists()


def test_fallback_writes_next_file_when_target_is_locked(tmp_path, monkeypatch):
    """spec §5.5：主目标写不进去时改投 captions.next.srt，不阻塞流水线。"""
    import chrometrans.output.srt as srt_mod

    target = tmp_path / "captions.srt"

    def only_target_fails(src, dst):
        if str(dst).endswith("captions.srt"):
            raise PermissionError(5, "拒绝访问")
        return os.replace(src, dst)

    monkeypatch.setattr(srt_mod.os, "replace", only_target_fails)
    written = srt_mod.write_with_fallback(target, "content",
                                          retries=1, base_delay=0.0)

    assert written == tmp_path / "captions.next.srt"
    assert written.read_text(encoding="utf-8") == "content"


def test_fallback_prefers_target_when_free(tmp_path):
    import chrometrans.output.srt as srt_mod

    target = tmp_path / "captions.srt"
    assert srt_mod.write_with_fallback(target, "content",
                                       retries=1, base_delay=0.0) == target
    assert not (tmp_path / "captions.next.srt").exists()


def test_fallback_returns_none_when_both_paths_fail(tmp_path, monkeypatch):
    import chrometrans.output.srt as srt_mod

    def always_fail(src, dst):
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(srt_mod.os, "replace", always_fail)
    assert srt_mod.write_with_fallback(tmp_path / "captions.srt", "content",
                                       retries=1, base_delay=0.0) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_srt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans.output.srt'`

- [ ] **Step 3: 实现**

`src/chrometrans/output/srt.py`：

```python
"""SRT 派生视图。每 ~20 cue 重渲一次（spec §5.5）。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from chrometrans.models import Cue


def format_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3600 * 1000)
    minutes, rem = divmod(rem, 60 * 1000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def render(cues: list[Cue]) -> str:
    """渲染双语 SRT。cue 内绝不出现空行（C22）。"""
    blocks = []
    for cue in cues:
        lines = [str(cue.id),
                 f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}",
                 cue.source]
        if cue.target:
            lines.append(cue.target)
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n\n"


def atomic_write(path: Path, text: str, retries: int = 5,
                 base_delay: float = 0.2) -> bool:
    """原子替换，带退避重试（C23）。

    返回 True 表示写入成功；False 表示重试耗尽 —— 调用方应记日志并继续，
    不能阻塞流水线（spec §6）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")

    delay = base_delay
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            if attempt == retries - 1:
                break
            if delay:
                time.sleep(delay)
            delay *= 2

    try:
        tmp.unlink()
    except OSError:
        pass
    return False


def write_with_fallback(path: Path, text: str, retries: int = 5,
                        base_delay: float = 0.2) -> Path | None:
    """先试主目标；被占用则改投 `captions.next.srt`（spec §5.5 / §6）。

    返回实际写入的路径；两处都失败返回 None，由调用方记日志。
    """
    path = Path(path)
    if atomic_write(path, text, retries=retries, base_delay=base_delay):
        return path

    fallback = path.with_name("captions.next.srt")
    if atomic_write(fallback, text, retries=retries, base_delay=base_delay):
        return fallback
    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_srt.py -v`
Expected: PASS（11 passed）

- [ ] **Step 5: Commit**

```bash
git add src/chrometrans/output/srt.py tests/test_srt.py
git commit -m "feat(output): SRT 渲染与带重试的原子替换"
```

---

## Task 5: 切句（`audio/segmenter.py`）

VAD 只做门控，分割规则全部由本模块实现。这是纯逻辑层，可完全离线单测。

> **与 spec §5.2 的一处有意偏离，执行者需要知道**：spec 写「`VADIterator` 只负责门控」，
> 但同一节又要求超长段用 `get_speech_timestamps_from_probs()` 挑切点 —— 而
> `VADIterator` **不暴露每帧概率**，拿不到就用不了那条规则。因此本任务**直接调用 silero
> 模型取概率**，自己做迟滞判定。规则表（350ms / 15s / 600ms / 512 样本）与切点规则
> 完全按 spec 实现，只是门控原语换了。**遇到冲突以本节为准**，并在 spec 中回填这一修正。

**Files:**
- Create: `src/chrometrans/audio/segmenter.py`
- Test: `tests/test_segmenter.py`

**Interfaces:**
- Consumes: `chrometrans.config.SegmenterConfig`
- Produces:
  - `Segment(index: int, start: float, end: float, audio: np.ndarray)`
  - `Segmenter(cfg: SegmenterConfig, vad=None)`，方法 `feed(chunk: np.ndarray) -> list[Segment]` / `flush() -> list[Segment]`。`vad` 是 `Callable[[np.ndarray, int], float]`（帧 → 语音概率 0..1），默认加载 silero 模型
  - `normalize(chunk, target_rms, max_gain, floor_rms) -> np.ndarray`
  - `pick_cut_point(probs, frame_samples, sample_rate, threshold, min_gap_s) -> int | None`

- [ ] **Step 1: 写失败测试**

`tests/test_segmenter.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_segmenter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans.audio.segmenter'`

- [ ] **Step 3: 实现**

`src/chrometrans/audio/segmenter.py`：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_segmenter.py -v`
Expected: PASS（17 passed）

> 若 `test_chunk_boundaries_do_not_lose_samples` 失败，检查 `_tape` 与 `_tape_origin` 是否始终同步推进 —— 这是最容易出错的点。

- [ ] **Step 5: Commit**

```bash
git add src/chrometrans/audio/segmenter.py tests/test_segmenter.py
git commit -m "feat(segmenter): VAD 门控 + 自有切句规则 + 带门限的归一化"
```

---

## Task 6: 翻译抽象与链（`translate/base.py`）

**Files:**
- Create: `src/chrometrans/translate/__init__.py`
- Create: `src/chrometrans/translate/base.py`
- Test: `tests/test_translate_base.py`

**Interfaces:**
- Consumes: `chrometrans.config.TranslateConfig`
- Produces:
  - `Translator(Protocol)` — `async translate(texts: list[str], src: str, tgt: str) -> list[str]`
  - `TranslationError(Exception)`（确定性失败 → 降级）
  - `TransientTranslationError(TranslationError)`（传输抖动 → 重试）
  - `ChainTranslator(providers: list[Translator], cfg: TranslateConfig)`，方法 `async translate(texts, src, tgt) -> list[str | None]`
  - `retry_transient(fn, max_retries, base_delay)` — 供 provider 复用的重试装饰器

- [ ] **Step 1: 写失败测试**

`tests/test_translate_base.py`：

```python
import asyncio

import pytest

from chrometrans.config import TranslateConfig
from chrometrans.translate.base import (
    ChainTranslator,
    TransientTranslationError,
    TranslationError,
    retry_transient,
)


class StubTranslator:
    def __init__(self, name, behaviour):
        self.name = name
        self.behaviour = behaviour
        self.calls = 0

    async def translate(self, texts, src, tgt):
        self.calls += 1
        return self.behaviour(texts)


def run(coro):
    return asyncio.run(coro)


def test_chain_uses_first_provider_on_success():
    a = StubTranslator("a", lambda t: [f"A:{x}" for x in t])
    b = StubTranslator("b", lambda t: [f"B:{x}" for x in t])
    chain = ChainTranslator([a, b], TranslateConfig())

    out = run(chain.translate(["hello"], "en", "zh-Hans"))
    assert out == ["A:hello"]
    assert b.calls == 0


def test_chain_falls_through_on_deterministic_failure():
    def boom(texts):
        raise TranslationError("400 bad request")

    a = StubTranslator("a", boom)
    b = StubTranslator("b", lambda t: [f"B:{x}" for x in t])
    chain = ChainTranslator([a, b], TranslateConfig())

    assert run(chain.translate(["hello"], "en", "zh-Hans")) == ["B:hello"]


def test_transient_error_is_retried_not_downgraded():
    """C19：SSL EOF / 连接重置属瞬时抖动，直接降级会让每次抖动都静默换引擎。"""
    attempts = {"n": 0}

    def flaky(texts):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientTranslationError("SSL UNEXPECTED_EOF_WHILE_READING")
        return [f"A:{x}" for x in texts]

    a = StubTranslator("a", flaky)
    b = StubTranslator("b", lambda t: [f"B:{x}" for x in t])
    chain = ChainTranslator([a, b], TranslateConfig(max_retries=3))

    assert run(chain.translate(["hello"], "en", "zh-Hans")) == ["A:hello"]
    assert b.calls == 0, "瞬时错误耗尽重试前不得降级"


def test_chain_returns_none_per_item_when_all_fail():
    def boom(texts):
        raise TranslationError("nope")

    chain = ChainTranslator([StubTranslator("a", boom)], TranslateConfig())
    assert run(chain.translate(["x", "y"], "en", "zh-Hans")) == [None, None]


def test_chain_returns_empty_for_empty_input():
    chain = ChainTranslator([], TranslateConfig())
    assert run(chain.translate([], "en", "zh-Hans")) == []


def test_retry_transient_gives_up_and_reraises():
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise TransientTranslationError("boom")

    with pytest.raises(TransientTranslationError):
        run(retry_transient(always_fail, max_retries=3, base_delay=0.0))
    assert calls["n"] == 3
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_translate_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans.translate'`

- [ ] **Step 3: 实现**

`src/chrometrans/translate/__init__.py`：

```python
"""翻译层：多引擎链式降级。"""
```

`src/chrometrans/translate/base.py`：

```python
"""翻译抽象与降级链（spec §5.4）。"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Protocol

from chrometrans.config import TranslateConfig


class TranslationError(Exception):
    """确定性失败（4xx 等）—— 应当降级到下一个 provider。"""


class TransientTranslationError(TranslationError):
    """传输层抖动（SSL EOF、连接重置）—— 必须重试，不得降级（C19）。"""


class Translator(Protocol):
    name: str

    async def translate(self, texts: list[str], src: str, tgt: str) -> list[str]: ...


async def retry_transient(fn: Callable[[], Awaitable], max_retries: int = 3,
                          base_delay: float = 0.5):
    """有界重试 + 指数退避。只重试 TransientTranslationError。"""
    delay = base_delay
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except TransientTranslationError as exc:
            last = exc
            if attempt == max_retries - 1:
                break
            if delay:
                await asyncio.sleep(delay)
            delay *= 2
    raise last


class ChainTranslator:
    """按序尝试各 provider。全部失败时该 cue 的译文为 None，原文照常保存。"""

    def __init__(self, providers: list[Translator], cfg: TranslateConfig):
        self._providers = list(providers)
        self._cfg = cfg

    async def translate(self, texts: list[str], src: str, tgt: str
                        ) -> list[str | None]:
        if not texts:
            return []

        for provider in self._providers:
            try:
                result = await retry_transient(
                    lambda p=provider: p.translate(texts, src, tgt),
                    max_retries=self._cfg.max_retries,
                    base_delay=self._cfg.retry_base_delay_s,
                )
            except Exception:
                continue

            if len(result) != len(texts):
                continue                       # 序号对不上，宁可换引擎也不错位

            return list(result)

        return [None] * len(texts)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_translate_base.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: Commit**

```bash
git add src/chrometrans/translate tests/test_translate_base.py
git commit -m "feat(translate): Translator 协议与带重试语义的降级链"
```

---

## Task 7: Microsoft 翻译（`translate/microsoft.py`）

Tier 0 免 key + Tier 1 Azure Key。免 key 端点实测 60 次请求中 13 次传输失败（C19），所以重试逻辑在这里最要紧。

**Files:**
- Create: `src/chrometrans/translate/microsoft.py`
- Test: `tests/test_translate_microsoft.py`

**Interfaces:**
- Consumes: `chrometrans.translate.base.{Translator, TranslationError, TransientTranslationError}`
- Produces: `MicrosoftTranslator(api_key: str | None, region: str, timeout_s: float)`，`name` 属性

- [ ] **Step 1: 写失败测试**

`tests/test_translate_microsoft.py`：

```python
import httpx
import pytest

from chrometrans.translate.base import (
    TransientTranslationError,
    TranslationError,
)
from chrometrans.translate.microsoft import MicrosoftTranslator


def _client_returning(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_free_tier_builds_request_correctly(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json=[{"translations": [{"text": "你好"}]}])

    t = MicrosoftTranslator(api_key=None, region="global", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))

    import asyncio
    out = asyncio.run(t.translate(["hello"], "en", "zh-Hans"))
    assert out == ["你好"]
    assert "api-edge.cognitive.microsofttranslator.com" in seen["url"]
    assert "from=en" in seen["url"] and "to=zh-Hans" in seen["url"]


def test_azure_tier_uses_key_and_region(monkeypatch):
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=[{"translations": [{"text": "你好"}]}])

    t = MicrosoftTranslator(api_key="SECRET", region="eastasia", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))

    import asyncio
    asyncio.run(t.translate(["hello"], "en", "zh-Hans"))
    assert seen["headers"]["ocp-apim-subscription-key"] == "SECRET"
    assert seen["headers"]["ocp-apim-subscription-region"] == "eastasia"


def test_4xx_raises_deterministic_error(monkeypatch):
    def handler(request):
        return httpx.Response(400, json={"error": "bad"})

    t = MicrosoftTranslator(api_key="K", region="global", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))

    import asyncio
    with pytest.raises(TranslationError):
        asyncio.run(t.translate(["hello"], "en", "zh-Hans"))


def test_transport_error_raises_transient(monkeypatch):
    """C19：传输层错误必须是 transient，才能被重试而不是降级。"""
    def handler(request):
        raise httpx.ConnectError("connection reset")

    t = MicrosoftTranslator(api_key="K", region="global", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))

    import asyncio
    with pytest.raises(TransientTranslationError):
        asyncio.run(t.translate(["hello"], "en", "zh-Hans"))


def test_preserves_order_across_batch(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[
            {"translations": [{"text": "一"}]},
            {"translations": [{"text": "二"}]},
            {"translations": [{"text": "三"}]},
        ])

    t = MicrosoftTranslator(api_key="K", region="global", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))

    import asyncio
    out = asyncio.run(t.translate(["a", "b", "c"], "en", "zh-Hans"))
    assert out == ["一", "二", "三"]


def test_does_not_escape_payload(monkeypatch):
    """C20：转义必须成对或干脆不做。本实现选择不转义。"""
    from chrometrans.translate.microsoft import build_payload

    assert build_payload(["a < b & c"]) == [{"Text": "a < b & c"}]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_translate_microsoft.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans.translate.microsoft'`

- [ ] **Step 3: 实现**

`src/chrometrans/translate/microsoft.py`：

```python
"""Microsoft Translator：Tier 0 免 key + Tier 1 Azure Key（spec §5.4）。"""
from __future__ import annotations

import httpx

from chrometrans.translate.base import (
    TransientTranslationError,
    TranslationError,
)

FREE_ENDPOINT = "https://api-edge.cognitive.microsofttranslator.com/translate"
AZURE_ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate"
API_VERSION = "3.0"


def build_payload(texts: list[str]) -> list[dict]:
    """构建请求体。刻意不做 html.escape（C20：转义必须成对，不做最省事）。"""
    return [{"Text": t} for t in texts]


class MicrosoftTranslator:
    def __init__(self, api_key: str | None = None, region: str = "global",
                 timeout_s: float = 10.0):
        self._key = api_key
        self._region = region
        self._timeout_s = timeout_s
        self.name = "microsoft-azure" if api_key else "microsoft-free"

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout_s)

    @property
    def _endpoint(self) -> str:
        return AZURE_ENDPOINT if self._key else FREE_ENDPOINT

    async def translate(self, texts: list[str], src: str, tgt: str) -> list[str]:
        if not texts:
            return []

        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Ocp-Apim-Subscription-Key"] = self._key
            headers["Ocp-Apim-Subscription-Region"] = self._region

        params = {"api-version": API_VERSION, "from": src, "to": tgt}

        try:
            async with self._make_client() as client:
                resp = await client.post(self._endpoint, params=params,
                                         headers=headers,
                                         json=build_payload(texts))
        except httpx.HTTPError as exc:
            # 连接重置 / SSL EOF 等瞬时抖动 → 重试而非降级（C19）
            raise TransientTranslationError(str(exc)) from exc

        if 500 <= resp.status_code or resp.status_code == 429:
            raise TransientTranslationError(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise TranslationError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        if len(data) != len(texts):
            raise TranslationError(
                f"返回条数不匹配：请求 {len(texts)}，返回 {len(data)}")
        return [item["translations"][0]["text"] for item in data]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_translate_microsoft.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 真实端点冒烟测试**

```bash
uv run python -c "
import asyncio
from chrometrans.translate.microsoft import MicrosoftTranslator

async def main():
    t = MicrosoftTranslator()
    print(await t.translate(['The derivative of this function is continuous'], 'en', 'zh-Hans'))

asyncio.run(main())
"
```
Expected: 打印一行中文译文。**若失败**：免 key 端点已变更（C17 的前车之鉴），检查 `FREE_ENDPOINT`，并确认 Azure F0 兜底可用。

- [ ] **Step 6: Commit**

```bash
git add src/chrometrans/translate/microsoft.py tests/test_translate_microsoft.py
git commit -m "feat(translate): Microsoft Tier 0/1，传输错误重试而非降级"
```

---

## Task 8: Google 翻译（`translate/google.py`）

Tier 2 兜底。结构上与 Microsoft 一致，独立成文件便于单独替换。

**Files:**
- Create: `src/chrometrans/translate/google.py`
- Test: `tests/test_translate_google.py`

**Interfaces:**
- Consumes: 同 Task 7
- Produces: `GoogleTranslator(api_key: str, timeout_s: float)`，`name` 属性

- [ ] **Step 1: 写失败测试**

`tests/test_translate_google.py`：

```python
import asyncio

import httpx
import pytest

from chrometrans.translate.base import TransientTranslationError
from chrometrans.translate.google import GoogleTranslator


def _client_returning(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_translates_batch_preserving_order(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={
            "data": {"translations": [{"translatedText": "一"},
                                      {"translatedText": "二"}]}
        })

    t = GoogleTranslator(api_key="K", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    out = asyncio.run(t.translate(["a", "b"], "en", "zh-Hans"))

    assert out == ["一", "二"]
    assert seen["body"].count("q=") == 2


def test_language_code_is_google_style(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"data": {"translations": [{"translatedText": "x"}]}})

    t = GoogleTranslator(api_key="K", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    asyncio.run(t.translate(["a"], "en", "zh-Hans"))

    assert "zh-CN" in seen["body"], "Google 用 zh-CN，不是 zh-Hans"


def test_transport_error_is_transient(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("reset")

    t = GoogleTranslator(api_key="K", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    with pytest.raises(TransientTranslationError):
        asyncio.run(t.translate(["a"], "en", "zh-Hans"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_translate_google.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans.translate.google'`

- [ ] **Step 3: 实现**

`src/chrometrans/translate/google.py`：

```python
"""Google Cloud Translation v2：Tier 2 兜底（spec §5.4）。"""
from __future__ import annotations

import httpx

from chrometrans.translate.base import (
    TransientTranslationError,
    TranslationError,
)

ENDPOINT = "https://translation.googleapis.com/language/translate/v2"

# Google 与 Microsoft 的语言代码不同（zh-Hans -> zh-CN）
_LANG_MAP = {"zh-Hans": "zh-CN", "zh-Hant": "zh-TW"}


def _google_lang(code: str) -> str:
    return _LANG_MAP.get(code, code)


class GoogleTranslator:
    def __init__(self, api_key: str, timeout_s: float = 10.0):
        self._key = api_key
        self._timeout_s = timeout_s
        self.name = "google"

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout_s)

    async def translate(self, texts: list[str], src: str, tgt: str) -> list[str]:
        if not texts:
            return []

        body = [("key", self._key), ("source", _google_lang(src)),
                ("target", _google_lang(tgt)), ("format", "text")]
        body += [("q", t) for t in texts]

        try:
            async with self._make_client() as client:
                resp = await client.post(ENDPOINT, content=httpx.QueryParams(body).encode())
        except httpx.HTTPError as exc:
            raise TransientTranslationError(str(exc)) from exc

        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientTranslationError(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise TranslationError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        items = resp.json()["data"]["translations"]
        if len(items) != len(texts):
            raise TranslationError(
                f"返回条数不匹配：请求 {len(texts)}，返回 {len(items)}")
        return [item["translatedText"] for item in items]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_translate_google.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add src/chrometrans/translate/google.py tests/test_translate_google.py
git commit -m "feat(translate): Google Tier 2 兜底"
```

---

## Task 9: ASR（`asr/whisper_engine.py`）

faster-whisper 封装。含三个易错点：必须显式 `vad_filter=False`（C9）、时间戳要还原为绝对时间、幻觉过滤方向不能反（C12）。

**Files:**
- Create: `src/chrometrans/asr/__init__.py`
- Create: `src/chrometrans/asr/whisper_engine.py`
- Test: `tests/test_whisper_engine.py`

**Interfaces:**
- Consumes: `chrometrans.config.AsrConfig`、`chrometrans.audio.segmenter.Segment`
- Produces:
  - `Utterance(start: float, end: float, text: str)`
  - `is_hallucination(no_speech_prob, avg_logprob, compression_ratio, cfg) -> bool`
  - `prepare_cuda_paths() -> None`
  - `WhisperEngine(cfg: AsrConfig)`，方法 `load()` / `transcribe(segment: Segment) -> Utterance | None`

- [ ] **Step 1: 写失败测试**

`tests/test_whisper_engine.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_whisper_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans.asr'`

- [ ] **Step 3: 安装 GPU 依赖**

```bash
uv add faster-whisper ctranslate2 nvidia-cublas-cu12 silero-vad
```

> `silero-vad` 会连带引入 torch / torchaudio（约 2.5 GB）。spec §9 记录了「体积不可接受就 vendor 掉 torch」的备选方案 —— 先按标准安装推进。

- [ ] **Step 4: 实现**

`src/chrometrans/asr/__init__.py`：

```python
"""语音识别。"""
```

`src/chrometrans/asr/whisper_engine.py`：

```python
"""faster-whisper 封装（spec §5.3）。"""
from __future__ import annotations

import os
import site
import sys
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
                continue                              # C6：单段失败不中断整段
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
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_whisper_engine.py -v`
Expected: PASS（7 passed）

- [ ] **Step 6: CUDA 冒烟测试**

```bash
uv run python -c "
import numpy as np
from chrometrans.asr.whisper_engine import WhisperEngine
from chrometrans.audio.segmenter import Segment
from chrometrans.config import AsrConfig

e = WhisperEngine(AsrConfig()); e.load()
print('模型加载成功')
print(e.transcribe(Segment(1, 0.0, 1.0, np.zeros(16000, dtype=np.float32))))
"
```
Expected: 打印「模型加载成功」，随后 `None`（静音被过滤）。**若报 `cublas64_12.dll is not found`** —— `prepare_cuda_paths()` 没生效，检查 `nvidia/cublas/bin` 是否真的存在于 site-packages 下。

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/chrometrans/asr tests/test_whisper_engine.py
git commit -m "feat(asr): faster-whisper 封装，显式 vad_filter=False + 幻觉过滤"
```

---

## Task 10: 编排（`engine.py`）

**Files:**
- Create: `src/chrometrans/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: 前面全部模块
- Produces: `Engine(cfg: Config, on_event: Callable[[dict], None])`，方法 `run()` / `stop()`；事件形状见 spec §5.6

- [ ] **Step 1: 写失败测试**

`tests/test_engine.py`：

```python
import asyncio
from pathlib import Path

import numpy as np
import pytest

from chrometrans.audio.segmenter import Segment
from chrometrans.config import Config, OutputConfig, TranslateConfig
from chrometrans.engine import Engine


class StubTranslator:
    name = "stub"

    def __init__(self, fail=False):
        self.fail = fail

    async def translate(self, texts, src, tgt):
        if self.fail:
            raise RuntimeError("boom")
        return [f"译:{t}" for t in texts]


def _engine(tmp_path, translator, segments, utterance="hello world"):
    events = []

    class StubAsr:
        def load(self): pass
        def transcribe(self, seg):
            from chrometrans.asr.whisper_engine import Utterance
            return Utterance(start=seg.start, end=seg.end, text=utterance)

    eng = Engine(
        cfg=Config(output=OutputConfig(output_root=tmp_path),
                   translate=TranslateConfig()),
        on_event=events.append,
        asr=StubAsr(),
        translator=translator,
        segments=segments,
    )
    return eng, events


def test_emits_cue_and_writes_both_formats(tmp_path):
    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    eng, events = _engine(tmp_path, StubTranslator(), segs)

    eng.run()

    cues = [e for e in events if e["event"] == "cue"]
    assert len(cues) == 1
    assert cues[0]["data"]["source"] == "hello world"
    assert cues[0]["data"]["target"] == "译:hello world"

    session = next(tmp_path.iterdir())
    assert (session / "captions.jsonl").exists()
    assert (session / "captions.srt").exists()
    assert "译:hello world" in (session / "captions.srt").read_text(encoding="utf-8")


def test_translation_failure_keeps_source(tmp_path):
    """spec §6：翻译整链失败时 target 置 null，原文照常保存。"""
    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    eng, events = _engine(tmp_path, StubTranslator(fail=True), segs)

    eng.run()

    cue = [e for e in events if e["event"] == "cue"][0]["data"]
    assert cue["target"] is None
    assert cue["source"] == "hello world"
    assert any(e["event"] == "error" for e in events)


def test_asr_failure_does_not_stop_pipeline(tmp_path):
    """单段 ASR 失败必须跳过并继续（spec §6）。"""
    segs = [Segment(i, float(i), float(i) + 1, np.zeros(16000, dtype=np.float32))
            for i in range(3)]

    class HalfBrokenAsr:
        def __init__(self): self.n = 0
        def load(self): pass
        def transcribe(self, seg):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("cuda oops")
            from chrometrans.asr.whisper_engine import Utterance
            return Utterance(start=seg.start, end=seg.end, text=f"line {self.n}")

    events = []
    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=events.append, asr=HalfBrokenAsr(),
                 translator=StubTranslator(), segments=segs)
    eng.run()

    cues = [e for e in events if e["event"] == "cue"]
    assert len(cues) == 2, "第 2 段失败不应影响第 1、3 段"
    assert any(e["event"] == "error" for e in events)


def test_emits_status_event(tmp_path):
    segs = []
    eng, events = _engine(tmp_path, StubTranslator(), segs)
    eng.run()
    assert any(e["event"] == "status" for e in events)


def test_srt_written_even_for_a_short_session(tmp_path):
    """默认每 20 条才重渲一次；收尾必须补渲，否则短会话根本没有 SRT。"""
    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    eng, _ = _engine(tmp_path, StubTranslator(), segs)
    eng.run()

    session = next(tmp_path.iterdir())
    srt = session / "captions.srt"
    assert srt.exists()
    assert srt.read_text(encoding="utf-8").startswith("1\n")


def test_srt_can_be_rerendered_from_jsonl_alone(tmp_path):
    """spec §7：崩溃后仅凭 JSONL 就能重渲出等价 SRT。"""
    from chrometrans.output.jsonl import JsonlWriter
    from chrometrans.output.srt import render

    segs = [Segment(i, float(i), float(i) + 1, np.zeros(16000, dtype=np.float32))
            for i in range(3)]
    eng, _ = _engine(tmp_path, StubTranslator(), segs)
    eng.run()

    session = next(tmp_path.iterdir())
    assert (session / "captions.srt").read_text(encoding="utf-8") == render(
        JsonlWriter(session / "captions.jsonl").read_all())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans.engine'`

- [ ] **Step 3: 实现**

`src/chrometrans/engine.py`：

```python
"""流水线编排。与显示层、存储层完全解耦（spec §5.6）。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from chrometrans.audio.segmenter import Segment
from chrometrans.config import Config
from chrometrans.models import Cue
from chrometrans.output.jsonl import JsonlWriter, session_dir
from chrometrans.output.srt import render, write_with_fallback


class Engine:
    """把各模块串起来，对外只发 cue / status / error 三种事件。

    依赖以参数注入，便于在无 GPU、无音频设备的环境下单测。
    """

    def __init__(self, cfg: Config, on_event: Callable[[dict], None],
                 asr, translator, segments: Iterable[Segment],
                 segment_iter_factory=None):
        self._cfg = cfg
        self._emit = on_event
        self._asr = asr
        self._translator = translator
        self._segments = segments
        self._segment_iter_factory = segment_iter_factory
        self._running = False

    # ---- 对外 ----

    def run(self) -> None:
        self._running = True
        self._asr.load()

        session = session_dir(self._cfg.output.output_root, datetime.now())
        writer = JsonlWriter(session / "captions.jsonl")
        writer.open()

        self._emit({"event": "status", "data": {
            "state": "running", "model": self._cfg.asr.model,
            "device": self._cfg.asr.device,
            "session": str(session)}})

        cue_id = 0
        source: Iterable[Segment] = (
            self._segment_iter_factory() if self._segment_iter_factory
            else self._segments
        )

        try:
            for segment in source:
                if not self._running:
                    break
                utterance = self._transcribe(segment)
                if utterance is None:
                    continue

                cue_id += 1
                target = self._translate(utterance.text)
                cue = Cue(id=cue_id, start=utterance.start, end=utterance.end,
                          source=utterance.text, target=target,
                          src_lang=self._cfg.translate.src,
                          tgt_lang=self._cfg.translate.tgt)

                try:
                    writer.append(cue)
                except OSError as exc:
                    self._emit({"event": "error",
                                "data": {"message": f"字幕落盘失败，已停止：{exc}"}})
                    break

                self._emit({"event": "cue", "data": cue.to_dict()})
                self._rerender_if_due(session, writer, cue_id)
        finally:
            writer.close()
            # 收尾必须再渲一次：否则不足 srt_rerender_every 条的短会话
            # （上一节课只录到几十句）根本不会产生 SRT 文件
            self._render_srt(session, writer)

        self._emit({"event": "status", "data": {"state": "stopped"}})

    def stop(self) -> None:
        self._running = False

    # ---- 内部 ----

    def _transcribe(self, segment: Segment):
        try:
            return self._asr.transcribe(segment)
        except Exception as exc:
            # 单段失败不得中断流水线（spec §6）
            self._emit({"event": "error", "data": {
                "message": f"第 {segment.index} 段识别失败：{exc}"}})
            return None

    def _translate(self, text: str) -> str | None:
        try:
            results = asyncio.run(self._translator.translate(
                [text], self._cfg.translate.src, self._cfg.translate.tgt))
        except Exception as exc:
            self._emit({"event": "error", "data": {"message": f"翻译失败：{exc}"}})
            return None
        return results[0] if results else None

    def _rerender_if_due(self, session: Path, writer: JsonlWriter,
                         cue_id: int) -> None:
        if cue_id % self._cfg.output.srt_rerender_every != 0:
            return
        self._render_srt(session, writer)

    def _render_srt(self, session: Path, writer: JsonlWriter) -> None:
        """从 JSONL 重渲 SRT。失败不阻塞流水线（JSONL 才是权威源）。"""
        written = write_with_fallback(
            session / "captions.srt", render(writer.read_all()),
            retries=self._cfg.output.replace_retries,
            base_delay=self._cfg.output.replace_base_delay_s)
        if written is None:
            self._emit({"event": "error", "data": {
                "message": "SRT 两处路径都写不进去，本次跳过（JSONL 完好，可随时重渲）"}})
        elif written.name != "captions.srt":
            self._emit({"event": "error", "data": {
                "message": f"captions.srt 被占用，已改写到 {written.name}"}})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_engine.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: Commit**

```bash
git add src/chrometrans/engine.py tests/test_engine.py
git commit -m "feat(engine): 流水线编排，事件解耦 + 单段失败不中断"
```

---

## Task 11: 本地网页（`server.py`）

**Files:**
- Create: `src/chrometrans/server.py`
- Create: `src/chrometrans/static/index.html`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `chrometrans.models.Cue`
- Produces: `create_app(bus) -> FastAPI`，其中 `bus` 是 `EventBus`（`subscribe() -> asyncio.Queue` / `publish(event: dict)`）；`serve(app, host, port)`

- [ ] **Step 1: 写失败测试**

`tests/test_server.py`：

```python
import asyncio

from fastapi.testclient import TestClient

from chrometrans.server import EventBus, create_app


def test_index_page_is_served():
    app = create_app(EventBus())
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_websocket_receives_cue_published_before_connect():
    """页面迟到或中途刷新时，应补看到最近的字幕（spec §5.7）。"""
    bus = EventBus()
    asyncio.run(bus.publish({"event": "cue", "data": {"id": 1, "source": "hi"}}))

    with TestClient(create_app(bus)).websocket_connect("/ws") as ws:
        data = ws.receive_json()

    assert data["event"] == "cue"
    assert data["data"]["id"] == 1


def test_backlog_does_not_replay_status_events():
    """status 是瞬时信号，重放会让刷新后的页面显示过期的运行状态。"""
    bus = EventBus()
    asyncio.run(bus.publish({"event": "status", "data": {"state": "running"}}))
    assert bus.subscribe().empty()


def test_bus_fans_out_to_multiple_subscribers():
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    asyncio.run(bus.publish({"event": "cue", "data": {"id": 7}}))

    assert q1.get_nowait()["data"]["id"] == 7
    assert q2.get_nowait()["data"]["id"] == 7
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans.server'`

- [ ] **Step 3: 安装 Web 依赖**

```bash
uv add fastapi "uvicorn[standard]" websockets httpx
uv add --dev pytest-asyncio
```

- [ ] **Step 4: 实现**

`src/chrometrans/server.py`：

```python
"""本地显示层：FastAPI + WebSocket + 静态页（spec §5.7）。"""
from __future__ import annotations

import asyncio
import collections
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

STATIC_DIR = Path(__file__).parent / "static"


class EventBus:
    """把引擎事件扇出给所有订阅者，并留一份近期 cue 供后连入的页面补看。"""

    def __init__(self, backlog: int = 50) -> None:
        self._backlog: collections.deque[dict] = collections.deque(maxlen=backlog)
        self._queues: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        for event in self._backlog:
            q.put_nowait(event)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    async def publish(self, event: dict) -> None:
        # 只有 cue 进 backlog：status/error 是瞬时信号，重放会让刚刷新的页面
        # 显示过期的运行状态
        if event.get("event") == "cue":
            self._backlog.append(event)
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass          # 前端跟不上就丢，绝不阻塞流水线


def create_app(bus: EventBus) -> FastAPI:
    app = FastAPI(title="chrometrans")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = bus.subscribe()
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(queue)

    return app
```

`src/chrometrans/static/index.html`：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>chrometrans 字幕</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #16181d; color: #e8eaed; }
  header { position: sticky; top: 0; padding: 10px 16px; background: #1f2229;
           border-bottom: 1px solid #2c3038; font-size: 13px; color: #9aa0a6; }
  #cues { padding: 16px; display: flex; flex-direction: column; gap: 14px; }
  .cue { border-left: 3px solid #3b6fd4; padding-left: 12px; }
  .src { font-size: 14px; color: #9aa0a6; }
  .tgt { font-size: 22px; line-height: 1.35; margin-top: 4px; }
  .tgt.missing { color: #6b7280; font-style: italic; font-size: 16px; }
</style>
</head>
<body>
<header id="status">连接中…</header>
<div id="cues"></div>
<script>
const cues = document.getElementById('cues');
const status = document.getElementById('status');
const MAX_CUES = 300;

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { status.textContent = '已连接'; };
  ws.onclose = () => {
    status.textContent = '连接断开，2 秒后重连…';
    setTimeout(connect, 2000);
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.event === 'cue') return addCue(msg.data);
    if (msg.event === 'status') return setStatus(msg.data);
    if (msg.event === 'error') return console.warn('pipeline error:', msg.data.message);
  };
}

function addCue(cue) {
  const el = document.createElement('div');
  el.className = 'cue';
  const src = document.createElement('div');
  src.className = 'src';
  src.textContent = cue.source;
  const tgt = document.createElement('div');
  tgt.className = cue.target ? 'tgt' : 'tgt missing';
  tgt.textContent = cue.target || '（未翻译）';
  el.append(src, tgt);
  cues.append(el);
  while (cues.children.length > MAX_CUES) cues.removeChild(cues.firstChild);
  window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
}

function setStatus(data) {
  if (data.state === 'running') {
    status.textContent = `运行中 · ${data.model} · ${data.device}`;
  } else {
    status.textContent = '已停止';
  }
}

connect();
</script>
</body>
</html>
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_server.py -v`
Expected: PASS（4 passed）

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/chrometrans/server.py src/chrometrans/static tests/test_server.py
git commit -m "feat(server): FastAPI + WebSocket + 本地字幕页，仅绑 127.0.0.1"
```

---

## Task 12: 捕获韧性与降级（`audio/source.py`）

spec §7 的「捕获层专项测试」列了五项尚未覆盖的行为，其中三项在这里补齐：PID 动态重解析、管道断开恢复、静音期不卡死。同时实现 spec §5.1 的降级路径。

**Files:**
- Create: `src/chrometrans/audio/source.py`
- Test: `tests/test_source.py`

**Interfaces:**
- Consumes: `chrometrans.audio.capture.{ProcessAudioStream, find_target_pid, CaptureError, PcmConverter, AudioFormat}`、`chrometrans.config.CaptureConfig`
- Produces:
  - `CaptureSource(cfg, on_event, open_process_stream=None, resolve_pid=None, open_loopback=None, sleep=time.sleep)`，属性 `mode`（`"process"` / `"loopback"`），方法 `chunks() -> Iterator[np.ndarray]` / `stop()`
  - `SystemLoopbackStream(cfg)`，方法 `start()` / `read() -> np.ndarray` / `stop()`
  - `check_signal(chunks: list[np.ndarray], min_rms: float) -> str` — 返回 `"ok"` / `"silent"` / `"no_data"`

- [ ] **Step 1: 写失败测试**

`tests/test_source.py`：

```python
import numpy as np
import pytest

from chrometrans.audio.capture import CaptureError
from chrometrans.audio.source import CaptureSource
from chrometrans.config import CaptureConfig


class FakeStream:
    """可脚本化的假捕获流。"""

    def __init__(self, chunks=(), fail_after=None, raise_on_start=None,
                 raise_on_read=None):
        self._chunks = list(chunks)
        self._fail_after = fail_after
        self._raise_on_start = raise_on_start
        self._raise_on_read = raise_on_read
        self._n = 0
        self.stopped = False

    def start(self):
        if self._raise_on_start:
            raise CaptureError(self._raise_on_start)

    def read(self):
        self._n += 1
        if self._fail_after is not None and self._n > self._fail_after:
            raise CaptureError("pipe broken")
        if self._raise_on_read:
            raise CaptureError(self._raise_on_read)
        return self._chunks[(self._n - 1) % len(self._chunks)]

    def stop(self):
        self.stopped = True


def _source(cfg, events, **kw):
    kw.setdefault("resolve_pid", lambda: 1234)
    kw.setdefault("sleep", lambda _s: None)
    return CaptureSource(cfg, events.append, **kw)


def _take(source, n):
    got = []
    for chunk in source.chunks():
        got.append(chunk)
        if len(got) >= n:
            source.stop()
    return got


def test_check_signal_classifies():
    """spec §9 的启动自检判定。"""
    from chrometrans.audio.source import check_signal

    assert check_signal([], 1e-4) == "no_data"
    assert check_signal([np.zeros(512, dtype=np.float32)], 1e-4) == "silent"
    assert check_signal([np.full(512, 0.05, dtype=np.float32)], 1e-4) == "ok"


def test_self_check_warns_when_the_capture_is_silent():
    """解析到 PID 后先测 RMS，持续静音即报警（spec §9）。

    这是最隐蔽的失败：PID 选错时管道照常工作，只是永远读不到声音。
    """
    events = []
    silent = [np.zeros(512, dtype=np.float32)] * 3
    source = _source(CaptureConfig(self_check_chunks=3), events,
                     open_process_stream=lambda pid: FakeStream(chunks=silent))
    _take(source, 3)

    warned = [e for e in events if e["data"].get("state") == "warning"]
    assert warned, "静音必须告警"
    assert "启动自检未通过" in warned[0]["data"]["message"]


def test_self_check_stays_quiet_on_real_audio():
    events = []
    loud = [np.full(512, 0.05, dtype=np.float32)] * 3
    source = _source(CaptureConfig(self_check_chunks=3), events,
                     open_process_stream=lambda pid: FakeStream(chunks=loud))
    _take(source, 3)

    assert not [e for e in events if e["data"].get("state") == "warning"]


def test_reconnects_when_the_stream_breaks():
    """Chrome 退出 / 管道断开 → 重建流，而不是崩溃（spec §7）。"""
    attempts = {"n": 0}

    def open_stream(pid):
        attempts["n"] += 1
        return FakeStream(chunks=[np.ones(512, dtype=np.float32)],
                          fail_after=2)

    events = []
    source = _source(CaptureConfig(max_reconnect_attempts=5,
                                   self_check_chunks=1), events,
                     open_process_stream=open_stream)
    got = _take(source, 4)

    assert len(got) == 4
    assert attempts["n"] >= 2, "断流后必须重连"
    assert source.mode == "process", "还没到降级阈值，不该降级"


def test_reconnect_backoff_grows_and_is_capped():
    delays = []
    source = _source(CaptureConfig(max_reconnect_attempts=5,
                                   reconnect_base_delay_s=1.0,
                                   reconnect_max_delay_s=4.0),
                     [],
                     open_process_stream=lambda pid: FakeStream(
                         raise_on_start="nope"),
                     open_loopback=lambda: FakeStream(
                         chunks=[np.full(512, 0.5, dtype=np.float32)]),
                     sleep=delays.append)
    _take(source, 1)

    assert delays[:3] == [1.0, 2.0, 4.0]
    assert max(delays) <= 4.0
    assert source.mode == "loopback"


def test_degrades_to_loopback_after_repeated_failures():
    """连续重连失败 → 降级，并显式告知隔离已失效（spec §5.1）。"""
    events = []
    loopback = FakeStream(chunks=[np.full(512, 0.5, dtype=np.float32)])
    source = _source(CaptureConfig(max_reconnect_attempts=2), events,
                     open_process_stream=lambda pid: FakeStream(
                         raise_on_start="nope"),
                     open_loopback=lambda: loopback)
    got = _take(source, 1)

    assert source.mode == "loopback"
    assert got[0][0] == pytest.approx(0.5)

    degraded = [e for e in events if e["data"].get("state") == "degraded"]
    assert degraded, "降级必须发 status 事件"
    assert "降级" in degraded[0]["data"]["message"]


def test_degrades_when_no_process_is_rendering_audio():
    def no_process():
        raise CaptureError("没有进程在渲染音频")

    events = []
    source = _source(CaptureConfig(), events, resolve_pid=no_process,
                     open_loopback=lambda: FakeStream(
                         chunks=[np.full(512, 0.5, dtype=np.float32)]),
                     open_process_stream=lambda pid: FakeStream())
    got = _take(source, 1)

    assert source.mode == "loopback"
    assert got


def test_loopback_fills_silence_instead_of_stalling():
    """C1 / spec §6：底层停流时补静音帧，切句器时序不卡死。"""
    never = lambda: FakeStream(raise_on_read="no data")
    events = []
    source = _source(CaptureConfig(max_reconnect_attempts=1), events,
                     open_process_stream=lambda pid: FakeStream(
                         raise_on_read="no data"),
                     open_loopback=never)
    got = _take(source, 3)

    assert len(got) == 3
    assert all(np.array_equal(c, np.zeros(512, dtype=np.float32)) for c in got)


def test_stop_is_idempotent():
    source = _source(CaptureConfig(), [], open_process_stream=lambda pid: FakeStream())
    source.stop()
    source.stop()          # 不应抛异常
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_source.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans.audio.source'`

- [ ] **Step 3: 实现**

`src/chrometrans/audio/source.py`：

```python
"""捕获韧性：PID 重解析、管道重建、降级到系统级 loopback（spec §5.1 / §6 / §7）。"""
from __future__ import annotations

import time
from typing import Callable, Iterator

import numpy as np

from chrometrans.audio.capture import (
    AudioFormat,
    CaptureError,
    PcmConverter,
    ProcessAudioStream,
    find_target_pid,
)
from chrometrans.config import CaptureConfig


class SystemLoopbackStream:
    """系统级 WASAPI loopback —— 降级路径（spec §5.1）。

    **会混入其他程序的声音**。走 PortAudio，有正常的流式读取接口，但 C1 的
    静音期停流分歧在此路径上并未解决，故调用方必须对读取失败补静音。
    """

    def __init__(self, cfg: CaptureConfig):
        self._cfg = cfg
        self._pa = None
        self._stream = None
        self._converter: PcmConverter | None = None

    def start(self) -> None:
        import pyaudiowpatch as pyaudio

        self._pa = pyaudio.PyAudio()
        device = self._pa.get_default_wasapi_loopback()
        channels = min(2, int(device["maxInputChannels"]))
        rate = int(device["defaultSampleRate"])
        self._stream = self._pa.open(
            format=pyaudio.paFloat32, channels=channels, rate=rate,
            input=True, input_device_index=device["index"],
            frames_per_buffer=1024)
        self._frames_per_buffer = 1024
        self._converter = PcmConverter(AudioFormat(channels, rate, 32),
                                       self._cfg.target_sample_rate)

    def read(self) -> np.ndarray:
        data = self._stream.read(self._frames_per_buffer,
                                 exception_on_overflow=False)
        return self._converter.convert(data)

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


def check_signal(chunks: list[np.ndarray], min_rms: float = 1e-4) -> str:
    """启动自检：判断自检窗口里是否真的读到了声音（spec §9）。

    PID 选错时管道照常工作、只是永远读到零 —— 这是最隐蔽的失败模式，
    所以「没报错」不等于「抓对了」。
    """
    parts = [c for c in chunks if c.size]
    if not parts:
        return "no_data"
    audio = np.concatenate(parts)
    rms = float(np.sqrt(np.mean(audio ** 2)))
    return "ok" if rms >= min_rms else "silent"


class CaptureSource:
    """统一的音频来源：把「捕获失败」变成「重试或降级」，而不是崩溃。"""

    def __init__(self, cfg: CaptureConfig, on_event: Callable[[dict], None],
                 open_process_stream: Callable[[int], object] | None = None,
                 resolve_pid: Callable[[], int] | None = None,
                 open_loopback: Callable[[], object] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._cfg = cfg
        self._emit = on_event
        self._open_process = open_process_stream or self._default_open_process
        self._resolve_pid = resolve_pid or (
            lambda: find_target_pid(cfg.process_names, cfg.probe_seconds))
        self._open_loopback = open_loopback or (lambda: SystemLoopbackStream(cfg))
        self._sleep = sleep
        self._stopped = False
        self._failures = 0
        self.mode = "process"

    def stop(self) -> None:
        self._stopped = True

    # ---- 对外 ----

    def chunks(self) -> Iterator[np.ndarray]:
        while not self._stopped:
            if self.mode == "process":
                yield from self._process_chunks()
            else:
                yield from self._loopback_chunks()

    # ---- 按进程 ----

    def _process_chunks(self) -> Iterator[np.ndarray]:
        try:
            pid = self._resolve_pid()          # 每次重连都重新解析：PID 会变
        except CaptureError as exc:
            self._enter_loopback(f"找不到目标进程：{exc}")
            return

        stream = None
        broke_midway = False
        try:
            stream = self._open_process(pid)
            stream.start()
            self._failures = 0

            # 启动自检（spec §9）：先确认真的读到了有声音的音频，再进常规循环
            head: list[np.ndarray] = []
            try:
                while len(head) < self._cfg.self_check_chunks:
                    head.append(stream.read())
            except CaptureError:
                broke_midway = True

            if not broke_midway:
                verdict = check_signal(head, self._cfg.self_check_min_rms)
                if verdict != "ok":
                    what = "静音" if verdict == "silent" else "空数据"
                    self._emit({"event": "status", "data": {
                        "state": "warning",
                        "message": f"启动自检未通过（读到的是{what}）—— "
                                   f"确认 Chrome 正在播放？PID={pid}"}})
                self._emit({"event": "status", "data": {
                    "state": "running", "capture": "process", "pid": pid}})
                for chunk in head:
                    yield chunk
                while not self._stopped:
                    try:
                        yield stream.read()
                    except CaptureError:
                        broke_midway = True
                        break
        except Exception as exc:
            self._failures += 1
            self._emit({"event": "error", "data": {
                "message": f"按进程捕获失败（第 {self._failures} 次）：{exc}"}})
            self._maybe_degrade()
            return
        finally:
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass

        if broke_midway:
            self._failures += 1
            self._emit({"event": "error", "data": {
                "message": f"捕获流中断（第 {self._failures} 次），正在重连…"}})
        self._maybe_degrade()

    def _maybe_degrade(self) -> None:
        if self._failures >= self._cfg.max_reconnect_attempts:
            self._enter_loopback(
                f"连续 {self._failures} 次重连失败，降级到系统级捕获")
            return
        delay = min(
            self._cfg.reconnect_base_delay_s * (2 ** max(0, self._failures - 1)),
            self._cfg.reconnect_max_delay_s)
        self._sleep(delay)

    def _enter_loopback(self, reason: str) -> None:
        if self.mode == "loopback":
            return
        self.mode = "loopback"
        self._failures = 0
        # §5.1：降级后隔离失效。必须显式告知，否则用户以为隔离还在生效 ——
        # 这是整个设计里最危险的失败方式
        self._emit({"event": "status", "data": {
            "state": "degraded", "capture": "loopback",
            "message": f"已降级到系统级捕获：会混入 Edge、背景音乐等声音。"
                       f"原因：{reason}"}})

    def _default_open_process(self, pid: int) -> ProcessAudioStream:
        return ProcessAudioStream(
            pid=pid, pipe_name=self._cfg.pipe_name,
            buffer_bytes=self._cfg.read_buffer_bytes,
            target_rate=self._cfg.target_sample_rate)

    # ---- 降级路径 ----

    def _loopback_chunks(self) -> Iterator[np.ndarray]:
        try:
            stream = self._open_loopback()
            stream.start()
        except Exception as exc:
            self._emit({"event": "error", "data": {
                "message": f"降级路径也启动失败：{exc}"}})
            self._sleep(self._cfg.loopback_retry_delay_s)
            return

        try:
            while not self._stopped:
                try:
                    yield stream.read()
                except Exception:
                    # C1：底层无数据时补静音，别让切句器的时序卡死（spec §6）
                    yield np.zeros(self._cfg.silence_fill_samples,
                                   dtype=np.float32)
        finally:
            try:
                stream.stop()
            except Exception:
                pass
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_source.py -v`
Expected: PASS（9 passed）

- [ ] **Step 5: 真实降级联调**

关掉 Chrome 的声音（或直接退出 Chrome），运行 `uv run chrometrans`。

Expected: 终端打印「已降级到系统级捕获：会混入 Edge、背景音乐等声音…」。此时用 Edge 放声音，字幕应能出来（但会混入其他程序的声音 —— 这是降级路径的已知代价）。

- [ ] **Step 6: Commit**

```bash
git add src/chrometrans/audio/source.py tests/test_source.py
git commit -m "feat(capture): PID 重解析、断流重连、降级到系统级 loopback"
```

---

## Task 13: 入口与端到端联调（`cli.py`）

把所有部件接起来，做成可运行的命令行程序。这是**唯一必须在真实网课场景下手动验证**的任务。

**Files:**
- Create: `src/chrometrans/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: 全部
- Produces: `main() -> int`（`[project.scripts]` 入口）；`build_runtime(cfg) -> tuple[Engine, EventBus]`

- [ ] **Step 1: 写失败测试**

`tests/test_cli.py`：

```python
import pytest

from chrometrans.cli import build_translator_chain, parse_args
from chrometrans.config import TranslateConfig


def test_parse_args_defaults():
    args = parse_args([])
    assert args.host == "127.0.0.1"
    assert args.no_server is False


def test_parse_args_rejects_non_loopback_host():
    """C25：只允许绑回环地址。"""
    with pytest.raises(SystemExit):
        parse_args(["--host", "0.0.0.0"])


def test_free_tier_only_when_no_keys():
    cfg = TranslateConfig(azure_key=None, google_key=None)
    chain = build_translator_chain(cfg)
    assert len(chain._providers) == 1
    assert chain._providers[0].name == "microsoft-free"


def test_full_chain_when_all_keys_present():
    cfg = TranslateConfig(azure_key="A", google_key="G")
    chain = build_translator_chain(cfg)
    assert [p.name for p in chain._providers] == [
        "microsoft-free", "microsoft-azure", "google"]


def test_publish_drops_events_before_the_server_is_up():
    """uvicorn 还没起来时直接丢弃，绝不阻塞引擎线程。"""
    from chrometrans.cli import publish
    from chrometrans.server import EventBus

    publish(EventBus(), None, {"event": "cue", "data": {}})   # 不应抛异常
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chrometrans.cli'`

- [ ] **Step 3: 实现**

`src/chrometrans/cli.py`：

```python
"""命令行入口。"""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from dataclasses import replace
from typing import Callable, Iterator

from chrometrans.audio.segmenter import Segment, Segmenter
from chrometrans.audio.source import CaptureSource
from chrometrans.config import Config, load_config
from chrometrans.engine import Engine
from chrometrans.translate.base import ChainTranslator
from chrometrans.translate.google import GoogleTranslator
from chrometrans.translate.microsoft import MicrosoftTranslator

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="chrometrans", description="Chrome 网课实时字幕")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-server", action="store_true",
                        help="不启动网页，只识别与落盘")
    parser.add_argument("--process", default="chrome.exe")
    args = parser.parse_args(argv)
    if args.host not in LOOPBACK_HOSTS:
        parser.error(f"只允许绑定回环地址（C25），收到：{args.host}")
    return args


def build_translator_chain(cfg) -> ChainTranslator:
    """Tier 0 免 key → Tier 1 Azure → Tier 2 Google。绝不能只有 Tier 0（C17）。"""
    providers = [MicrosoftTranslator(api_key=None, region=cfg.azure_region,
                                     timeout_s=cfg.timeout_s)]
    if cfg.azure_key:
        providers.append(MicrosoftTranslator(api_key=cfg.azure_key,
                                             region=cfg.azure_region,
                                             timeout_s=cfg.timeout_s))
    if cfg.google_key:
        providers.append(GoogleTranslator(api_key=cfg.google_key,
                                          timeout_s=cfg.timeout_s))
    return ChainTranslator(providers, cfg)


def segment_source(cfg: Config, emit: Callable[[dict], None],
                   process_names: tuple[str, ...]
                   ) -> Callable[[], Iterator[Segment]]:
    """音频 → 语音段。重连与降级由 CaptureSource 负责（Task 12）。"""
    def factory() -> Iterator[Segment]:
        capture_cfg = replace(cfg.capture, process_names=process_names)
        source = CaptureSource(capture_cfg, emit)
        segmenter = Segmenter(cfg.segmenter)
        try:
            for chunk in source.chunks():
                if chunk.size:
                    yield from segmenter.feed(chunk)
        finally:
            yield from segmenter.flush()
            source.stop()
    return factory


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config()

    from chrometrans.asr.whisper_engine import WhisperEngine
    from chrometrans.server import EventBus, create_app

    bus = EventBus()
    loop: asyncio.AbstractEventLoop | None = None

    if not args.no_server:
        import uvicorn

        def _serve() -> None:
            nonlocal loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            # loop="asyncio" 阻止 uvicorn 另起一个循环，否则下面的 loop 指向的
            # 是个空转的循环，所有事件都会被投递到没人消费的地方
            config = uvicorn.Config(create_app(bus), host=args.host,
                                    port=args.port, log_level="warning",
                                    loop="asyncio")
            loop.run_until_complete(uvicorn.Server(config).serve())

        threading.Thread(target=_serve, daemon=True).start()
        print(f"字幕页： http://{args.host}:{args.port}/")

    def emit(event: dict) -> None:
        print_event(event)
        publish(bus, loop, event)

    engine = Engine(cfg=cfg, on_event=emit,
                    asr=WhisperEngine(cfg.asr),
                    translator=build_translator_chain(cfg.translate),
                    segments=(),
                    segment_iter_factory=segment_source(
                        cfg, emit, (args.process,)))

    try:
        engine.run()
    except KeyboardInterrupt:
        engine.stop()
    return 0


def print_event(event: dict) -> None:
    data = event.get("data", {})
    if event["event"] == "cue":
        print(f"[{data['start']:6.2f}] {data['source']}")
        if data.get("target"):
            print(f"         {data['target']}")
    elif event["event"] == "error" or data.get("state") == "degraded":
        print(f"! {data.get('message')}", file=sys.stderr)


def publish(bus, loop: asyncio.AbstractEventLoop | None, event: dict) -> None:
    """把事件投给 uvicorn 的事件循环。

    引擎跑在主线程、uvicorn 跑在后台线程，**两者的循环不同** —— 必须用
    `run_coroutine_threadsafe` 跨线程投递。`create_task` 在这里会静默丢事件，
    网页永远看不到字幕。
    """
    if loop is None or not loop.is_running():
        return                       # 服务还没起来，丢弃即可
    try:
        asyncio.run_coroutine_threadsafe(bus.publish(event), loop)
    except RuntimeError:
        pass                         # 循环正在关闭


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 全量测试**

Run: `uv run pytest -v`
Expected: 全部 PASS。`integration` 标记的用例若失败，先确认 Chrome 在播放。

- [ ] **Step 6: 端到端联调（手工）**

```bash
uv run chrometrans
```

同时：Chrome 打开网课（有英文语音），浏览器再开一个标签访问 `http://127.0.0.1:8765/`。

逐项确认：
1. 终端打印 `字幕页： http://127.0.0.1:8765/`
2. 网页顶部显示「运行中 · large-v3-turbo · cuda」
3. 说英文时网页出现新卡片，英文在上、中文在下
4. 关掉网页，终端仍继续打印字幕（显示层可选，spec §6）
5. 暂停 Chrome 播放 → 不再出新卡片，但程序不崩
6. Ctrl+C 能干净退出
7. `transcripts/<时间戳>/` 下同时有 `captions.jsonl` 和 `captions.srt`，SRT 能被播放器打开且不错位

- [ ] **Step 7: 后台播放验证隔离**

在 Chrome 放网课的同时，用 Edge 放另一个有声音的视频，并开一个音乐播放器。

Expected: 字幕**只有网课内容**，不含 Edge 与音乐。若混入 —— 说明降级到了系统级 loopback，检查启动日志与 `find_target_pid` 的返回值。

- [ ] **Step 8: Commit**

```bash
git add src/chrometrans/cli.py tests/test_cli.py
git commit -m "feat(cli): 命令行入口 + 端到端联调"
```

---

## 收尾检查（不属于任何单个任务）

实现完毕后逐项确认，这些是 spec 里明确要求但横跨多任务的事项：

- [ ] **长时 soak（C4）**：连续跑 >2 小时，确认无音频劣化、无内存增长、SRT 持续重渲正常。这是 OBS 同源路径的已知风险，**不能跳过**。
- [ ] **静音期长时行为（C1）**：短测已确认持续输出零帧，需在 >2 小时运行中复现确认。
- [ ] **`README.md`** 仍是空的，补上安装与运行说明（含 Azure/Google key 的环境变量名）。
- [ ] **`.gitignore`** 确认含 `transcripts/` 与 `captions.next.srt`。
- [ ] **回填 spec**：Task 5 用「直接调 silero 模型取概率」替代了 spec §5.2 写的
      「`VADIterator` 只负责门控」，理由是该节同时要求的超长段切点规则需要每帧概率。
      把这条修正写回 spec §5.2 与 §3.1 的约束表（新增一条 C30）。
- [ ] **翻译凭据（必需，不是可选）**：申请 Azure F0 与 Google Cloud 的 key，
      并设好对应环境变量。**没有 key 就没有任何中文翻译** —— 免 key 层实测
      5/5 返回 HTTP 401（连伪造 key 也一样），已失效。此时程序仍会正常识别英文
      并落盘（`target` 为 `null`），但网页上每条都是「（未翻译）」。
- [ ] **首次运行下载模型**：`large-v3-turbo` 约 1.6 GB。若 HuggingFace Xet 传输卡在
      0 字节，需设 `HF_HUB_DISABLE_XET=1`（spec §8）。
- [ ] **平台在窗口隐藏时是否自动暂停播放**：spec §9 列为「需实测」。把 Chrome 网课
      窗口**最小化**（不是被遮挡）后观察是否还有字幕。若会暂停，运行期间需保持窗口
      可见 —— 可以放在其他窗口后面，但不能最小化。
- [ ] **pin `process-audio-capture` 版本**：整个主捕获路径压在该库的未文档化行为上
      （§9），必须锁死版本，不能让它随 `>=1.0.0` 漂移。

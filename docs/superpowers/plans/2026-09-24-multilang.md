# 多语言支持实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 CLI 与 GUI 都能选择音频语言（en / ru / zh），幻觉过滤阈值随语言标定且有据可查，中文音频走单语模式并保证简体输出。

**Architecture:** `LanguageProfile` 是源语言两侧（`asr.language` 与 `translate.src`）的唯一权威，`load_config(language)` 是它进入 `Config` 的唯一入口，不变量收在这一个函数上并由测试逐个 profile 锁住。阈值先用标定工具在真实素材上测出来再写进代码，标定报告进仓库作为依据。单语模式不新增会话级参数——`Cue.tgt_lang is None` 本身自描述，四处渲染器从数据派生，避免再造一个需要保持同步的状态。

**Tech Stack:** Python 3.13 / faster-whisper (`large-v3-turbo`) / silero-vad / PySide6 / pytest / argparse；标定工具复用生产同一条解码参数路径。

**Spec:** `docs/superpowers/specs/2026-09-24-multilang-design.md`（约束 C38–C46；本次任务与规格一起读）

---

## 与规格的三处偏差（先看这里）

规格 §10 的改动清单在下面三处**按原样做不到或明显更差**，本计划按下述方式落地。每处都给了理由；不同意就改回来，改动只影响对应任务。

1. **`scripts/calibrate.py` → `src/chrometrans/calibrate.py`。**
   `pyproject.toml` 的 `[tool.hatch.build.targets.wheel] packages = ["src/chrometrans"]`，`scripts/` 不进包。console script 的入口必须指向可导入的模块，写 `scripts.calibrate:main` 装完之后必然 ImportError。所以模块放包内，命令名仍是规格里的 `chrometrans-calibrate`。

2. **渲染器的 `bilingual` 参数取消，改为从 `Cue.tgt_lang` 派生。**
   规格 §5.3 要求四处渲染器收一个会话级 `bilingual` 参数。但 §5.3 同时定义了 `tgt_lang is None ⟺ 本会话单语`——参数与数据是同一件事的两种表达，而规格自己在 §5.1 对 `translate_src` 说过「少一个需要和别的字段保持同步的状态，就没地方能不同步」。更要紧的是：原规格 §7 的契约「崩溃后仅凭 JSONL 就能重渲出等价 SRT」在 `test_engine.py:119-146` 有测试锁着，那个调用点只有 `render(writer.read_all())`，没有会话级参数可传。派生则两边都成立。
   `bilingual` 仍按 §5.3 进 `running` 状态事件（launcher 需要它显示「中文 · 不翻译」）。

3. **新增 `dropped.jsonl`（规格没要求的落盘）。**
   C45 的论据是「它是『先按默认值、试用后再调』这一约定能够成立的唯一前提」。但事件是瞬时的：字幕窗里那条提示会被下一句顶走（`Transcript` 上限 300 条），网页同理，而捕获期间启动器窗口是 `hide()` 的。GUI 用户「试完再回来调阈值」时手里什么都没有。所以除事件外，被丢弃的段另落一份 `dropped.jsonl` 到会话目录，与 `captions.srt` / `captions.md` 同为派生视图。

---

## Global Constraints

以下每条对本计划的每个任务都成立，不再逐任务重复。

- **C38** 语言集合封闭为 `en` / `ru` / `zh` 白名单。未知 code **拒绝启动并列出可用值**，不得退回默认语言。
- **C39** `asr.language` 与 `translate.src` 必须由同一份 `LanguageProfile` 派生，唯一入口是 `load_config`。二者不同步 = 用英语模型听俄语音频 = 编造英文。
- **C40** 每个 profile 必填非空 `calibrated_on`，写明这组数字的依据。「实测后决定沿用上游默认值」是合法产出，前提是真的测过。
- **C41** 标定以「误杀率 = 0」为唯一硬判据；漏放率尽力而为，不设硬指标。
- **C42** 合成的平坦噪声**不得**作为负样本（切句器不放它过去，见规格 §6.1）。它唯一的用途是 `gate` 子命令里的对照。
- **C43** 单语模式是**会话级**属性，四处渲染器（`output/srt.py`、`output/markdown.py`、`gui/caption_window.py`、`static/index.html`）必须一致表现。
- **C44** 故意不翻译**不得**表现为「翻译失败」，不得出现「（未翻译）」。
- **C45** 幻觉丢弃必须可见：发 `status` 事件带被丢弃的文本，并落 `dropped.jsonl`。
- **C46** 中文输出必须为简体。`initial_prompt` 定向，**并实测繁体残留率**；非零才补确定性转换（不得依赖网络翻译）。
- **C9** `vad_filter=False` 必须显式传。
- **C12** 幻觉段跳过、不中断整段；`compression_ratio` 高 = 幻觉，方向不得反。
- **C22** SRT 的 cue 内绝不出现空行。
- **C25** 只允许绑回环地址。
- **C36** GUI 只做摆放；判断在别的模块里。`gui/settings.py` **不 import Qt**。
- 测试约定：`addopts = -m 'not integration and not gui'`，两个 marker 已在 `pyproject.toml:37-43`。`gui` marker 的用例用 `QT_QPA_PLATFORM=offscreen`。**本计划新增的测试都不需要 GPU。**
- 全 UTF-8。在 Bash 工具里跑中文输出会变乱码——**凡是要人看的结果一律写文件再用 Read 读**，所以本计划的两个子命令都把 `--out` 设为必填。

---

## 文件结构

| 文件 | 职责 | 本计划中的变化 |
|---|---|---|
| `src/chrometrans/config.py` | 全部可调参数；语言画像 | 新增 `LanguageProfile` / `LANGUAGES` / `DEFAULT_LANGUAGE`；`load_config(language)` |
| `src/chrometrans/calibrate.py` | 标定工具（新） | 素材构造、统计与推荐、跑 Whisper、CLI |
| `src/chrometrans/asr/whisper_engine.py` | faster-whisper 封装 | `transcribe_kwargs()` 抽出；`transcribe` 返回 `TranscribeResult` |
| `src/chrometrans/models.py` | 跨模块数据模型 | `Cue.tgt_lang` 放宽为 `str \| None`；新增 `is_monolingual()` |
| `src/chrometrans/engine.py` | 流水线编排 | 丢弃事件 + `dropped.jsonl`；单语模式；`bilingual` 状态字段 |
| `src/chrometrans/output/srt.py` | SRT 派生视图 | 单语分支 |
| `src/chrometrans/output/markdown.py` | Markdown 派生视图 | 单语分支（含表头） |
| `src/chrometrans/cli.py` | 命令行入口 | `--language`；`dropped` 文案 |
| `src/chrometrans/gui/settings.py` | 设置持久化 | `language` 字段 + 白名单校验 |
| `src/chrometrans/gui/launcher.py` | 启动器窗口 | 语言下拉 + `selected_language()` |
| `src/chrometrans/gui/app.py` | 接线 | 语言 → `load_config`；启动恢复 / 停止保存 |
| `src/chrometrans/gui/caption_window.py` | 悬浮字幕窗 | 单语分支；丢弃提示行 |
| `src/chrometrans/static/index.html` | 网页显示 | 单语分支；丢弃提示 |
| `docs/superpowers/calibration/` | 标定报告（新） | ru / zh 两份 |

---

### Task 1: `config.py` 的 `LanguageProfile` 与 `load_config` 联动入口

只放 `en` 一份。`ru` / `zh` 由 Task 4 在拿到实测数字之后加入——**白名单只在有数据支撑时才增长**，这是 C38 与 C40 合起来的意思。

**Files:**
- Modify: `src/chrometrans/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `LanguageProfile`（frozen dataclass，字段见下）
  - `LANGUAGES: dict[str, LanguageProfile]`、`DEFAULT_LANGUAGE: str = "en"`
  - `load_config(language: str = DEFAULT_LANGUAGE) -> Config`（未知 code 抛 `ValueError`）
  - `AsrConfig.initial_prompt: str | None = None`
  - `TranslateConfig.src: str | None`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_config.py`：

```python
import pytest

from chrometrans.config import DEFAULT_LANGUAGE, LANGUAGES, load_config


def test_every_profile_is_fully_derived_from_itself():
    """C39 的锁：源语言两侧必须来自同一份 profile。

    asr.language 与 translate.src 不同步的后果是用英语模型听俄语音频 ——
    Whisper 不报错，它会输出一段通顺但完全编造的英文。
    """
    for code, profile in LANGUAGES.items():
        cfg = load_config(code)
        assert cfg.asr.language == profile.asr_language
        assert cfg.translate.src == profile.translate_src
        assert cfg.asr.initial_prompt == profile.initial_prompt
        assert cfg.asr.no_speech_prob_threshold == profile.no_speech_prob_threshold
        assert cfg.asr.avg_logprob_threshold == profile.avg_logprob_threshold
        assert (cfg.asr.compression_ratio_threshold
                == profile.compression_ratio_threshold)


def test_every_profile_has_calibration_evidence():
    """C40：每门语言的阈值都要有人为它负责。加语言忘标定 = 这条红。"""
    for code, profile in LANGUAGES.items():
        assert profile.calibrated_on.strip(), f"{code} 没有标定依据"
        for name in ("no_speech_prob_threshold", "avg_logprob_threshold",
                     "compression_ratio_threshold"):
            assert isinstance(getattr(profile, name), float), f"{code}.{name}"
        assert profile.code == code
        assert profile.label.strip()


def test_unknown_language_is_rejected_not_defaulted():
    """C38：未知值不得退回默认语言 —— 那等于静默用了另一门语言的阈值。"""
    with pytest.raises(ValueError) as exc:
        load_config("klingon")

    message = str(exc.value)
    assert "klingon" in message
    assert "en" in message, "要列出可用值"


def test_default_language_is_english():
    assert DEFAULT_LANGUAGE == "en"
    assert load_config().asr.language == "en"
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL —`ImportError: cannot import name 'DEFAULT_LANGUAGE'`

- [ ] **Step 3: 实现**

`src/chrometrans/config.py`——`SegmenterConfig` 之后插入：

```python
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
```

`AsrConfig` 加一个字段（放在 `language` 之后，与 profile 的字段顺序对应）：

```python
    initial_prompt: str | None = None
```

`TranslateConfig.src` 改类型：

```python
    src: str | None = "en"
```

`load_config` 整体替换：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS（含原有两条）

- [ ] **Step 5: 跑全量测试确认没有连带破坏**

Run: `uv run pytest -q`
Expected: PASS。若有失败，多半是某处手工构造 `TranslateConfig(src=None)` 之外的地方断言了 `src == "en"`——那仍是 `en` profile 的值，应当照旧通过。

- [ ] **Step 6: 提交**

```bash
git add src/chrometrans/config.py tests/test_config.py
git commit -m "feat(config): LanguageProfile 与 load_config 的语言联动入口"
```

---

### Task 2: `whisper_engine` 抽出解码参数 + `transcribe` 返回结果对象

两件事同属一个模块、同一个理由：标定必须走**生产同一份**解码参数，否则标的不是线上那件事；而丢弃的段必须带回来，因为统计量只有这里拿得到（C45 / spec §5.4）。

**Files:**
- Modify: `src/chrometrans/asr/whisper_engine.py`
- Modify: `src/chrometrans/engine.py`（只改调用点解出 `.utterance`，丢弃处理归 Task 5）
- Modify: `tests/test_engine.py`（三个 stub 改成返回 `TranscribeResult`）
- Test: `tests/test_whisper_engine.py`

**Interfaces:**
- Consumes: `AsrConfig.initial_prompt`（Task 1）
- Produces:
  - `transcribe_kwargs(cfg: AsrConfig) -> dict`
  - `Dropped`（frozen dataclass：`text`, `no_speech_prob`, `avg_logprob`, `compression_ratio`）
  - `TranscribeResult`（frozen dataclass：`utterance: Utterance | None`, `dropped: tuple[Dropped, ...] = ()`）
  - `WhisperEngine.transcribe(segment) -> TranscribeResult`
  - `Engine._transcribe(segment) -> TranscribeResult | None`（异常时仍是 `None`）

为什么要连消费侧一起改（**接口变更任务拥有它的消费者**）：只改 `whisper_engine.py`
的话，`engine.py:68` 的 `if utterance is None` 会把 `TranscribeResult` 当
`Utterance` 用，`utterance.text` 抛 AttributeError，被 `_transcribe` 的 except 吞掉
——**每一段都报「识别失败」，一条字幕都出不来**。而测试是绿的，因为
`tests/test_engine.py` 的 stub 要到 Task 5 才改。测试全绿而应用是坏的，正是 C45
整套论证要消灭的那种失败，所以这个交接不留给下一个任务。

本任务只把调用点解出 `.utterance`，**`result.dropped` 先原地丢掉** —— 把它变成事件
与 `dropped.jsonl` 是 Task 5 的事。在此之前「丢弃不发声」是既有状态，不是本任务
引入的回归。

- [ ] **Step 1: 写失败的测试**

`tests/test_whisper_engine.py`——先把现有四处按新返回值改掉（1-4 行号是改动前的）：

| 位置 | 现在 | 改成 |
|---|---|---|
| `test_transcribe_returns_none_when_filtered` 的断言 | `engine.transcribe(_seg()) is None` | `.utterance is None`，并加一行断言丢弃文本 |
| `test_transcribe_joins_multiple_whisper_segments` | `utt = engine.transcribe(...)` | `utt = engine.transcribe(...).utterance`，其后 `.text/.start/.end` 不动 |
| `test_transcribe_returns_none_on_empty_text` | 同第一条 | `.utterance is None` |
| `test_transcribe_keeps_good_segments_when_some_are_hallucinated` | `utt = engine.transcribe(...)` | `utt = engine.transcribe(...).utterance` |

`test_transcribe_returns_none_when_filtered` 顺带加一行——它喂的正是「被丢弃的段」：

```python
    result = engine.transcribe(_seg())

    assert result.utterance is None
    assert [d.text for d in result.dropped] == ["repeated"]
```

（原来的 `assert engine.transcribe(_seg()) is None` 换成上面两行。）

然后追加两个共用 helper 与四条新用例：

```python
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
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `uv run pytest tests/test_whisper_engine.py -v`
Expected: FAIL —`ImportError: cannot import name 'TranscribeResult'`（先改 import 再加实现，报错才是可读的）

- [ ] **Step 3: 实现**

`src/chrometrans/asr/whisper_engine.py`——在 `Utterance` 之后插入：

```python
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
```

在 `is_hallucination` 之前插入：

```python
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
```

`transcribe` 整体替换为：

```python
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
```

- [ ] **Step 4: 改消费侧（同一轮，不留红旗）**

`src/chrometrans/engine.py`——主循环里 `utterance = self._transcribe(segment)` 的两行：

```python
                result = self._transcribe(segment)
                if result is None:
                    continue
                # 本任务只解出识别结果；result.dropped 由 Task 5 变成事件与
                # dropped.jsonl。在此之前「丢弃不发声」是既有状态。
                utterance = result.utterance
                if utterance is None:
                    continue
```

`_transcribe` 的文档字符串补一句（返回值类型变了）：

```python
    def _transcribe(self, segment: Segment):
        """返回 TranscribeResult；单段失败时返回 None（不得中断流水线，spec §6）。"""
```

`tests/test_engine.py`——三个 stub 改成同一份契约：

```python
    class StubAsr:
        def load(self): pass

        def transcribe(self, seg):
            from chrometrans.asr.whisper_engine import TranscribeResult, Utterance
            return TranscribeResult(
                utterance=Utterance(start=seg.start, end=seg.end, text=utterance))
```

`HalfBrokenAsr` 同样包一层 `TranscribeResult(utterance=...)`；返 `None` 表示「这段什么都没出」的那处（`RecordingAsr.transcribe`）改成 `return TranscribeResult(utterance=None)` —— 注意与「识别失败」不同：stub 里的 `None` 一直是「空结果」，而 `_transcribe` 的 `None` 是异常路径，两者在引擎里走同一条 `continue`。

- [ ] **Step 5: 跑测试确认通过（两个文件一起）**

Run: `uv run pytest tests/test_whisper_engine.py tests/test_engine.py -v`
Expected: 两个文件都 PASS

- [ ] **Step 6: 提交**

```bash
git add src/chrometrans/asr/whisper_engine.py src/chrometrans/engine.py \
        tests/test_whisper_engine.py tests/test_engine.py
git commit -m "feat(asr): 抽出 transcribe_kwargs，transcribe 返回 TranscribeResult"
```

---

### Task 3: `calibrate.py` 第 1、2 块——素材构造与统计推荐（纯函数）

这一块**完全离线可测**，不碰 GPU、不碰 silero。它是 Task 4 的判据来源，所以先把判据做成可测的函数，再让它去跑真实音频。

**Files:**
- Create: `src/chrometrans/calibrate.py`
- Test: `tests/test_calibrate.py`

**Interfaces:**
- Consumes: `is_hallucination`（`asr/whisper_engine.py`）、`AsrConfig`（`config.py`）
- Produces:
  - `SPEECH = "speech"`、`NONSPEECH = "nonspeech"`
  - `Thresholds`（frozen：`no_speech_prob`, `avg_logprob`, `compression_ratio`；`.as_asr_config() -> AsrConfig`）
  - `UPSTREAM_BASELINE: Thresholds`（`0.6 / -1.0 / 2.4`）
  - `Row`（frozen：`index`, `start`, `end`, `text`, `no_speech_prob`, `avg_logprob`, `compression_ratio`, `label=SPEECH`；`.from_dict()`, `.to_dict()`）
  - `Confusion`（frozen：`true_speech`, `false_drop`, `false_pass`, `true_nonspeech`；属性 `positives`, `negatives`, `false_drop_rate`, `false_pass_rate`）
  - `confusion(rows, thresholds) -> Confusion`
  - `dropped_rows(rows, thresholds) -> list[Row]`
  - `Recommendation`（frozen：`baseline`, `chosen`, `baseline_confusion`, `chosen_confusion`, `note`）
  - `recommend(rows, baseline=UPSTREAM_BASELINE) -> Recommendation`
  - `synthetic_clip(seconds, dbfs, seed=0) -> np.ndarray`、`digital_silence(seconds) -> np.ndarray`
  - `TRADITIONAL_MARKERS: frozenset[str]`、`traditional_ratio(texts) -> tuple[float, list[str]]`
  - `save_rows(path, meta, rows)`、`load_rows(path) -> tuple[dict, list[Row]]`

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_calibrate.py`：

```python
import numpy as np
import pytest

from chrometrans.calibrate import (
    NONSPEECH,
    SPEECH,
    UPSTREAM_BASELINE,
    Confusion,
    Row,
    Thresholds,
    confusion,
    digital_silence,
    dropped_rows,
    load_rows,
    recommend,
    save_rows,
    synthetic_clip,
    traditional_ratio,
)


def _row(text, nsp, alp, cr, label=SPEECH):
    return Row(index=1, start=0.0, end=1.0, text=text, no_speech_prob=nsp,
               avg_logprob=alp, compression_ratio=cr, label=label)


def test_upstream_baseline_matches_the_en_profile():
    """标定的基准就是 en 那组数字。两处各写一遍迟早会分叉。"""
    from chrometrans.config import LANGUAGES

    en = LANGUAGES["en"]
    assert UPSTREAM_BASELINE == Thresholds(
        no_speech_prob=en.no_speech_prob_threshold,
        avg_logprob=en.avg_logprob_threshold,
        compression_ratio=en.compression_ratio_threshold)


def test_confusion_classifies_the_four_ways():
    rows = [
        _row("真话", 0.05, -0.2, 1.2),                       # 真语音，留
        _row("被误杀的真话", 0.9, -1.5, 1.2),                 # 真语音，丢
        _row("放过去的幻觉", 0.05, -0.2, 1.2, NONSPEECH),     # 非语音，留
        _row("抓到的幻觉", 0.9, -1.5, 1.2, NONSPEECH),        # 非语音，丢
    ]

    c = confusion(rows, UPSTREAM_BASELINE)

    assert (c.true_speech, c.false_drop, c.false_pass, c.true_nonspeech) == (1, 1, 1, 1)
    assert c.positives == 2 and c.negatives == 2
    assert c.false_drop_rate == 0.5
    assert c.false_pass_rate == 0.5


def test_rates_are_none_or_zero_when_a_side_is_empty():
    """零正样本 / 零负样本都要能算，不能 ZeroDivisionError。"""
    c = Confusion(true_speech=0, false_drop=0, false_pass=0, true_nonspeech=3)
    assert c.positives == 0
    assert c.false_drop_rate == 0.0
    assert c.false_pass_rate == 0.0, "有负样本、漏放为 0 —— 是 0，不是「测不出来」"

    c = Confusion(true_speech=5, false_drop=0, false_pass=0, true_nonspeech=0)
    assert c.negatives == 0
    assert c.false_drop_rate == 0.0
    assert c.false_pass_rate is None, "没有负样本时漏放率是「测不出来」，不是 0"


def test_recommend_keeps_the_baseline_when_there_are_no_negatives():
    """C41：负样本为空时漏放无法度量，凭正样本单侧收紧是在看不见的那一侧下注。"""
    # 这条行必须是被基准**留下**的（nsp/alp/cr 都在门限内），否则基准自己就是
    # 误杀，那时该做的是放宽、不是保持 —— 见下面 loosens 那条的同一个行。
    rows = [_row("人说话", 0.05, -0.2, 1.5)]

    rec = recommend(rows)

    assert rec.chosen == UPSTREAM_BASELINE
    assert "负样本" in rec.note
    assert rec.chosen_confusion.false_drop == 0


def test_recommend_loosens_when_the_baseline_kills_real_speech():
    """C41：误杀是硬判据。基准阈值杀掉真语音时，推荐值必须能救回来。"""
    rows = [_row("真语音", 0.7, -1.2, 1.5)]

    rec = recommend(rows)

    assert rec.baseline_confusion.false_drop == 1, "前提：基准确实误杀了"
    assert rec.chosen_confusion.false_drop == 0
    assert rec.chosen != UPSTREAM_BASELINE


def test_recommend_prefers_the_tightest_that_still_kills_no_real_speech():
    """误杀为 0 的候选里挑漏放最少的（C41 的拐点）。"""
    rows = [
        _row("真语音", 0.05, -0.2, 1.2),
        _row("抓得到的幻觉", 0.95, -1.8, 1.2, NONSPEECH),
    ]

    rec = recommend(rows)

    assert rec.chosen_confusion.false_drop == 0
    assert rec.chosen_confusion.false_pass == 0, "这组负样本是能抓到的，不该放过"


def test_recommend_says_so_when_the_grid_cannot_reach_zero_false_drops():
    """网格里没有一组能做到误杀 = 0 时，不许假装有 —— 保持原值并说明。"""
    # 一条 compression_ratio 高到离谱的真语音：任何候选都会把它当重复输出丢掉
    rows = [_row("真语音", 0.05, -0.2, 99.0)]

    rec = recommend(rows)

    assert rec.chosen == UPSTREAM_BASELINE
    assert rec.chosen_confusion.false_drop == 1, "误杀没被消除，就不该声称消除了"
    assert "误杀" in rec.note


def test_dropped_rows_lists_what_the_filter_would_throw_away():
    """报告里要逐条列出被丢弃的文本，这份清单就是它的来源。"""
    rows = [_row("留着的", 0.05, -0.2, 1.2), _row("要丢的", 0.95, -1.8, 1.2)]

    assert [r.text for r in dropped_rows(rows, UPSTREAM_BASELINE)] == ["要丢的"]


def test_row_label_defaults_to_speech_when_absent():
    """操作者没动过的行就是「人说话」—— 沉默不是一种标注。"""
    row = Row.from_dict({"index": 1, "start": 0.0, "end": 1.0, "text": "x",
                         "no_speech_prob": 0.1, "avg_logprob": -0.2,
                         "compression_ratio": 1.2})
    assert row.label == SPEECH


def test_rows_round_trip_through_a_file(tmp_path):
    meta = {"audio": "sample/中文音频.mp3", "language": "zh",
            "initial_prompt": None, "window_s": None}
    rows = [_row("一句", 0.1, -0.2, 1.2), _row("另一句", 0.2, -0.3, 1.3, NONSPEECH)]
    p = tmp_path / "zh.rows.json"

    save_rows(p, meta, rows)
    got_meta, got_rows = load_rows(p)

    assert got_meta == meta
    assert got_rows == rows


def test_synthetic_clip_hits_the_requested_level():
    """C42：合成噪声只用于 gate 对照，但既然要生成，电平就得说得出准头。"""
    clip = synthetic_clip(seconds=2.0, dbfs=-25.0, seed=1)

    assert clip.shape == (32000,)
    assert clip.dtype == np.float32
    rms = float(np.sqrt(np.mean(clip ** 2)))
    assert abs(20 * np.log10(rms) - (-25.0)) < 0.1


def test_synthetic_clip_is_reproducible():
    a = synthetic_clip(seconds=1.0, dbfs=-30.0, seed=7)
    b = synthetic_clip(seconds=1.0, dbfs=-30.0, seed=7)
    assert np.array_equal(a, b)


def test_digital_silence_is_all_zeros():
    assert not digital_silence(1.0).any()
    assert digital_silence(1.0).shape == (16000,)


def test_traditional_ratio_counts_segments_not_characters():
    """C46 要的是「有多少条输出夹了繁体」，不是字符占比。"""
    ratio, hits = traditional_ratio(["这是简体", "這是繁體", "简体裡混了這個"])

    assert ratio == pytest.approx(2 / 3)
    assert len(hits) == 2
    assert "這是繁體" in hits


def test_traditional_markers_are_absent_from_simplified_text():
    """标记字必须真的是繁体专有 —— 混进简体也有的字会凭空报残留。

    刻意点名三个踩过的字：幫助 / 確定 / 檢查 里的「助」「定」「查」两边同形。
    真把它们算作标记，一份正常的简体转写会被判成繁体残留，而 C46 的判定
    （要不要补确定性转换）就靠这个数字。
    """
    plain = ("这是普通话的句子，请用简体中文转写，我们学习数据网络线路电脑错误"
             "帮助我们确定并检查一下")

    assert traditional_ratio([plain]) == (0.0, [])


def test_traditional_ratio_of_nothing_is_zero():
    assert traditional_ratio([]) == (0.0, [])
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `uv run pytest tests/test_calibrate.py -v`
Expected: FAIL —`ModuleNotFoundError: No module named 'chrometrans.calibrate'`

- [ ] **Step 3: 实现**

新建 `src/chrometrans/calibrate.py`：

```python
"""幻觉阈值标定工具（开发者工具）。

三块，只有第 3 块需要 GPU 与样本文件：
  1. 素材构造 —— 纯函数
  2. 统计与推荐 —— 纯函数
  3. 跑 Whisper —— 重，不进 CI

**这个脚本不写 config.py**：数字由人贴进去、填上 `calibrated_on`、提交（C40）。
阈值是契约，而契约要有人的签名。

用法见 README「标定」一节；真实运行报告在 docs/superpowers/calibration/。
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from chrometrans.asr.whisper_engine import is_hallucination, transcribe_kwargs
from chrometrans.audio.segmenter import Segment, Segmenter, normalize
from chrometrans.config import AsrConfig, SegmenterConfig

SPEECH = "speech"
NONSPEECH = "nonspeech"
SAMPLE_RATE = 16000


# ---- 第 1 块：素材构造 ----

def synthetic_clip(seconds: float, dbfs: float, seed: int = 0,
                   sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """定长定电平的白噪声。**只用于验证切句闸门（C42）**。

    C42：合成的平坦噪声不得作为负样本 —— 切句器根本不放它过去（spec §6.1 实测
    白噪声 -25 dBFS 也是 0 段），拿它标定等于给一条不存在的路径调参数。
    所以它唯一的用途是 `gate` 子命令里那组对照：证明闸门确实拦得住。
    """
    n = int(round(seconds * sample_rate))
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n).astype(np.float32)
    rms = float(np.sqrt(np.mean(noise ** 2)))
    target = 10.0 ** (dbfs / 20.0)
    return (noise * (target / rms)).astype(np.float32)


def digital_silence(seconds: float,
                    sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    return np.zeros(int(round(seconds * sample_rate)), dtype=np.float32)


# ---- 第 2 块：统计与推荐 ----

@dataclass(frozen=True)
class Thresholds:
    """一组幻觉过滤阈值。"""
    no_speech_prob: float
    avg_logprob: float
    compression_ratio: float

    def as_asr_config(self) -> AsrConfig:
        """变成生产用的 AsrConfig。

        标定与线上走**同一个** is_hallucination、同一个 AsrConfig —— 判据函数
        自己实现一遍是最容易让标定结果落空的做法。
        """
        return AsrConfig(
            no_speech_prob_threshold=self.no_speech_prob,
            avg_logprob_threshold=self.avg_logprob,
            compression_ratio_threshold=self.compression_ratio)


# faster-whisper 参考实现的默认值（transcribe.py:274-276），也是 en profile 的值。
# test_calibrate.py 里有一条测试把它和 LANGUAGES["en"] 锁在一起。
UPSTREAM_BASELINE = Thresholds(no_speech_prob=0.6, avg_logprob=-1.0,
                               compression_ratio=2.4)

# 候选网格。刻意围着基准取密、往外取疏：真要动阈值，动一小步就够。
GRID = {
    "no_speech_prob": (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9),
    "avg_logprob": (-1.6, -1.4, -1.2, -1.0, -0.8, -0.6),
    "compression_ratio": (2.0, 2.4, 2.8, 3.2),
}


@dataclass(frozen=True)
class Row:
    """一段 Whisper 输出及其统计量，外加一条判定标注。

    label 默认 `speech`：操作者没动过的行就是「人说话」。沉默不是一种标注。
    """
    index: int
    start: float
    end: float
    text: str
    no_speech_prob: float
    avg_logprob: float
    compression_ratio: float
    label: str = SPEECH

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Row":
        return cls(**{**d, "label": d.get("label", SPEECH)})


@dataclass(frozen=True)
class Confusion:
    true_speech: int      # 标注为语音，未被丢
    false_drop: int       # 标注为语音，被丢     ← C41 硬要求为 0
    false_pass: int       # 标注为非语音，未被丢
    true_nonspeech: int   # 标注为非语音，被丢

    @property
    def positives(self) -> int:
        return self.true_speech + self.false_drop

    @property
    def negatives(self) -> int:
        return self.false_pass + self.true_nonspeech

    @property
    def false_drop_rate(self) -> float:
        return self.false_drop / self.positives if self.positives else 0.0

    @property
    def false_pass_rate(self) -> float | None:
        """没有负样本时是 None 而不是 0.0 —— 「测不出来」和「测出来是零」不同。"""
        return self.false_pass / self.negatives if self.negatives else None


def confusion(rows: list[Row], thresholds: Thresholds) -> Confusion:
    cfg = thresholds.as_asr_config()
    counts = {"true_speech": 0, "false_drop": 0,
              "false_pass": 0, "true_nonspeech": 0}
    for row in rows:
        drop = is_hallucination(row.no_speech_prob, row.avg_logprob,
                                row.compression_ratio, cfg)
        if row.label == SPEECH:
            counts["false_drop" if drop else "true_speech"] += 1
        else:
            counts["true_nonspeech" if drop else "false_pass"] += 1
    return Confusion(**counts)


def dropped_rows(rows: list[Row], thresholds: Thresholds) -> list[Row]:
    cfg = thresholds.as_asr_config()
    return [r for r in rows
            if is_hallucination(r.no_speech_prob, r.avg_logprob,
                                r.compression_ratio, cfg)]


@dataclass(frozen=True)
class Recommendation:
    baseline: Thresholds
    chosen: Thresholds
    baseline_confusion: Confusion
    chosen_confusion: Confusion
    note: str


def _distance(a: Thresholds, b: Thresholds) -> float:
    """平手时的偏向：离基准越近越省事，也越不容易过拟合到这几十分钟素材上。"""
    return (abs(a.no_speech_prob - b.no_speech_prob)
            + abs(a.avg_logprob - b.avg_logprob)
            + abs(a.compression_ratio - b.compression_ratio))


def recommend(rows: list[Row],
              baseline: Thresholds = UPSTREAM_BASELINE) -> Recommendation:
    """在「误杀 = 0」的候选里挑漏放最少的一个（C41）。

    没有负样本、且基准没有误杀时，漏放无法度量，此时**保持基准不动**并说明原因
    —— 凭正样本单侧收紧或放松，都是在看不见的那一侧下注。但基准一旦误杀
    （C41 硬要求为 0），就必须在网格里找能救回来的候选，哪怕没有负样本：
    「软指标测不出来」不能拿来豁免唯一那条硬指标。
    """
    base = confusion(rows, baseline)
    if base.negatives == 0 and base.false_drop == 0:
        return Recommendation(
            baseline, baseline, base, base,
            "负样本为空：漏放无法度量，保持原值（C41）。"
            "要让这一步有意义，需要 silero 会放行的非语音素材（掌声 / 笑声 / "
            "带人声的音乐 / 交叠说话）—— 合成噪声不算（C42）。")

    survivors = []
    for nsp in GRID["no_speech_prob"]:
        for alp in GRID["avg_logprob"]:
            for cr in GRID["compression_ratio"]:
                candidate = Thresholds(nsp, alp, cr)
                c = confusion(rows, candidate)
                if c.false_drop == 0:
                    survivors.append((c.false_pass, _distance(candidate, baseline),
                                      candidate, c))
    if not survivors:
        return Recommendation(
            baseline, baseline, base, base,
            f"候选网格里没有一组能做到误杀 = 0（基准误杀 {base.false_drop} 段），"
            f"保持原值。这说明样本里有「无论如何都会被当成幻觉」的真语音，"
            f"该查的是那条语音本身，不是阈值。")

    survivors.sort(key=lambda item: (item[0], item[1]))
    _, _, chosen, chosen_c = survivors[0]
    return Recommendation(
        baseline, chosen, base, chosen_c,
        "在误杀 = 0 的候选里漏放最少；平手时取离基准最近的。")


# ---- C46：繁体残留 ----

# 繁体专有字（简体写法不同）。**只收两边写法确实不同的字** —— 混进简体也有的
# 字会凭空报出残留率，而 C46 的判定就靠这个数字。已经踩过三个：
# 幫助/確定/檢查 里的「助」「定」「查」两边同形，把它们当标记会让一份完全正常的
# 简体转写被判成 100% 繁体残留。
TRADITIONAL_MARKERS = frozenset(
    "這個說嗎為對時會來們實後點語識別問題進還樣轉發現過從東華標準備體學習"
    "聽聲類數據網絡線電腦錯誤麼幾應該讓覺開關產業務給經濟觀點紀錄記憶選"
    "擇適內長門車見認話讀寫課練習幫確檢環節圖書館邊際離開週雲與將專業員"
    "導師鄉親愛歡樂歲頭條風飛馬鳥魚龍龜"
)


def traditional_ratio(texts: list[str]) -> tuple[float, list[str]]:
    """夹了繁体专有字的条目占比，以及这些条目本身。

    C46 要的是「有多少条输出夹了繁体」，不是字符占比 —— 一条里出现一个繁体字
    和十个繁体字，对「要不要补确定性转换」是同一个答案。
    """
    hits = [t for t in texts if TRADITIONAL_MARKERS & set(t)]
    return (len(hits) / len(texts) if texts else 0.0, hits)


# ---- 记录的存取 ----

def save_rows(path: Path, meta: dict, rows: list[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"meta": meta, "rows": [r.to_dict() for r in rows]},
        ensure_ascii=False, indent=2), encoding="utf-8")


def load_rows(path: Path) -> tuple[dict, list[Row]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return raw.get("meta", {}), [Row.from_dict(d) for d in raw["rows"]]
```

顶部这几个 import 都是模块级的，**不能写成函数内 import**：`test_calibrate.py` 里那条「必须走生产切句器」的用例靠 `monkeypatch.setattr(cal, "Segmenter", ...)` 换掉切句器，函数内 import 的名字在模块命名空间里不存在，补丁打不上去。silero 与 faster-whisper 仍是延迟 import（在 `Segmenter.__init__` 和第 3 块里），所以测试收集时不会拖进 torch 与 CUDA。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_calibrate.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/chrometrans/calibrate.py tests/test_calibrate.py
git commit -m "feat(calibrate): 标定的统计与推荐（纯函数部分）"
```

---

### Task 4: 跑真实标定，产出 ru / zh 的 profile 与报告

这是规格 §8 说的「第 3 块的替代物是真实运行」——报告就是它的证据，`calibrated_on` 指向它。

**前置：** 本机有 CUDA，`sample/` 下五个文件在位（该目录 git-ignored，见 `.gitignore:26-28`）。

**Files:**
- Modify: `src/chrometrans/calibrate.py`（第 3 块 + CLI）
- Modify: `src/chrometrans/config.py`（加入 ru / zh 两个 profile）
- Modify: `pyproject.toml`（console script）
- Create: `docs/superpowers/calibration/2026-09-24-ru.md`、`-zh.md`
- Test: `tests/test_calibrate.py`、`tests/test_config.py`

**Interfaces:**
- Consumes: `Thresholds` / `Row` / `confusion` / `recommend` / `dropped_rows` / `traditional_ratio` / `save_rows` / `load_rows`（Task 3）、`transcribe_kwargs`（Task 2）、`LanguageProfile` / `LANGUAGES`（Task 1）
- Produces:
  - `segments_for(audio, window_s=None, cfg=None) -> list[Segment]`
  - `record(audio_path, language, initial_prompt=None, window_s=None, model_name=..., device=..., compute_type=...) -> list[Row]`
  - `gate_report(clips) -> list[GateRow]`
  - console script `chrometrans-calibrate`（子命令 `record` / `recommend` / `gate`，`--out` 一律必填）
  - `LANGUAGES["ru"]`、`LANGUAGES["zh"]`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_calibrate.py`：

```python
def test_segments_for_slices_fixed_windows_when_asked(monkeypatch):
    """链路 2：绕过切句器直接切固定长度窗口。

    这是有意的一条「不存在的路径」——它把「Whisper 在这些音频上会输出什么」
    与「切句器放不放它过去」分开，混在一起时看不出是哪一环出的问题。
    """
    from chrometrans.calibrate import segments_for

    audio = np.zeros(16000 * 25, dtype=np.float32)
    segs = segments_for(audio, window_s=10.0)

    assert [round(s.start, 3) for s in segs] == [0.0, 10.0, 20.0]
    assert all(s.audio.size <= 16000 * 10 for s in segs)


def test_segments_for_goes_through_the_real_segmenter(monkeypatch):
    """不给 window_s 时必须走生产切句器 —— 标定的就是线上那件事。"""
    import chrometrans.calibrate as cal
    from chrometrans.audio.segmenter import Segment

    called = {}

    class FakeSegmenter:
        def __init__(self, cfg=None):
            called["built"] = True

        def feed(self, chunk):
            return [Segment(1, 0.0, 1.0, np.zeros(16000, dtype=np.float32))]

        def flush(self):
            return []

    monkeypatch.setattr(cal, "Segmenter", FakeSegmenter)
    segs = cal.segments_for(np.zeros(16000, dtype=np.float32))

    assert called["built"]
    assert len(segs) == 1


def test_cli_requires_out_everywhere():
    """中文走 stdout 在 Windows 的 Bash 工具里是乱码。要人看的结果一律落文件。"""
    from chrometrans.calibrate import parse_args

    with pytest.raises(SystemExit):
        parse_args(["record", "--audio", "a.mp3", "--language", "zh"])
    with pytest.raises(SystemExit):
        parse_args(["recommend", "--records", "r.json"])


def test_rows_skip_blank_segments_so_the_matrix_matches_production():
    """C41：空白段不得进矩阵 —— 误差方向恰好是最不能错的那一侧。

    生产里空白段两个方向都不可见（幻觉的因 `if text:` 不进 dropped，非幻觉的
    直接 continue）。若这里给它建行，「非幻觉但空白」会被 confusion 记成
    `true_speech`，而生产其实什么都没出 —— 标定会把静默丢掉的那段算成「留住了」，
    让**误杀看起来更少**。C41 只认这一条硬判据，所以口径必须与生产一致。
    """
    from chrometrans.calibrate import rows_from

    class FakeSeg:
        def __init__(self, text, nsp=0.05, alp=-0.2, cr=1.2):
            self.start, self.end = 0.0, 1.0
            self.text = text
            self.no_speech_prob, self.avg_logprob = nsp, alp
            self.compression_ratio = cr

    segment = Segment(3, 10.0, 14.0, np.zeros(16000, dtype=np.float32))
    rows = rows_from(segment, [FakeSeg("人说话"),
                               FakeSeg("   "),
                               FakeSeg("", nsp=0.95, alp=-1.8)])

    assert [r.text for r in rows] == ["人说话"]
    assert rows[0].index == 3, "index 是切句器段落的，不是 Whisper 的内部序号"
    assert (rows[0].start, rows[0].end) == (10.0, 11.0), "时间是绝对时间"
```

（`Segment` 若未在文件头导入，在这个测试里就地 `from chrometrans.audio.segmenter
import Segment` —— 与上一条测试同一写法。）

- [ ] **Step 2: 跑测试确认它失败**

Run: `uv run pytest tests/test_calibrate.py -v`
Expected: FAIL —`ImportError: cannot import name 'segments_for'`

- [ ] **Step 3: 实现第 3 块与 CLI**

追加到 `src/chrometrans/calibrate.py`：

```python
# ---- 第 3 块：跑 Whisper（需要 GPU 与样本文件） ----

# 喂给切句器的块大小。10ms 量级与生产管道一致，别一次灌整段 ——
# 切句器的内存回收是按块推进的。
FEED_BLOCK = 16000


def segments_for(audio: np.ndarray, window_s: float | None = None,
                 cfg=None) -> list[Segment]:
    """把整段音频切成待识别的段。

    window_s 给定时**绕过切句器**直接硬切固定长度窗口 —— 那是标定的链路 2，
    用来把「Whisper 输出什么」与「切句器放不放它过去」分开观察。
    """
    cfg = cfg or SegmenterConfig()
    rate = cfg.sample_rate

    if window_s is not None:
        step = int(round(window_s * rate))
        total_s = len(audio) / rate
        return [Segment(index=i + 1, start=i * window_s,
                        end=min((i + 1) * window_s, total_s),
                        audio=normalize(audio[i * step:(i + 1) * step],
                                        cfg.norm_target_rms, cfg.norm_max_gain,
                                        cfg.norm_floor_rms))
                for i in range(math.ceil(len(audio) / step))]

    segmenter = Segmenter(cfg)
    out: list[Segment] = []
    for i in range(0, len(audio), FEED_BLOCK):
        out += segmenter.feed(audio[i:i + FEED_BLOCK])
    out += segmenter.flush()
    return out


def rows_from(segment: Segment, whisper_segments) -> list[Row]:
    """一个切句器段落的 Whisper 输出 → `Row` 列表。**空白段一律不进。**

    生产里空白段两个方向都不可见（`whisper_engine.py`：幻觉的因 `if text:` 不进
    `dropped`，非幻觉的直接 `continue`）。这里若不跳过，「非幻觉但空白」会被
    `confusion` 记成 `true_speech`，而生产其实什么都没出 —— 标定会把静默丢掉的
    那段算成「留住了」，误差方向是让**误杀看起来更少**。C41 只认这条硬判据，
    所以口径必须与生产一致。

    单独成函数是为了能在不加载模型的前提下把这条口径钉在测试里。
    """
    rows: list[Row] = []
    for seg in whisper_segments:
        text = seg.text.strip()
        if not text:
            continue
        rows.append(Row(
            index=segment.index,
            start=segment.start + seg.start,
            end=segment.start + seg.end,
            text=text,
            no_speech_prob=seg.no_speech_prob,
            avg_logprob=seg.avg_logprob,
            compression_ratio=seg.compression_ratio))
    return rows


def record(audio_path: Path, language: str,
           initial_prompt: str | None = None,
           window_s: float | None = None,
           model_name: str = "large-v3-turbo", device: str = "cuda",
           compute_type: str = "int8_float16") -> list[Row]:
    """跑真实切句器 + Whisper，逐段记录三个统计量与文本。

    解码参数来自 `transcribe_kwargs`，与生产完全一致（spec §5.5）——
    参数不一致时，标出来的是给另一个配置调的阈值。
    不能用 `WhisperEngine.transcribe`：它会把幻觉段就地过滤掉，而这里要的正是
    那些段本身。
    """
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio

    from chrometrans.asr.whisper_engine import prepare_cuda_paths

    prepare_cuda_paths()
    audio = decode_audio(str(audio_path), sampling_rate=SAMPLE_RATE)
    asr = AsrConfig(language=language, initial_prompt=initial_prompt)
    model = WhisperModel(model_name, device=device, compute_type=compute_type)

    rows: list[Row] = []
    for segment in segments_for(audio, window_s):
        whisper_segments, _info = model.transcribe(segment.audio,
                                                   **transcribe_kwargs(asr))
        rows.extend(rows_from(segment, whisper_segments))
    return rows


@dataclass(frozen=True)
class GateRow:
    """一道闸门对一种素材的放行情况。"""
    label: str
    seconds: float
    segments: int
    speech_seconds: float


def gate_report(clips: list[tuple[str, np.ndarray]]) -> list[GateRow]:
    """对照：合成噪声 / 数字静音能不能过切句器（C42 的现场证据）。

    这一段存在的意义是让 spec §6.1 那条实测结论**可复跑**：闸门失效时，
    它会在报告里直接显形，而不是等谁想起来再写一个探针。
    """
    rows = []
    for label, audio in clips:
        segs = segments_for(audio)
        rows.append(GateRow(
            label=label, seconds=len(audio) / SAMPLE_RATE,
            segments=len(segs),
            speech_seconds=sum(s.end - s.start for s in segs)))
    return rows


# ---- CLI ----

def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="chrometrans-calibrate",
        description="幻觉过滤阈值标定（开发者工具，需要 GPU）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="跑切句器 + Whisper，逐段记录统计量")
    r.add_argument("--audio", type=Path, required=True)
    r.add_argument("--language", required=True)
    r.add_argument("--initial-prompt", default=None)
    r.add_argument("--window-s", type=float, default=None,
                   help="给定则绕过切句器，直接切固定长度窗口（链路 2）")
    r.add_argument("--model", default="large-v3-turbo")
    r.add_argument("--device", default="cuda")
    r.add_argument("--compute-type", default="int8_float16")
    r.add_argument("--out", type=Path, required=True)

    c = sub.add_parser("recommend", help="由带标注的记录算混淆矩阵与推荐阈值")
    c.add_argument("--records", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)

    g = sub.add_parser("gate", help="对照：合成噪声 / 真实底噪能否过切句器（C42）")
    g.add_argument("--audio", type=Path, action="append", default=[])
    g.add_argument("--seconds", type=float, default=20.0)
    g.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)
```

`--out` 一律 `required=True`：中文输出在 Windows 的 Bash 工具里会变乱码，凡是要人看的结果都落文件再读。

- [ ] **Step 4: 实现 `main` 与三个子命令的渲染**

继续追加：

```python
def _fmt_thresholds(t: Thresholds) -> str:
    return (f"no_speech_prob={t.no_speech_prob} "
            f"avg_logprob={t.avg_logprob} "
            f"compression_ratio={t.compression_ratio}")


def _render_recommendation(meta: dict, rows: list[Row],
                           rec: Recommendation) -> str:
    """把推荐写成一份可直接提交的标定报告。

    报告是 `calibrated_on` 指向的东西（C40），所以它必须自带材料标识与推导
    过程 —— 只留一个数字，半年后没人说得清那是怎么来的。
    """
    speech = sum(1 for r in rows if r.label == SPEECH)
    ratio, trad_hits = traditional_ratio([r.text for r in rows])
    # 只分两支，且**照抄 rec.note**：早先这里还有一支写死文案的分支，条件是
    # `rec.baseline_confusion.false_drop and rec.chosen == rec.baseline`，
    # 而文字说的是「误杀已为 0」—— 条件（误杀 > 0）与文字正好相反。它自称要覆盖的
    # 情形（误杀 = 0、无负样本）够不到那里，真进得来的情形（网格救不回来、退回基准）
    # 反倒被灌了一句「误杀已为 0」，把没消除的误杀说成消除了，正是 C41 要拦的。
    # 结论照实由 recommend 写进 note，这里不转述。
    if rec.chosen == rec.baseline:
        verdict = "**照抄基准值**：" + rec.note
    else:
        verdict = f"**采用推荐值**：{rec.note}"

    lines = [
        f"# {meta.get('language')} 幻觉阈值标定报告",
        "",
        f"- 日期：{meta.get('date', '')}",
        f"- 素材：`{meta.get('audio')}`"
        + (f"（固定 {meta['window_s']}s 窗口，绕过切句器）" if meta.get("window_s")
           else "（走生产切句器）"),
        f"- 模型：`{meta.get('model')}` · language=`{meta.get('language')}`"
        f" · initial_prompt=`{meta.get('initial_prompt')}`",
        f"- 正样本：{speech} 条 / 负样本：{len(rows) - speech} 条",
        "",
        "## 混淆矩阵",
        "",
        "| 阈值 | 真语音留下 | **误杀** | 漏放 | 非语音拦下 | 误杀率 | 漏放率 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, t, c in (("基准", rec.baseline, rec.baseline_confusion),
                       ("推荐", rec.chosen, rec.chosen_confusion)):
        rate = "测不出来" if c.false_pass_rate is None else f"{c.false_pass_rate:.1%}"
        lines.append(f"| {name} `{_fmt_thresholds(t)}` | {c.true_speech} | "
                     f"{c.false_drop} | {c.false_pass} | {c.true_nonspeech} | "
                     f"{c.false_drop_rate:.1%} | {rate} |")

    lines += ["", "## 结论", "", verdict, "", "## 被丢弃的条目", ""]
    dropped = dropped_rows(rows, rec.chosen)
    if not dropped:
        lines.append("（无）")
    for r in dropped:
        lines.append(f"- `[{r.start:.1f}s]` nsp={r.no_speech_prob:.3f} "
                     f"alp={r.avg_logprob:.3f} cr={r.compression_ratio:.3f} "
                     f"label={r.label}：{r.text}")

    lines += ["", "## 繁体残留率（C46）", "",
              f"{ratio:.1%}（{len(trad_hits)} / {len(rows)} 条）"]
    if ratio > 0:
        lines += ["", "非零。按 C46 必须补确定性的繁→简转换，不得依赖网络翻译。", ""]
        lines += [f"- {t}" for t in trad_hits[:20]]
    else:
        lines += ["", "为零，无需补转换（C46 只在非零时才要求）。"]
    return "\n".join(lines) + "\n"


def _render_gate(rows: list[GateRow]) -> str:
    lines = ["| 素材 | 输入时长 | 切句器放行段数 | 放行语音时长 |",
             "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r.label} | {r.seconds:.1f}s | {r.segments} | "
                     f"{r.speech_seconds:.1f}s |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.cmd == "record":
        rows = record(args.audio, args.language,
                      initial_prompt=args.initial_prompt,
                      window_s=args.window_s, model_name=args.model,
                      device=args.device, compute_type=args.compute_type)
        save_rows(args.out, {
            "audio": str(args.audio), "language": args.language,
            "initial_prompt": args.initial_prompt, "window_s": args.window_s,
            "model": args.model,
            "date": datetime.now().strftime("%Y-%m-%d"),
        }, rows)
        print(f"写出 {len(rows)} 条记录 → {args.out}")
        return 0

    if args.cmd == "recommend":
        meta, rows = load_rows(args.records)
        rec = recommend(rows)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(_render_recommendation(meta, rows, rec),
                            encoding="utf-8")
        print(f"报告已写出 → {args.out}")
        return 0

    clips = [("数字静音", digital_silence(args.seconds)),
             ("白噪声 -25 dBFS", synthetic_clip(args.seconds, -25.0, seed=1)),
             ("白噪声 -35 dBFS", synthetic_clip(args.seconds, -35.0, seed=2))]
    clips += [(f"真实素材 {p.name}", decode_for_gate(p)) for p in args.audio]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(_render_gate(gate_report(clips)), encoding="utf-8")
    print(f"对照表已写出 → {args.out}")
    return 0


def decode_for_gate(path: Path) -> np.ndarray:
    from faster_whisper.audio import decode_audio

    return decode_audio(str(path), sampling_rate=SAMPLE_RATE)


if __name__ == "__main__":
    raise SystemExit(main())
```

顶部 import 补上 `import argparse` 与 `from datetime import datetime`。

- [ ] **Step 5: 加 console script**

`pyproject.toml` 的 `[project.scripts]`：

```toml
chrometrans-calibrate = "chrometrans.calibrate:main"
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_calibrate.py -v`
Expected: PASS

- [ ] **Step 7: 跑 `gate`，把规格 §6.1 的结论重新坐实一遍**

```bash
uv run chrometrans-calibrate gate --audio "sample/底噪.MP3" \
  --out .superpowers/calibration/gate.md
```

然后 Read `.superpowers/calibration/gate.md`。

Expected：数字静音 / 白噪声 -25 / 白噪声 -35 三行的放行段数都是 **0**；`底噪.MP3` 也是 **0**。任何一行不为 0 就停下——C42 与 §6.1 的前提不成立，标定方案要重来。

- [ ] **Step 8: 跑 ru 的 `record`**

```bash
uv run chrometrans-calibrate record --audio "sample/俄语音频.MP3" \
  --language ru --out .superpowers/calibration/ru.rows.json
```

Read `.superpowers/calibration/ru.rows.json`。段数应与规格 §6.1 那张表一致——**不一致就先查清原因再往下走**，别拿一份对不上的记录去标定。差得远（比如个位数）多半是模型没在 GPU 上跑完整段。

- [ ] **Step 9: 用俄语字幕给 ru 的条目打标注**

`sample/俄语字幕(不全.txt` 只覆盖 0–210s 的说话部分（spec §6.2）；220s 之后是音乐，silero 概率恰好 0.000，本来就进不了段。所以逐条比对：**字幕里找得到的 → 保持 `speech`**；**字幕里没有、且读起来是重复或成句但不着边际的 → 改成 `nonspeech`**。

用 Edit 直接改 `ru.rows.json` 里对应行的 `"label"`。

> 这一轮不要为了填满负样本而硬凑（C42 / C41）。一条都标不出来是完全正常的产出，`recommend` 会照实说「漏放无法度量」并保持原值——**前提是基准没有误杀**。若基准在这次标注里误杀了真语音，它会先放宽（C41 的硬判据优先），报告里会用「误杀」那句说明。两种都是照实说，别把后者当成 bug。

- [ ] **Step 10: 出 ru 的报告**

```bash
uv run chrometrans-calibrate recommend \
  --records .superpowers/calibration/ru.rows.json \
  --out docs/superpowers/calibration/2026-09-24-ru.md
```

Read 生成的报告，核对：误杀率、被丢弃条目逐条有文本、结论段是否照实。

- [ ] **Step 11: 跑 zh 的 `record`**

```bash
uv run chrometrans-calibrate record --audio "sample/中文音频.mp3" \
  --language zh --initial-prompt "以下是普通话的句子，请用简体中文转写。" \
  --out .superpowers/calibration/zh.rows.json
```

段数与时长应与规格 §6.1 的记录一致（中文素材 496.4s，走生产切句器）。对不上先查原因。

`--initial-prompt` 必须在这里给出，且**与后面写进 zh profile 的完全一致** —— 提示词参与解码，不一致时标的是另一个配置。这条约束是 C46 的直接后果。

- [ ] **Step 12: 用中文字幕给 zh 的条目打标注并出报告**

同 Step 9 / 10，素材换成 `sample/中文字幕.txt`，输出 `docs/superpowers/calibration/2026-09-24-zh.md`。

- [ ] **Step 13: 把两份报告的数字写进 `LANGUAGES`**

在 `src/chrometrans/config.py` 的 `LANGUAGES` 里加两项。**阈值取两份报告「结论」段采用的数字**；`calibrated_on` 指向报告文件——这是 C40 的全部要求，也是「有人为这行负责」的落地方式：

```python
    "ru": LanguageProfile(
        code="ru",
        label="俄语",
        asr_language="ru",
        translate_src="ru",
        initial_prompt=None,
        no_speech_prob_threshold=<报告里采用的 no_speech_prob>,
        avg_logprob_threshold=<报告里采用的 avg_logprob>,
        compression_ratio_threshold=<报告里采用的 compression_ratio>,
        calibrated_on="docs/superpowers/calibration/2026-09-24-ru.md"
                      "（sample/俄语音频.MP3，38 段正样本）",
    ),
    "zh": LanguageProfile(
        code="zh",
        label="中文",
        asr_language="zh",
        # None = 中文走单语模式，本次会话不翻译（spec §5.3 / C44）
        translate_src=None,
        initial_prompt="以下是普通话的句子，请用简体中文转写。",
        no_speech_prob_threshold=<报告里采用的 no_speech_prob>,
        avg_logprob_threshold=<报告里采用的 avg_logprob>,
        compression_ratio_threshold=<报告里采用的 compression_ratio>,
        calibrated_on="docs/superpowers/calibration/2026-09-24-zh.md"
                      "（sample/中文音频.mp3，59 段正样本）",
    ),
```

段数与素材名按实际报告填。若某门语言的 `recommend` 输出是「照抄基准值」，那三个数字就照抄 `en` 的——**并在 `calibrated_on` 里写明「实测后沿用上游默认值」**，这是 C40 明文承认的合法产出。

上面三处 `<...>` 是**故意写成不能通过 Python 解析的**：照抄过去会直接 SyntaxError，所以不可能忘了换。

- [ ] **Step 14: 加 zh 单语模式的锁**

追加到 `tests/test_config.py`：

```python
def test_chinese_is_monolingual():
    """C43 / C44：中文不翻译，且这件事是从 profile 派生的，不是散在各处的 if。"""
    cfg = load_config("zh")

    assert cfg.translate.src is None
    assert cfg.asr.language == "zh"
    assert cfg.asr.initial_prompt, "C46：靠提示词定向简体"


def test_russian_translates_from_russian():
    cfg = load_config("ru")

    assert cfg.asr.language == "ru"
    assert cfg.translate.src == "ru", "C39：两侧必须同步，否则是英语模型听俄语"
```

> **不要删 `test_invariant_holds_for_a_non_english_profile`。** Task 1 落地时补的这条
> （见 `tests/test_config.py:71`）用一份合成 `"zz"` profile 锁住「两个语言字段必须
> 分叉」这件事。加入 ru/zh 之后，Task 1 那条按 profile 迭代的测试**才第一次真正生效**
> —— 在那之前它是空洞的（en 的每个字段值恰好等于两个 config 的默认值，硬编码
> `language="en"` 也能全绿，实测过）。合成那条仍然不可替代：ru 上 `asr_language` 与
> `translate_src` 碰巧都是 `"ru"`，只有 zz 的 `"zz"` / `"zz-Hans"` 把分叉这件事钉死。

- [ ] **Step 15: 跑全量测试**

Run: `uv run pytest -q`
Expected: 全绿。（Task 2 已经把 `tests/test_engine.py` 的 stub 一并改到
`TranscribeResult`，不再有「要等 Task 5」的例外。）

- [ ] **Step 16: 提交**

```bash
git add src/chrometrans/calibrate.py src/chrometrans/config.py pyproject.toml \
        docs/superpowers/calibration/ tests/test_calibrate.py tests/test_config.py
git commit -m "feat(calibrate): 标定工具与 ru/zh 实测标定报告

按 C40：阈值与 calibrated_on 一并提交，报告是它的依据。"
```

> **C46 的分支**：Read zh 报告。繁体残留率为 **0** → 本任务到此为止。**非零** → 再做下面两步，然后重新提交：
>
> 1. `uv add zhconv`（纯 Python、离线、确定性——C46 禁止用网络翻译做这一步）
> 2. `WhisperEngine.transcribe` 里拼 `text` 的那一行改为 `zhconv.convert(text, "zh-cn")`，并在 `tests/test_whisper_engine.py` 加一条 `test_chinese_output_is_converted_to_simplified`：喂一条含 `"這是繁體"` 的假段，断言 `result.utterance.text == "这是繁体"`。

---

### Task 5: `engine` 发出丢弃事件并落 `dropped.jsonl`

**Files:**
- Modify: `src/chrometrans/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `TranscribeResult` / `Dropped`（Task 2）
- Produces:
  - 事件 `{"event": "status", "data": {"state": "dropped", "message": str, "text": str, "no_speech_prob": float, "avg_logprob": float, "compression_ratio": float}}`
  - 会话目录下的 `dropped.jsonl`（有丢弃时才产生）
  - `Engine._write_view(session, filename, body)`（`_render_one` 的一半被拆出来，供 drops 复用）

- [ ] **Step 1: 写失败的测试**

`tests/test_engine.py` 追加：

> `StubAsr` / `HalfBrokenAsr` / `RecordingAsr` 三个 stub **已经在 Task 2 里改好**了
> （接口变更任务拥有它的消费者），这里不用再动。若你看到它们还返回裸 `Utterance`，
> 说明 Task 2 没落地，停下来报告，不要在这里补。

```python
def test_dropped_segments_are_reported_with_their_text(tmp_path):
    """C45：丢弃必须可见，且要带着文本。

    只说「丢了 1 段」用户判断不了那是垃圾还是真话 —— 而判断正是这个事件的全部
    目的。
    """
    from chrometrans.asr.whisper_engine import Dropped, TranscribeResult, Utterance

    events = []

    class DroppingAsr:
        def load(self): pass

        def transcribe(self, seg):
            return TranscribeResult(
                utterance=Utterance(start=seg.start, end=seg.end, text="留下的话"),
                dropped=(Dropped(text="编出来的话", no_speech_prob=0.95,
                                 avg_logprob=-1.8, compression_ratio=1.1),))

    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=events.append, asr=DroppingAsr(),
                 translator=StubTranslator(),
                 segments=[Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))])
    eng.run()

    drops = [e["data"] for e in events
             if e["event"] == "status" and e["data"].get("state") == "dropped"]
    assert len(drops) == 1
    assert "编出来的话" in drops[0]["text"]
    assert drops[0]["compression_ratio"] == 1.1, "三个统计量要一并带上"
    assert [e["data"]["source"] for e in events if e["event"] == "cue"] == ["留下的话"]


def test_dropped_segments_land_in_dropped_jsonl(tmp_path):
    """事件是瞬时的（字幕窗、网页的状态栏都会被下一句顶走），而 C45 要支撑的
    「试用完再回来调阈值」是事后行为 —— 只在事件里报等于没报。"""
    import json as jsonlib

    from chrometrans.asr.whisper_engine import Dropped, TranscribeResult

    class AllDroppedAsr:
        def load(self): pass

        def transcribe(self, seg):
            return TranscribeResult(utterance=None, dropped=(
                Dropped(text="编的", no_speech_prob=0.9, avg_logprob=-1.7,
                        compression_ratio=1.2),))

    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=lambda e: None, asr=AllDroppedAsr(),
                 translator=StubTranslator(),
                 segments=[Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))])
    eng.run()

    session = next(tmp_path.iterdir())
    lines = (session / "dropped.jsonl").read_text(encoding="utf-8").splitlines()
    assert [jsonlib.loads(l)["text"] for l in lines] == ["编的"]


def test_a_clean_session_writes_no_dropped_file(tmp_path):
    """没丢东西就不该多一个空文件 —— 空文件会被读成「丢了但没记下来」。"""
    eng, _ = _engine(tmp_path, StubTranslator(),
                     [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))])
    eng.run()

    assert not (next(tmp_path.iterdir()) / "dropped.jsonl").exists()


def test_dropped_text_in_the_event_is_bounded(tmp_path):
    """幻觉可以长到整段。事件要过 WebSocket，不该被一条文本撑爆。"""
    from chrometrans.asr.whisper_engine import Dropped, TranscribeResult

    long_text = "啊" * 5000
    events = []

    class LongDropAsr:
        def load(self): pass

        def transcribe(self, seg):
            return TranscribeResult(utterance=None, dropped=(
                Dropped(text=long_text, no_speech_prob=0.9, avg_logprob=-1.7,
                        compression_ratio=1.2),))

    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=events.append, asr=LongDropAsr(),
                 translator=StubTranslator(),
                 segments=[Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))])
    eng.run()

    drop = [e["data"] for e in events if e["data"].get("state") == "dropped"][0]
    assert len(drop["message"]) < 500, "事件里的文本要截断"
    assert len(drop["text"]) == 5000, "落盘的原文不截断"
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `uv run pytest tests/test_engine.py -v`
Expected: FAIL —没有 `dropped` 事件，也没有 `dropped.jsonl`

- [ ] **Step 3: 实现**

`src/chrometrans/engine.py`——`__init__` 末尾加一行：

```python
        self._dropped: list[dict] = []
```

文件末尾（`Engine` 类之外）加一个模块级函数：

```python
def _preview(text: str, limit: int = 200) -> str:
    """事件里的文本要截断：幻觉可以长到整段，而事件要走 WebSocket。

    落盘的 `dropped.jsonl` 不截断 —— 那是给人逐条看的。
    """
    return text if len(text) <= limit else text[:limit] + "…"
```

主循环里，Task 2 留下的那个 `result = self._transcribe(segment)` 块替换为
（差别只有中间插入的 `_report_drops` 一行）：

```python
                result = self._transcribe(segment)
                if result is None:
                    continue
                self._report_drops(result.dropped)
                utterance = result.utterance
                if utterance is None:
                    continue
```

加两个方法：

```python
    def _report_drops(self, dropped) -> None:
        """C45：丢弃必须可见。

        静默 continue 有两个后果：与本项目对静默失败的一贯取向矛盾（降级要播报、
        启动自检读到静音要报警），而且「先按默认值、试用后再调」这条约定会失效
        —— 偏严的误杀用户永远发现不了，只会觉得字幕时有时无。
        """
        for item in dropped:
            record = {"text": item.text,
                      "no_speech_prob": item.no_speech_prob,
                      "avg_logprob": item.avg_logprob,
                      "compression_ratio": item.compression_ratio}
            self._dropped.append(record)
            self._emit({"event": "status", "data": {
                "state": "dropped",
                "message": f"丢弃疑似幻觉：{_preview(item.text)}",
                **record}})

    def _render_drops(self, session: Path) -> None:
        """把被丢弃的段写成 dropped.jsonl。

        与另外两个视图同规矩：逐视图独立兜错，失败不阻塞流水线。
        """
        if not self._dropped:
            return
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n"
                       for r in self._dropped)
        self._write_view(session, "dropped.jsonl", body)
```

顶部 import 补 `import json`。

`_render_one` / `_render_views` 拆分。**下面 `_write_view` 里的 `write_with_fallback(...)` 及其两条失败分支，是从现在 `engine.py:151-166` 原样搬过来的，一个字都不要改**——那套「重试 → 改写旁路文件名 → 报错」的既有行为有测试锁着，重写一遍就是引入第二份可能不同的行为。

```python
    def _render_views(self, session: Path, writer: JsonlWriter) -> None:
        """从 JSONL 重渲全部派生视图。

        逐视图独立兜错：一个视图渲不出来（文件被占用、数据畸形）不能让另一个
        视图也缺一块 —— 两者的失败原因毫不相干，没理由互相连累。
        """
        self._render_drops(session)
        self._render_one(session, "captions.srt", render_srt, writer)
        self._render_one(session, "captions.md", render_markdown, writer)

    def _render_one(self, session: Path, filename: str, renderer,
                    writer: JsonlWriter) -> None:
        """渲染单个派生视图。失败不阻塞流水线（JSONL 才是权威源）。"""
        try:
            # read_all() 放在 try 里面：JSONL 读不出来（被独占、文件损坏）同样
            # 不能逃出去 —— 这条路径跑在 run() 的 finally 里。
            body = renderer(writer.read_all())
        except Exception as exc:
            # 重渲绝不能抛：它跑在 run() 的 finally 里，抛出去会吞掉
            # stopped 事件并让整个捕获进程崩掉（R22）。JSONL 完好，随时可重渲。
            self._emit({"event": "error", "data": {
                "message": f"{filename} 重渲失败，本次跳过（JSONL 完好，可随时重渲）：{exc}"}})
            return
        self._write_view(session, filename, body)

    def _write_view(self, session: Path, filename: str, body: str) -> None:
        """原子写一个派生文件，失败只报不抛。"""
        try:
            written = write_with_fallback(
                session / filename, body,
                retries=self._cfg.output.replace_retries,
                base_delay=self._cfg.output.replace_base_delay_s)
        except Exception as exc:
            self._emit({"event": "error", "data": {
                "message": f"{filename} 重渲失败，本次跳过（JSONL 完好，可随时重渲）：{exc}"}})
            return
        if written is None:
            self._emit({"event": "error", "data": {
                "message": f"{filename} 两处路径都写不进去，本次跳过（JSONL 完好，可随时重渲）"}})
        elif written.name != filename:
            self._emit({"event": "error", "data": {
                "message": f"{filename} 被占用，已改写到 {written.name}"}})
```

`_transcribe` 现在返回 `TranscribeResult | None`（异常时仍是 `None`），签名不变、注释补一句：

```python
    def _transcribe(self, segment: Segment):
        """返回 TranscribeResult；单段失败时返回 None（不得中断流水线，spec §6）。"""
        try:
            return self._asr.transcribe(segment)
        except Exception as exc:
            self._emit({"event": "error", "data": {
                "message": f"第 {segment.index} 段识别失败：{exc}"}})
            return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_engine.py -v`
Expected: PASS，含原有的 `test_render_failure_does_not_escape` 与两条「两个视图互不连累」

- [ ] **Step 5: 提交**

```bash
git add src/chrometrans/engine.py tests/test_engine.py
git commit -m "feat(engine): 幻觉丢弃发事件并落 dropped.jsonl（C45）"
```

---

### Task 6: 单语模式——`Cue` 语义与 engine 分支

中文不翻译。这不是「翻译失败」的另一种写法，JSONL 是权威文件，它的 schema 必须把两者分开（C44）。

**Files:**
- Modify: `src/chrometrans/models.py`、`src/chrometrans/engine.py`
- Test: `tests/test_models.py`、`tests/test_engine.py`

**Interfaces:**
- Consumes: `Config.translate.src`（Task 1）
- Produces:
  - `Cue.tgt_lang: str | None`
  - `is_monolingual(cue: Cue) -> bool`（`models.py`）
  - `Engine` 的 `running` 状态事件新增 `"bilingual": bool`
  - 单语时 `cue.target is None`、`cue.tgt_lang is None`、`cue.src_lang == cfg.asr.language`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_models.py`：

```python
def test_is_monolingual_reads_the_target_language():
    """C44 的判据：tgt_lang 为 None 是「本会话不翻译」，
    tgt_lang 有值而 target 为空是「翻译失败」。两者必须分得开。"""
    from chrometrans.models import Cue, is_monolingual

    mono = Cue(id=1, start=0.0, end=1.0, source="你好", target=None,
               src_lang="zh", tgt_lang=None)
    failed = Cue(id=2, start=0.0, end=1.0, source="hi", target=None,
                 src_lang="en", tgt_lang="zh-Hans")

    assert is_monolingual(mono) is True
    assert is_monolingual(failed) is False


def test_is_monolingual_accepts_the_dict_form():
    """事件、JSONL、GUI 三处流动的都是 dict —— 判据必须认得它。"""
    from chrometrans.models import is_monolingual

    assert is_monolingual({"source": "你好", "target": None,
                           "tgt_lang": None}) is True
    assert is_monolingual({"source": "hi", "target": "你好",
                           "tgt_lang": "zh-Hans"}) is False


def test_a_missing_key_is_not_monolingual():
    """键缺失说明喂进来的东西有问题，不能当成「本会话不翻译」。

    这条判据是有方向的：把畸形输入当成单语，会把一条其实存在的译文悄悄吞掉，
    而屏幕上什么异常都看不出来。退回双语渲染（显示「（未翻译）」）至少是可见的。
    既有的 test_gui_caption_window.py 里有四条手工构造的 cue dict 正是这个形状，
    它们必须照旧通过。
    """
    from chrometrans.models import is_monolingual

    assert is_monolingual({"source": "hello", "target": None}) is False
    assert is_monolingual({"source": "hello", "target": "你好"}) is False
```

追加到 `tests/test_engine.py`：

```python
def test_monolingual_session_never_calls_the_translator(tmp_path):
    """C44：单语会话不调翻译器，也不发翻译失败事件。

    「被调用即失败」比断言结果更硬 —— 结果是 None 也可能是翻译失败换来的，
    而那正是要区分的东西。
    """
    class ExplodingTranslator:
        name = "boom"

        async def translate(self, texts, src, tgt):
            raise AssertionError("单语会话不得调用翻译器（C44）")

    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    events = []
    eng = Engine(
        cfg=Config(output=OutputConfig(output_root=tmp_path),
                   asr=AsrConfig(language="zh"),
                   translate=TranslateConfig(src=None, tgt="zh-Hans")),
        on_event=events.append, asr=_stub_asr("这句话"),
        translator=ExplodingTranslator(), segments=segs)

    eng.run()

    cue = [e["data"] for e in events if e["event"] == "cue"][0]
    assert cue["source"] == "这句话"
    assert cue["target"] is None
    assert cue["tgt_lang"] is None, "tgt_lang 为 None = 本会话不翻译（spec §5.3）"
    assert cue["src_lang"] == "zh", "src_lang 取实际识别的语言，永不为 None"
    assert not [e for e in events if e["event"] == "error"], \
        "故意不翻译不得表现为翻译失败（C44）"


def test_target_language_is_recorded_in_bilingual_sessions(tmp_path):
    """反向也要锁：双语会话的 tgt_lang 必须保持有值，否则上一条就成了
    「不管什么会话都写 None」。"""
    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    eng, events = _engine(tmp_path, StubTranslator(), segs)

    eng.run()

    cue = [e["data"] for e in events if e["event"] == "cue"][0]
    assert cue["tgt_lang"] == "zh-Hans"
    assert cue["target"] == "译:hello world"


def test_running_status_reports_bilingual_both_ways(tmp_path):
    """会话级标志由启动的 status 事件给出（spec §5.3）—— 两个方向都断言。"""
    segs = []

    def bilingual_flag(cfg):
        events = []
        Engine(cfg=cfg, on_event=events.append, asr=_stub_asr("x"),
               translator=StubTranslator(), segments=segs).run()
        return [e["data"]["bilingual"] for e in events
                if e["event"] == "status" and e["data"].get("state") == "running"][0]

    assert bilingual_flag(Config(output=OutputConfig(output_root=tmp_path / "a"))) is True
    assert bilingual_flag(Config(output=OutputConfig(output_root=tmp_path / "b"),
                                 translate=TranslateConfig(src=None))) is False
```

`_stub_asr` 是新的模块级 helper（把 `_engine` 里那个内联 stub 抽出来，Task 5 已经改过它）：

```python
def _stub_asr(utterance: str):
    from chrometrans.asr.whisper_engine import TranscribeResult, Utterance

    class StubAsr:
        def load(self): pass

        def transcribe(self, seg):
            return TranscribeResult(
                utterance=Utterance(start=seg.start, end=seg.end, text=utterance))

    return StubAsr()
```

并让 `_engine` 复用它。

- [ ] **Step 2: 跑测试确认它失败**

Run: `uv run pytest tests/test_models.py tests/test_engine.py -v`
Expected: FAIL —`ImportError: cannot import name 'is_monolingual'`

- [ ] **Step 3: 实现**

`src/chrometrans/models.py`——字段与函数：

```python
@dataclass(frozen=True)
class Cue:
    """一条字幕。整句识别，一经写出即为终态（spec §5.5）。

    单语会话（中文）下 `tgt_lang` 为 None、`target` 为 None；双语会话下
    `tgt_lang` 有值。于是 `target is None` 的两种成因在 JSONL 里可区分：
    **tgt_lang 有值而 target 为空 = 翻译失败；tgt_lang 本身为 None = 本会话不翻译。**
    这就是 C44 在数据层的表达 —— JSONL 是权威文件，它的 schema 是契约。
    """

    id: int
    start: float          # 秒，相对捕获开始
    end: float
    source: str           # 原文
    target: str | None    # 译文；翻译整链失败时为 None
    src_lang: str         # 实际识别的语言，永不为 None
    tgt_lang: str | None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Cue":
        return cls(**d)


def is_monolingual(cue) -> bool:
    """本 cue 是否属于单语会话（不翻译）。

    接受 `Cue` 或它的 dict 形式：事件、JSONL、GUI 三处流动的都是 dict，而
    GUI 的渲染器收的正是 dict。判据只有这一份，四处渲染器据此保持一致（C43）。

    判据是**显式的 None**，不是「键缺失」。`tgt_lang` 是 cue schema 的必填
    字段，缺了说明喂进来的东西有问题 —— 那时退回双语渲染（显示「（未翻译）」），
    而不是当作「本会话不翻译」把译文悄悄吞掉。不安全的方向不该是兜底方向。
    """
    if isinstance(cue, dict):
        return "tgt_lang" in cue and cue["tgt_lang"] is None
    return cue.tgt_lang is None
```

`index.html` 那边要用**严格相等**（`=== null`，不是 `== null`）才对得上这条规则：JS 里 `undefined == null` 为真，宽松比较会把「键缺失」也算成单语。两处判据必须逐字同义，否则同一份 cue 在屏幕和网页上会是两个样子（C43）。

`src/chrometrans/engine.py`：

`__init__` 里加：

```python
        # 单语模式是会话级属性，由 translate.src 是否为 None 派生 —— 不另设
        # 一个需要和它保持同步的开关（spec §5.1 对 translate_src 就是这么做的）。
        self._bilingual = cfg.translate.src is not None
```

`running` 状态事件（`run()` 里第二个 `_emit`）：

```python
        self._emit({"event": "status", "data": {
            "state": "running", "model": self._cfg.asr.model,
            "device": self._cfg.asr.device,
            "session": str(session),
            "bilingual": self._bilingual}})
```

**`bilingual` 的用途**（spec §5.3 要求它进这条事件，不能是个没人读的字段）：三个显示面在 `running` 那行末尾加上模式，规则只有一条 —— `bilingual` 为真追加 ` · 翻译中`，为假追加 ` · 不翻译`。

落地在三处，后面的任务各自改：`cli.print_event` 的 running 分支（Task 8）、`gui/app.py` 的 `dispatch_status`（Task 11）、`index.html` 的 `setStatus`（Task 12）。三处文案必须一致 —— 这条本来就是既有约定（`cli.py:128` 的注释写着「文案与 index.html 的 setStatus() 逐字一致」）。

`Cue(...)` 构造：

```python
                cue = Cue(id=cue_id, start=utterance.start, end=utterance.end,
                          source=utterance.text, target=target,
                          # src_lang 取实际识别的语言而非 translate.src：
                          # 单语时后者是 None，而识别的语言是确定无疑的（spec §5.3）
                          src_lang=self._cfg.asr.language,
                          tgt_lang=(self._cfg.translate.tgt
                                    if self._bilingual else None))
```

`_translate`：

```python
    def _translate(self, text: str) -> str | None:
        """单语会话直接返回 None，**不调翻译器**（C44）。

        这里不是「翻译失败」，是「本会话不翻译」。区分靠 Cue.tgt_lang：
        单语时它是 None，双语时是有值的语言码。所以这里也不发任何事件 ——
        单语会话里一条 error 都不该有。
        """
        if not self._bilingual:
            return None
        try:
            results = asyncio.run(self._translator.translate(
                [text], self._cfg.translate.src, self._cfg.translate.tgt))
        except Exception as exc:
            self._emit({"event": "error", "data": {"message": f"翻译失败：{exc}"}})
            return None
        return results[0] if results else None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_models.py tests/test_engine.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/chrometrans/models.py src/chrometrans/engine.py tests/
git commit -m "feat(engine): 中文单语模式与 Cue 的语言字段语义（C43/C44）"
```

---

### Task 7: 两个文本派生视图的单语分支

**Files:**
- Modify: `src/chrometrans/output/srt.py`、`src/chrometrans/output/markdown.py`
- Test: `tests/test_srt.py`、`tests/test_markdown.py`

**Interfaces:**
- Consumes: `is_monolingual`（Task 6）
- Produces: `render(cues)` 的行为——**签名不变**。单语时不出译文。理由见「与规格的偏差 2」。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_srt.py`。**不动既有的 `_cue`**（它的 `tgt_lang="zh-Hans"` 是双语基线，7 条既有用例都靠它）——另加一个单语 helper：

```python
def _mono(i=1, start=0.0, end=1.0, src="你好", tgt=None):
    """单语会话的 cue：tgt_lang 是 None（C44 的判据）。"""
    return Cue(id=i, start=start, end=end, source=src, target=tgt,
               src_lang="zh", tgt_lang=None)


def test_render_monolingual_omits_the_target_line():
    """C43/C44：单语会话不出译文行，且不得出现（未翻译）这种「失败」措辞。"""
    text = render([_mono()])

    assert text == "1\n00:00:00,000 --> 00:00:01,000\n你好\n\n"
    assert "未翻译" not in text


def test_render_monolingual_ignores_a_stray_target():
    """会话级属性说了算：单语会话里即使混进一条带译文的 cue 也不出译文。

    否则「单语」就退化成逐条判断，四个渲染器迟早各判各的。
    """
    text = render([_mono(tgt="hello")])

    assert "hello" not in text


def test_blank_line_rule_holds_in_monolingual_mode():
    """C22 回归：单语分支不能把 cue 内的空行规则带坏。"""
    cues = [_mono(i, float(i), float(i) + 1.0, src="一\n\n二") for i in range(3)]

    blocks = [b for b in render(cues).split("\n\n") if b.strip()]
    assert len(blocks) == len(cues)
```

追加到 `tests/test_markdown.py`（同样另加 `_mono`，不动既有的 `_cue`）：

```python
def _mono(i=1, start=0.0, end=1.0, src="你好", tgt=None):
    """单语会话的 cue：tgt_lang 是 None（C44 的判据）。"""
    return Cue(id=i, start=start, end=end, source=src, target=tgt,
               src_lang="zh", tgt_lang=None)


def test_header_never_prints_none():
    """单语会话的表头不能渲染成 `zh → None`。

    这是 schema 放宽成 `str | None` 之后唯一一处真的会把 None 印出来的地方：
    表头是拼字符串，None 会老老实实变成 "None" 四个字母。
    """
    out = render([_mono()])

    assert "None" not in out
    assert "zh（不翻译）" in out


def test_monolingual_body_has_no_target_block():
    out = render([_mono(tgt="hello")])

    assert "hello" not in out
    assert "你好" in out


def test_bilingual_header_is_unchanged():
    """C43 的另一半：双语输出逐字不得变（回归既有格式）。"""
    out = render([_cue(1, 0.0, 1.0)])

    assert "en → zh-Hans" in out
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `uv run pytest tests/test_srt.py tests/test_markdown.py -v`
Expected: FAIL —SRT 会多出一行；Markdown 表头出现 `zh → None`

- [ ] **Step 3: 实现**

两处都是**改条件，不是重写函数** —— 现有 `render` 的排版逻辑有 7 条测试锁着，重写一遍只会引入无谓的差异。

`src/chrometrans/output/srt.py`，两处：

```python
# 第 7 行
from chrometrans.models import Cue, is_monolingual
```

```python
# 第 35 行，原来是 `if cue.target:`
        # 单语会话（tgt_lang is None）不出译文行。**从 cue 自己派生，不另收一个
        # 会话级参数**：JSONL 是权威源，渲染器必须能只凭它重渲（原规格 §7，
        # test_engine.py 的 "can_be_rerendered_from_jsonl_alone" 锁着这一点），
        # 多一个要与数据保持同步的入参就多一处能不同步。
        if cue.target and not is_monolingual(cue):
            lines += _cue_lines(cue.target)
```

`src/chrometrans/output/markdown.py`，三处：

```python
# 第 9 行
from chrometrans.models import Cue, is_monolingual
```

```python
# 第 37-45 行，原来 head 里第 40 行是
#   f"{cues[0].src_lang} → {cues[0].tgt_lang} ｜ {len(cues)} 条 ｜ "
    # 表头是拼字符串，None 会老老实实渲染成 "None" 四个字母 —— 单语会话下
    # `tgt_lang` 正是 None，所以这里必须分支（C44）。这一处是本单元唯一一个
    # 「不分支就会直接印出 None」的地方。
    scope = (f"{cues[0].src_lang}（不翻译）" if is_monolingual(cues[0])
             else f"{cues[0].src_lang} → {cues[0].tgt_lang}")
    head = [
        "# 网课字幕",
        "",
        f"{scope} ｜ {len(cues)} 条 ｜ "
        f"{format_clock(min(c.start for c in cues))} → "
        f"{format_clock(max(c.end for c in cues))}",
        "",
        "---",
    ]
```

```python
# 第 51 行
        target = (_one_line(cue.target)
                  if (cue.target and not is_monolingual(cue)) else "")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_srt.py tests/test_markdown.py tests/test_engine.py -v`
Expected: PASS（含三处「仅凭 JSONL 重渲」的等价性测试）

- [ ] **Step 5: 提交**

```bash
git add src/chrometrans/output/ tests/
git commit -m "feat(output): srt/markdown 的单语分支（C43/C44）"
```

---

### Task 8: CLI 的 `--language` 与 dropped 文案

**Files:**
- Modify: `src/chrometrans/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `LANGUAGES` / `DEFAULT_LANGUAGE` / `load_config(language)`（Task 1）
- Produces: `parse_args` 的 `args.language`；`print_event` 对 `state == "dropped"` 的处理

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_cli.py`：

```python
def test_parse_args_language_defaults_to_english():
    from chrometrans.config import DEFAULT_LANGUAGE

    assert parse_args([]).language == DEFAULT_LANGUAGE


def test_parse_args_rejects_unknown_language_and_lists_the_choices(capsys):
    """C38：未知值拒绝启动并列出可用值，不得静默退回默认语言。"""
    with pytest.raises(SystemExit) as exc:
        parse_args(["--language", "klingon"])

    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "klingon" in err
    assert "ru" in err and "zh" in err, "要列出可用值"


def test_language_choices_come_from_the_profile_table():
    """加语言时 CLI 不该还要单独改一处。"""
    from chrometrans.config import LANGUAGES

    for code in LANGUAGES:
        assert parse_args(["--language", code]).language == code


def test_main_forwards_the_language_choice(monkeypatch):
    """最危险的是 main 忘了把 args.language 传下去 —— 那就退回英语模型听俄语
    （C39）。这条测的就是那一行。

    只需要挡掉 Engine 一个：`WhisperEngine(cfg.asr)` 构造时不加载模型、
    `build_translator_chain` 不联网、`segment_source` 返回的是个还没被调用的
    工厂 —— 真正的重活都在 Engine.run 里，而 NoopEngine 不跑它。
    """
    import chrometrans.cli as cli
    from chrometrans.config import Config

    seen = {}

    def fake_load(language=cli.DEFAULT_LANGUAGE):
        seen["language"] = language
        return Config()

    class NoopEngine:
        def __init__(self, **kwargs): pass
        def run(self): pass
        def stop(self): pass

    monkeypatch.setattr(cli, "load_config", fake_load)
    monkeypatch.setattr(cli, "Engine", NoopEngine)

    assert cli.main(["--no-server", "--language", "ru"]) == 0
    assert seen["language"] == "ru"


def test_print_event_reports_dropped_segments(capsys):
    """C45：丢弃在终端上必须是可见的。

    与 warning / degraded 同类：都是「有东西没按预期走」。
    """
    print_event({"event": "status", "data": {
        "state": "dropped", "message": "丢弃疑似幻觉：谢谢观看", "text": "谢谢观看",
        "no_speech_prob": 0.95, "avg_logprob": -1.8, "compression_ratio": 1.1}})

    out, err = capsys.readouterr()
    assert "丢弃疑似幻觉" in err
    assert "谢谢观看" in err
    assert "谢谢观看" not in out, "状态不能污染 stdout"


def test_running_line_reports_the_session_mode(capsys):
    """C44：单语会话下「不翻译」是设计如此，值得说出来。

    两条都必须出现 —— 「不翻译」不含子串「翻译中」，所以只看一条在不在
    就足以判定另一条有没有漏印。
    """
    base = {"state": "running", "model": "large-v3-turbo", "device": "cuda"}
    print_event({"event": "status", "data": {**base, "bilingual": True}})
    print_event({"event": "status", "data": {**base, "bilingual": False}})

    err = capsys.readouterr().err
    assert "翻译中" in err
    assert "不翻译" in err


def test_running_line_says_nothing_about_mode_when_the_key_is_missing(capsys):
    """键缺失时什么也不追加 —— 把「不知道」说成「不翻译」是最危险的静默说谎。"""
    print_event({"event": "status", "data": {
        "state": "running", "model": "large-v3-turbo", "device": "cuda"}})

    err = capsys.readouterr().err
    assert "运行中" in err
    assert "不翻译" not in err and "翻译中" not in err
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL —`unrecognized arguments: --language`

- [ ] **Step 3: 实现**

`src/chrometrans/cli.py`——import 改成：

```python
from chrometrans.config import DEFAULT_LANGUAGE, LANGUAGES, load_config
```

`parse_args` 加一个选项（放在 `--process` 之后）：

```python
    # choices 由 LANGUAGES 生成：加语言时这里不用跟着改，而未知值由 argparse
    # 直接拒绝并列全可用值 —— 正是 C38 要的那句话。
    parser.add_argument("--language", default=DEFAULT_LANGUAGE,
                        choices=sorted(LANGUAGES),
                        help="音频语言；同时决定 ASR 语言与翻译源语言（C39）")
```

`main`：

```python
    cfg = load_config(args.language)
```

`print_event` 的状态分支改成（两处）：

```python
# 第 130-134 行，running 分支：加上会话模式
    if state == "running":
        # 模式由会话级的 bilingual 派生（Task 6）。中文单语会话下「不翻译」是
        # 设计如此 —— 不写出来，用户会以为翻译坏了（C44）。文案与 index.html
        # 的 setStatus() 一致。
        mode = data.get("bilingual")
        suffix = "" if mode is None else (" · 翻译中" if mode else " · 不翻译")
        if data.get("model"):
            print(f"— 运行中 · {data['model']} · {data['device']}{suffix}",
                  file=sys.stderr)
        else:
            print(f"— 运行中 · 按进程捕获（PID {data.get('pid')}）{suffix}",
                  file=sys.stderr)
```

```python
# 第 137-138 行，原来的 elif state in ("degraded", "warning"):
    elif state in ("degraded", "warning", "dropped"):
        # dropped 与这两个同类：都是「有东西没按预期走」。C45 要求它可见，
        # 而终端是唯一不会被下一句字幕顶走的地方 —— 它还有 scrollback。
        print(f"! {data.get('message')}", file=sys.stderr)
```

`mode is None` 时**什么也不追加**，而不是当成「不翻译」：键缺失说明有路径忘了带它，此时说「不翻译」恰恰是那种最危险的静默说谎（Task 6 有测试锁住 running 事件永远带这个键）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: 端到端试一次解析**

```bash
uv run chrometrans --help
```

Expected：帮助里出现 `--language {en,ru,zh}`。

- [ ] **Step 6: 提交**

```bash
git add src/chrometrans/cli.py tests/test_cli.py
git commit -m "feat(cli): --language 选项与 dropped 文案（C38/C45）"
```

---

### Task 9: `gui/settings.py` 持久化语言

**Files:**
- Modify: `src/chrometrans/gui/settings.py`
- Test: `tests/test_gui_settings.py`

**Interfaces:**
- Consumes: `LANGUAGES` / `DEFAULT_LANGUAGE`（Task 1）
- Produces: `GuiSettings.language: str`；`with_language(s, code) -> GuiSettings`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_gui_settings.py`：

```python
def test_language_defaults_to_english():
    from chrometrans.config import DEFAULT_LANGUAGE
    from chrometrans.gui.settings import GuiSettings

    assert GuiSettings().language == DEFAULT_LANGUAGE


def test_language_round_trips(tmp_path):
    from chrometrans.gui.settings import GuiSettings, load, save

    p = tmp_path / "gui.json"
    save(GuiSettings(language="ru"), p)

    assert load(p).language == "ru"


def test_unknown_language_falls_back_alone(tmp_path):
    """C38 的持久化面：坏 language 只连累自己 —— 沿用「坏字段不牵连好字段」
    的既有策略，别让一个手改坏的配置文件把窗口位置也搭进去。"""
    from chrometrans.config import DEFAULT_LANGUAGE
    from chrometrans.gui.settings import load

    p = tmp_path / "gui.json"
    p.write_text(json.dumps({"language": "klingon", "width": 1234}),
                 encoding="utf-8")

    got = load(p)
    assert got.language == DEFAULT_LANGUAGE
    assert got.width == 1234, "好字段要留下来"


def test_non_string_language_falls_back(tmp_path):
    from chrometrans.config import DEFAULT_LANGUAGE
    from chrometrans.gui.settings import load

    p = tmp_path / "gui.json"
    p.write_text(json.dumps({"language": 123}), encoding="utf-8")

    assert load(p).language == DEFAULT_LANGUAGE


def test_with_language_overrides_only_the_language():
    """「存了不读」和「读了不存」是最容易漏的两件事，所以这一步单独可测。"""
    from chrometrans.gui.settings import GuiSettings, with_language

    s = GuiSettings(x=10, y=20, width=640, font_px=30)

    assert with_language(s, "zh") == GuiSettings(x=10, y=20, width=640,
                                                 font_px=30, language="zh")
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `uv run pytest tests/test_gui_settings.py -v`
Expected: FAIL —`TypeError: __init__() got an unexpected keyword argument 'language'`

- [ ] **Step 3: 实现**

`src/chrometrans/gui/settings.py`：

```python
from chrometrans.config import DEFAULT_LANGUAGE, LANGUAGES
```

`GuiSettings` 加字段（放最后，保持既有字段顺序不动）：

```python
    language: str = DEFAULT_LANGUAGE
```

`_coerce` 的位置字段循环与 opacity 那段之后、`return GuiSettings(**out)` 之前插入（`default = GuiSettings()` 这个局部变量在函数开头就有，既有字段就是这么退默认的）：

```python
    # 白名单校验，与 CLI 的 choices 同源（C38）。非字符串一并退默认 ——
    # 沿用这个函数「坏字段只连累自己」的既有策略。
    value = raw.get("language")
    out["language"] = (value if isinstance(value, str) and value in LANGUAGES
                       else default.language)
```

`replace` 已经在文件顶部 import 过了（`from dataclasses import asdict, dataclass, replace`），`with_language` 不用再加。

文件末尾加：

```python
def with_language(s: GuiSettings, language: str) -> GuiSettings:
    """把当前选中的语言并进要保存的设置。

    单独提出来是为了能离线测：`gui/app.py` 那边够不到，而「存了不读」与
    「读了不存」恰恰是最容易漏的两件事。
    """
    return replace(s, language=language)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_gui_settings.py -v`
Expected: PASS（含原有的往返用例——它没传 `language`，两边都用默认值，仍相等）

- [ ] **Step 5: 提交**

```bash
git add src/chrometrans/gui/settings.py tests/test_gui_settings.py
git commit -m "feat(gui): 语言进 gui.json 持久化，白名单校验"
```

---

### Task 10: GUI 的语言下拉与接线

**Files:**
- Modify: `src/chrometrans/gui/launcher.py`、`src/chrometrans/gui/app.py`
- Test: `tests/test_gui_launcher.py`、`tests/test_gui_app.py`

**Interfaces:**
- Consumes: `LANGUAGES` / `DEFAULT_LANGUAGE`（Task 1）、`GuiSettings.language` / `with_language`（Task 9）
- Produces:
  - `LauncherWindow.selected_language() -> str`、`set_language(code) -> None`
  - `on_start` 用 `load_config(launcher.selected_language())` 现算 `cfg`
  - 停止 / 退出时把语言一并存进 `gui.json`；启动时读回来

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_gui_launcher.py`。用该文件既有的 `launcher` fixture（它已经把 `list_audio_processes` 换成空列表并在收尾关窗口），别自己 new：

```python
def test_language_dropdown_lists_every_language_in_table_order(launcher):
    """下拉项与顺序都由 LANGUAGES 生成 —— 加语言时这里不该还要单独改一处。"""
    from chrometrans.config import LANGUAGES

    got = [launcher.language_at(i) for i in range(launcher.language_count())]

    assert got == list(LANGUAGES)


def test_selected_language_defaults_to_english(launcher):
    from chrometrans.config import DEFAULT_LANGUAGE

    assert launcher.selected_language() == DEFAULT_LANGUAGE


def test_set_language_selects_that_code(launcher):
    launcher.set_language("zh")

    assert launcher.selected_language() == "zh"


def test_set_language_ignores_an_unknown_code(launcher):
    """界面只可能产出白名单里的值；给个野值不该把当前选择弄丢。"""
    launcher.set_language("zh")
    launcher.set_language("klingon")

    assert launcher.selected_language() == "zh"


def test_language_dropdown_is_disabled_while_capturing(launcher):
    """换语言要重建 engine，而捕获中本来就禁止重入 —— 所以下拉要禁用，
    不能只是「点了没反应」。"""
    assert launcher.language_editable()

    launcher.set_capturing(True)
    assert not launcher.language_editable()

    launcher.set_capturing(False)
    assert launcher.language_editable()
```

追加到 `tests/test_gui_app.py`：

```python
def test_startup_restores_the_saved_language(qapp, tmp_path, monkeypatch):
    """持久化的语言要真的回到下拉里 —— 存了不读等于没存。

    照 test_startup_path_runs_to_completion 的写法驱动整条启动路径（同一个
    settings_path 补丁、同一个「用重复定时器退循环」的退出口）。
    """
    import json

    from PySide6.QtCore import QTimer
    from chrometrans.gui import app as gui_app
    from chrometrans.gui import settings as gui_settings
    from chrometrans.gui.launcher import LauncherWindow

    path = tmp_path / "gui.json"
    path.write_text(json.dumps({"language": "zh"}), encoding="utf-8")
    monkeypatch.setattr(gui_settings, "settings_path", lambda: path)

    # 只验「main 把存下来的语言交给了下拉」，所以把 set_language 换成探针；
    # 下拉本身选不选得中由 test_gui_launcher.py 那几条管。
    seen = {}
    monkeypatch.setattr(LauncherWindow, "set_language",
                        lambda self, code: seen.update(code=code))

    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(qapp.quit)
    timer.start()
    try:
        assert gui_app.main([]) == 0
    finally:
        timer.stop()

    assert seen.get("code") == "zh", "启动时必须把存下来的语言放回下拉"
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `uv run pytest -m gui tests/test_gui_launcher.py tests/test_gui_app.py -v`
Expected: FAIL —`AttributeError: 'LauncherWindow' object has no attribute 'language_at'`

- [ ] **Step 3: 实现 `launcher.py`**

import 补：

```python
from chrometrans.config import DEFAULT_LANGUAGE, LANGUAGES
```

`__init__` 里，`self._page_check` 之后加：

```python
        self._lang_combo = QComboBox(self)
        # 项与顺序都来自 LANGUAGES：加语言时这里不用跟着改
        for code, profile in LANGUAGES.items():
            self._lang_combo.addItem(profile.label, code)
        self._lang_combo.setCurrentIndex(list(LANGUAGES).index(DEFAULT_LANGUAGE))
```

布局里，进程下拉与提示之间插入一行：

```python
        layout.addWidget(QLabel("音频语言：", self))
        layout.addWidget(self._lang_combo)
        layout.addWidget(self._hint_label)
```

（即把 `layout.addWidget(self._hint_label)` 换成上面三行。）

在 `# ---- 列表 ----` 小节之前加一小节：

```python
    # ---- 音频语言 ----

    def selected_language(self) -> str:
        return self._lang_combo.currentData()

    def set_language(self, code: str) -> None:
        index = self._lang_combo.findData(code)
        if index >= 0:
            self._lang_combo.setCurrentIndex(index)

    def language_count(self) -> int:
        """给测试用：下拉项数。"""
        return self._lang_combo.count()

    def language_at(self, index: int) -> str:
        """给测试用：第 index 项的 code。"""
        return self._lang_combo.itemData(index)

    def language_editable(self) -> bool:
        """给测试用：下拉是否可用。"""
        return self._lang_combo.isEnabled()
```

`set_capturing` 里加一行：

```python
    def set_capturing(self, capturing: bool) -> None:
        self._capturing = capturing
        # 换语言要重建 engine（换阈值集、换解码配置），而捕获中禁止重入 ——
        # 所以这里禁用，不是「点了没反应」。
        self._lang_combo.setEnabled(not capturing)
        # 已经在捕获就不必再轮询列表了
        if capturing:
            self._timer.stop()
        elif not self._timer.isActive():
            self._timer.start()
        self._sync_buttons()
```

- [ ] **Step 4: 实现 `app.py`**

四处小改，都是插入或改一行：

```python
# 第 21 行，原来只 import 了 restore_position 与 save
from chrometrans.gui.settings import restore_position, save as save_settings
from chrometrans.gui.settings import with_language
```

```python
# 第 166-167 行，原来是 caption.apply_settings(restore_position(load_settings(), _screens()))
    # 恢复上次的位置与语言；窗口落在已拔掉的显示器上就居中回来
    settings = load_settings()
    caption.apply_settings(restore_position(settings, _screens()))
    launcher.set_language(settings.language)
```

```python
# 第 182-183 行
    def persist() -> None:
        # 语言与窗口几何一起存 —— with_language 单独可测，见 gui/settings.py
        save_settings(with_language(caption.current_settings(),
                                    launcher.selected_language()))
```

```python
# 第 152 行：保留原样，但补一句注释说明它不负责捕获用的 cfg
    # 这里的 cfg 只用来生成「没配 key」的提示（纯环境变量，与语言无关）。
    # 真正跑捕获用的那份在 on_start 里现算 —— 用户可以在启动器里改语言。
    cfg = load_config()
```

然后 `on_start` 里现算 cfg。插入在第 227 行的 `return` 之后：

```python
        try:
            # 语言在启动器里随时可能被改，所以这里现算，不能用 main 开头那份。
            # 界面能选的语言一定在 LANGUAGES 里，这个兜底是给「设置文件被手改成
            # 未知值」留的 —— 宁可拒绝启动并说明，也不要退回英语模型去听俄语
            # （C38/C39）。
            session_cfg = load_config(launcher.selected_language())
        except ValueError as exc:
            relay.error.emit(str(exc))
            return
```

最后一行改成用这份新的：

```python
# 第 243 行，原来是 controller.start(proc, cfg=cfg, emit=emit)
        controller.start(proc, cfg=session_cfg, emit=emit)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest -m gui tests/test_gui_launcher.py tests/test_gui_app.py -v`
Expected: PASS（含既有的 `test_startup_path_runs_to_completion`）

- [ ] **Step 6: 手工确认一次**

```bash
uv run chrometrans-gui
```

切换语言下拉到「中文」→ 开始 → 停止 → 重新启动程序，确认下拉回到「中文」。这一步是为了验证「读了不存 / 存了不读」两端，自动化只覆盖了单侧。

- [ ] **Step 7: 提交**

```bash
git add src/chrometrans/gui/launcher.py src/chrometrans/gui/app.py tests/
git commit -m "feat(gui): 语言下拉、接线与持久化"
```

---

### Task 11: 字幕窗的单语分支与丢弃提示

字幕窗是 GUI 用户在捕获期间**唯一看得见**的界面（启动器在 `on_start` 里被 `hide()`），所以 C44 与 C45 的可见性都落在这里。

**Files:**
- Modify: `src/chrometrans/gui/caption_window.py`
- Test: `tests/test_gui_caption_window.py`

**Interfaces:**
- Consumes: `is_monolingual`（Task 6）
- Produces:
  - `render_cue_html(cue: dict, font_px: int) -> str`——**签名不变**，单语分支由 `cue["tgt_lang"]` 决定；`cue["notice"]` 非空时渲染一条提示行
  - `CaptionWindow.add_notice(text: str) -> None`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_gui_caption_window.py`：

前三条是纯函数，沿用该文件既有做法**不取 fixture**（`test_render_cue_html_*` 三条就是这样）；后两条要有窗口的，用该文件既有的 `window` fixture：

```python
def test_monolingual_cue_has_no_placeholder():
    """C44：中文模式下翻译是故意不做的，复用「翻译失败」那条状态会让每句话
    底下挂一条错误提示。"""
    from chrometrans.gui.caption_window import render_cue_html

    html = render_cue_html({"source": "你好", "target": None, "tgt_lang": None}, 22)

    assert "你好" in html
    assert "未翻译" not in html


def test_failed_translation_still_shows_the_placeholder():
    """反向也要锁：双语会话里翻译真挂了，那条提示必须还在。

    只测上一条的话，把 `if target:` 直接删掉也能通过。
    """
    from chrometrans.gui.caption_window import render_cue_html

    html = render_cue_html({"source": "hello", "target": None,
                            "tgt_lang": "zh-Hans"}, 22)

    assert "未翻译" in html


def test_a_cue_without_the_key_renders_like_a_bilingual_one():
    """喂进来的 dict 缺 tgt_lang 时，退回双语渲染 —— 不能把译文吞掉。

    该文件里原有的三条纯函数用例构造的正是这种 dict，它们必须照旧通过，
    这条是它们的显式化：

        test_render_cue_html_puts_the_source_above_the_target
        test_render_cue_html_invents_nothing_when_translation_is_missing
        test_render_cue_html_escapes_markup_in_the_text
    """
    from chrometrans.gui.caption_window import render_cue_html

    html = render_cue_html({"source": "hello", "target": "你好"}, 22)

    assert "你好" in html
    assert html.index("hello") < html.index("你好")


def test_monolingual_cue_uses_the_large_font_for_the_source():
    """单语时原文就是主角，不该还是那个灰色小字。"""
    from chrometrans.gui.caption_window import render_cue_html

    html = render_cue_html({"source": "你好", "target": None, "tgt_lang": None}, 22)

    assert "font-size:30px" in html, "单语下原文用译文的字号"


def test_notice_rows_render_and_survive_a_rerender(window):
    """C45：丢弃提示必须看得见，而且不能在改字号时凭空消失。

    提示行走的是同一个 Transcript 模型，所以 _rerender 重建时它还在 ——
    只 append 到控件上的写法会在改一次字号之后把它抹掉。
    """
    window.add_cue({"source": "第一句", "target": "first", "tgt_lang": "en"})
    window.add_notice("丢弃疑似幻觉：谢谢观看")
    assert "丢弃疑似幻觉" in window.toPlainText()

    window.set_font_px(28)

    assert "丢弃疑似幻觉" in window.toPlainText()
    assert "第一句" in window.toPlainText()


def test_clear_cues_also_clears_notices(window):
    """新会话开始时上一次的丢弃提示不该留着。"""
    window.add_notice("丢弃疑似幻觉：旧会话的")
    window.clear_cues()

    assert "丢弃疑似幻觉" not in window.toPlainText()
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `uv run pytest -m gui tests/test_gui_caption_window.py -v`
Expected: FAIL —单语 cue 里出现「未翻译」，且 `add_notice` 不存在

- [ ] **Step 3: 实现**

`src/chrometrans/gui/caption_window.py`——import 补：

```python
from chrometrans.models import is_monolingual
```

`render_cue_html` 整体替换：

```python
_NOTICE_COLOR = "#8a7a4a"


def render_cue_html(cue: dict, font_px: int) -> str:
    """一条字幕 → 一段 HTML。

    顺序与 captions.srt 和网页一致 —— 同一份内容在三个地方不该有三种排法。
    文本必须转义：ASR 输出里出现 `<` 或 `&` 时，当 HTML 解释会吃掉半句话。
    """
    notice = cue.get("notice")
    if notice:
        return (f'<div style="color:{_NOTICE_COLOR};font-style:italic;'
                f'font-size:{max(MIN_FONT_PX, font_px - 6)}px;'
                f'line-height:1.35">⊘ {html_escape.escape(notice)}</div>')

    source = html_escape.escape(cue.get("source") or "")
    target = cue.get("target")
    # C44：故意不翻译（单语会话）不得表现为翻译失败。判据是 tgt_lang，
    # 与 output/srt.py、output/markdown.py 用的是同一个 —— 四处不一致是
    # C43 明令禁止的。
    if is_monolingual(cue):
        return (f'<div style="color:#f5f6f8;font-size:{font_px + 8}px;'
                f'line-height:1.35">{source}</div>')

    parts = [
        f'<div style="color:#9aa0a6;font-size:{font_px}px;'
        f'line-height:1.35">{source}</div>'
    ]
    if target:
        parts.append(
            f'<div style="color:#f5f6f8;font-size:{font_px + 8}px;'
            f'line-height:1.35;margin-top:2px">{html_escape.escape(target)}</div>')
    else:
        parts.append(
            f'<div style="color:#6b7280;font-style:italic;'
            f'font-size:{font_px}px;margin-top:2px">（未翻译）</div>')
    return "".join(parts)
```

`is_monolingual` 在 Task 6 里已经定义成同时接受 `Cue` 与 dict（GUI 这边流动的是 dict），所以这里直接调，不用再改它。

`CaptionWindow` 加方法（放在 `clear_cues` 之后）：

```python
    def add_notice(self, text: str) -> None:
        """插一条提示行（C45 的丢弃提示走这里）。

        进的是同一个 Transcript 模型，不是直接 append 到控件：改字号或淘汰最旧
        一条都会触发 _rerender 重建整份文档，只 append 的写法会在那一刻凭空
        消失 —— 而「凭空消失」正是 C45 要消灭的东西。
        """
        self.add_cue({"notice": text})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest -m gui tests/test_gui_caption_window.py -v`
Expected: PASS（含既有的渲染 / 滚动 / 字号用例）

- [ ] **Step 5: 把事件接到窗口上**

`on_status` 现在是 `main()` 里的闭包（`app.py:185-202`），从测试里够不到。而 C45 的最后一公里恰恰就在这一行上 —— 事件发出来了但没人接，是会在前 11 个任务全绿的情况下发生的失败。所以先把它提到模块级再在 `main` 里接上。搬的过程中有两处改动：新增 `dropped` 分支（C45），以及 `running` 分支补上会话模式（Task 6 定义的 `bilingual` 消费者）。其余逐字照搬。

```python
def dispatch_status(data: dict, *, launcher, caption) -> None:
    """把一条 status 事件分发给界面。

    提到模块级只有一个理由：它是「事件 → 界面」的唯一映射，原来埋在 main() 的
    闭包里，测试够不到 —— 新增的 dropped 分支尤其需要被测到（C45 要的就是它
    可见）。
    """
    state = data.get("state")
    if state == "loading":
        launcher.set_status(data.get("message", "正在加载模型…"))
    elif state == "running":
        # 模式由会话级的 bilingual 派生（Task 6）。与 cli.print_event、
        # index.html 的 setStatus() 逐字一致（C43）。键缺失时什么也不追加。
        mode = data.get("bilingual")
        suffix = "" if mode is None else (" · 翻译中" if mode else " · 不翻译")
        if data.get("model"):
            launcher.set_status(
                f"运行中 · {data['model']} · {data['device']}{suffix}")
        else:
            launcher.set_status(
                f"运行中 · 按进程捕获（PID {data.get('pid')}）{suffix}")
    elif state == "warning":
        launcher.set_status(data.get("message", ""))
    elif state == "degraded":
        # 原文照搬并标红 —— 降级意味着声音隔离已经失效（原规格 §5.1）
        launcher.set_status(data.get("message", ""), degraded=True)
    elif state == "dropped":
        # C45：这张窗口是捕获期间唯一看得见的东西（启动器在 on_start 里已
        # hide），所以提示要落在这里，而不是只写进启动器那个看不见的状态行。
        message = data.get("message", "")
        caption.add_notice(message)
        launcher.set_status(message)
    elif state == "stopped":
        launcher.set_status("已停止")
```

`main` 里原来的 `relay.status.connect(on_status)`（第 211 行）连同 `on_status` 整个函数删掉，换成一行：

```python
    relay.status.connect(
        lambda data: dispatch_status(data, launcher=launcher, caption=caption))
```

然后 `tests/test_gui_app.py` 追加：

```python
def test_dropped_status_reaches_the_caption_window(qapp):
    """C45 的最后一公里：事件产生在 engine 里，而用户看的是字幕窗。

    断在这一行，前面所有用例照样全绿，而用户什么也看不到。
    """
    from chrometrans.gui.app import dispatch_status
    from chrometrans.gui.caption_window import CaptionWindow
    from chrometrans.gui.launcher import LauncherWindow

    caption = CaptionWindow()
    launcher = LauncherWindow()

    dispatch_status({"state": "dropped", "message": "丢弃疑似幻觉：谢谢观看"},
                    launcher=launcher, caption=caption)

    assert "丢弃疑似幻觉：谢谢观看" in caption.toPlainText()
    assert "丢弃疑似幻觉：谢谢观看" in launcher.status_text()
    caption.close()
    launcher.close()


def test_ordinary_statuses_still_reach_the_launcher(qapp):
    """回归：提到模块级不能把原有那五个状态接丢。"""
    from chrometrans.gui.app import dispatch_status
    from chrometrans.gui.caption_window import CaptionWindow
    from chrometrans.gui.launcher import LauncherWindow

    caption = CaptionWindow()
    launcher = LauncherWindow()

    dispatch_status({"state": "running", "model": "large-v3-turbo",
                     "device": "cuda"}, launcher=launcher, caption=caption)

    assert "large-v3-turbo" in launcher.status_text()
    caption.close()
    launcher.close()
```

- [ ] **Step 6: 跑测试并提交**

Run: `uv run pytest -m gui tests/test_gui_caption_window.py tests/test_gui_app.py -v`
Expected: PASS

```bash
git add src/chrometrans/gui/caption_window.py src/chrometrans/gui/app.py \
        src/chrometrans/models.py tests/
git commit -m "feat(gui): 字幕窗的单语分支与丢弃提示（C44/C45）"
```

---

### Task 12: 网页的单语分支与丢弃提示（含源码级断言测试）

`index.html` 目前**没有任何测试读它**（spec §8）。这一轮要往无覆盖处加两个分支，所以同时补上那条弱但真实的源码级断言。

**Files:**
- Modify: `src/chrometrans/static/index.html`
- Test: `tests/test_static_index.py`（新）

**Interfaces:**
- Consumes: 事件里 `data.tgt_lang`、`data.state == "dropped"`（Task 5/6）
- Produces: 无 Python 接口

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_static_index.py`：

```python
"""index.html 的源码级断言。

**这些断言很弱**，它们只证明「那段代码还在文件里」，不证明它跑得对。留着它们是
因为 index.html 此前零覆盖，而这一轮要往里加两个分支（单语、丢弃提示）——
为三个 JS 分支引入 playwright 不划算，但让它们可以被无声删掉更不划算。

判断依据写在这里，免得日后有人误以为它比实际更强。
"""
from pathlib import Path

import pytest

INDEX = (Path(__file__).resolve().parents[1]
         / "src" / "chrometrans" / "static" / "index.html")


@pytest.fixture(scope="module")
def source() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_monolingual_branch_exists(source):
    """C44：单语会话（tgt_lang 为 null）只出原文，不出「（未翻译）」。

    断言的是**严格相等那一行**，不是「源码里出现过 tgt_lang」—— 后者在
    `cue.tgt_lang == null` 这种宽松比较下也照样通过，而宽松比较在 JS 里会把
    `undefined`（键缺失）也算成单语，正好是我们要避免的兜底方向。
    """
    assert "cueIsMonolingual" in source
    assert "cue.tgt_lang === null" in source


def test_failed_translation_still_has_its_placeholder(source):
    """双语会话里翻译真挂了，那条提示必须还在。"""
    assert "（未翻译）" in source


def test_dropped_notices_have_a_branch(source):
    """C45：丢弃提示要在页面上有落点。"""
    assert "addNotice" in source
    assert "'dropped'" in source or '"dropped"' in source


def test_the_bilingual_header_still_renders_the_target(source):
    """双语路径没被误删。"""
    assert "未翻译" in source
    assert "data.message" in source


def test_the_running_line_reports_the_session_mode(source):
    """C44：单语会话下「不翻译」要在页面上说出来，与 cli / GUI 三处一致（C43）。"""
    assert "翻译中" in source
    assert "不翻译" in source
    assert "data.bilingual" in source


def test_the_page_is_still_a_single_self_contained_file(source):
    """网页是只读显示，不开新资源依赖 —— 一个 <script src> 就能让它在离线时白屏。"""
    assert "<script src=" not in source
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `uv run pytest tests/test_static_index.py -v`
Expected: FAIL —`assert 'cueIsMonolingual' in source`

- [ ] **Step 3: 实现**

`src/chrometrans/static/index.html`——样式表里加一行（`.tgt.missing` 那行之后）：

```css
  .notice { color: #8a7a4a; font-style: italic; font-size: 13px; }
```

`ws.onmessage` 那个分支块改成：

```js
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.event === 'cue') return addCue(msg.data);
    if (msg.event === 'status') return onStatus(msg.data);
    if (msg.event === 'error') return console.warn('pipeline error:', msg.data.message);
  };
```

`addCue` 之前插入两个判据与 `onStatus` / `addNotice`：

```js
// C44：tgt_lang 为 null = 本会话不翻译（单语）；tgt_lang 有值而 target 为空
// = 翻译失败。两者必须分得开 —— 复用后一条会让中文模式下每句话底下挂一条
// 错误提示。判据与 output/srt.py、output/markdown.py、caption_window.py 同源。
function cueIsMonolingual(cue) {
  // 严格相等，不能用 == null：JS 里 undefined == null 为真，宽松比较会把
  // 「键缺失」也算成单语，于是把译文吞掉。判据与 models.is_monolingual 同义。
  return cue.tgt_lang === null;
}

function addNotice(text) {
  const el = document.createElement('div');
  el.className = 'notice';
  el.textContent = `⊘ ${text}`;
  cues.append(el);
  while (cues.children.length > MAX_CUES) cues.removeChild(cues.firstChild);
  window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
}

// 状态事件里只有 dropped 是「事件」而不是「当前状态」——它描述的是刚发生的一件
// 事，塞进页眉只会被下一条状态顶走。所以它走 addNotice（C45）。
function onStatus(data) {
  if (data.state === 'dropped') return addNotice(data.message);
  return setStatus(data);
}
```

`setStatus` 的 `running` 分支改成（`data.bilingual` 的消费者，Task 6 定义的；文案与 `cli.print_event` 和 `gui/app.py` 逐字一致）：

```js
    case 'running': {
      // 模式由会话级的 bilingual 派生。中文单语会话下「不翻译」是设计如此 ——
      // 不写出来，用户会以为翻译坏了（C44）。键缺失时什么也不追加。
      const mode = data.bilingual == null ? ''
        : (data.bilingual ? ' · 翻译中' : ' · 不翻译');
      status.textContent = (data.model
        ? `运行中 · ${data.model} · ${data.device}`
        : `运行中 · 按进程捕获（PID ${data.pid}）`) + mode;
      break;
    }
```

`addCue` 整段替换（原文在 `index.html:43-56`）：

```js
function addCue(cue) {
  const el = document.createElement('div');
  el.className = 'cue';
  const src = document.createElement('div');

  if (cueIsMonolingual(cue)) {
    // 单语会话：原文就是主角，用大字号白字，且不出现「（未翻译）」（C44）
    src.className = 'tgt';
    src.textContent = cue.source;
    el.append(src);
  } else {
    src.className = 'src';
    src.textContent = cue.source;
    const tgt = document.createElement('div');
    tgt.className = cue.target ? 'tgt' : 'tgt missing';
    tgt.textContent = cue.target || '（未翻译）';
    el.append(src, tgt);
  }

  cues.append(el);
  while (cues.children.length > MAX_CUES) cues.removeChild(cues.firstChild);
  window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_static_index.py -v`
Expected: PASS

- [ ] **Step 5: 手工看一眼页面**

```bash
uv run chrometrans --no-server
```

开另一个终端起 `chrometrans`（带 server），或直接 `uv run chrometrans --language zh`，在浏览器打开 `http://127.0.0.1:8765/`，确认中文模式下只有一行白字、没有「（未翻译）」。

- [ ] **Step 6: 提交**

```bash
git add src/chrometrans/static/index.html tests/test_static_index.py
git commit -m "feat(web): 单语分支与丢弃提示，补源码级断言测试"
```

---

### Task 13: 收尾——全量测试、文档、样式一致性核对

**Files:**
- Modify: `README.md`
- Test: 全量

**Interfaces:**
- Consumes: 前 12 个任务的全部产出
- Produces: 无

- [ ] **Step 1: 全量测试（含 gui）**

Run: `uv run pytest -q && uv run pytest -m gui -q`
Expected: 两条都 PASS

- [ ] **Step 2: 四处的措辞一致性核对**

`cli.py:128` 的注释写着「文案与 index.html 的 setStatus() 逐字一致」。本轮新增了 `dropped`，四处（`cli.print_event`、`gui/app.py:on_status`、`index.html:onStatus`、字幕窗）都走同一个 `message` 字段，所以天然一致。核对一遍：`grep -rn "丢弃疑似幻觉" src/` —— 应当只在 `engine.py` 出现一次（构造 message 的那一行）。

- [ ] **Step 3: 全语言冒烟**

各跑一次，确认启动不报错、`--help` 里三个语言都在：

```bash
uv run chrometrans --help
uv run chrometrans --language klingon
```

第二条要**非零退出**并列出 `en` / `ru` / `zh`（C38）。

- [ ] **Step 4: 更新 README**

加一节「音频语言」与一节「阈值标定」，各自一句话加一条命令：

```markdown
## 音频语言

`--language en|ru|zh`（GUI 在启动器的「音频语言」下拉里选，选择会被记住）。

- `en` / `ru`：识别后翻译成简体中文
- `zh`：**中文单语模式**，不翻译。中文模式下字幕窗与网页只出一行原文，
  不会显示「（未翻译）」—— 那是翻译失败的说法，不是本会话不翻译的说法

语言同时决定 ASR 语言与翻译源语言，二者由同一份 `LanguageProfile` 派生
（`config.py` 的 `LANGUAGES`）。未知语言会拒绝启动并列出可用值。

## 阈值标定

幻觉过滤的三个阈值（`no_speech_prob` / `avg_logprob` / `compression_ratio`）
按语言各一套，写死在 `LANGUAGES` 里，每条都带 `calibrated_on` 指向标定报告
（`docs/superpowers/calibration/`）。改用一组数字要重跑标定：

```bash
uv run chrometrans-calibrate record --audio <素材> --language ru \
  --out rows.json
# 逐条核对 rows.json，把该标 nonspeech 的行的 label 改掉
uv run chrometrans-calibrate recommend --records rows.json --out report.md
```

标定判据是「误杀率 = 0」（真语音被丢弃的条数必须为零）；漏放率尽力而为，
因为 silero 切句器本身就拦掉了绝大多数非语音（见标定报告里的对照表）。

会话目录下多一个 `dropped.jsonl`：被幻觉过滤丢掉的每一段都在这里面，带原文
与三个统计量。误杀是静默的，所以它必须留痕。
```

- [ ] **Step 5: 提交**

```bash
git add README.md
git commit -m "docs: README 补音频语言与阈值标定两节"
```

- [ ] **Step 6: 确认分支状态**

```bash
git log --oneline main..HEAD
git status --short
```

Expected：本计划的提交都在，工作区干净。

---

## 收尾

- [ ] `uv run pytest -q` 与 `uv run pytest -m gui -q` 都全绿
- [ ] `docs/superpowers/calibration/` 下两份报告就位，且 `LANGUAGES` 里两条 `calibrated_on` 指得到它们
- [ ] `docs/superpowers/calibration/` 里的 C46 分支有明确结论：繁体残留率为 0（并写明）或已补确定性转换
- [ ] `README.md` 的「音频语言」与「阈值标定」两节写完
- [ ] 分支 `feat/multilang` 推 origin，开 PR

---

## 本计划明确不做

| 不做 | 理由 |
|---|---|
| 目标语言可选（俄译英等） | 用户没提出过。固定 `zh-Hans`（YAGNI） |
| Whisper 支持的其余 ~96 门语言 | 白名单是封闭的。放开就意味着存在未标定的语言，而那正是要消灭的状态（C38） |
| 自动语言检测（`language=None`） | 段长 0.35–15s，逐段检测不稳；且语言必须在启动时确定，阈值集随之而定 |
| 运行中切换语言 | 换语言要重建 engine。捕获中本来就禁止重入，下拉禁用即可 |
| en 的重新标定 | C40：把评测集上的默认值换成一个人 8 分钟录音上的自定义值是降级 |
| 网页前端的语言选择 | 网页是只读显示，选择权在 CLI / GUI |
| 真正的正/负样本扩充采集 | 需要新的素材（掌声、笑声、带人声的音乐）。本次只在已有素材上标定，`calibrated_on` 已写明适用范围受限 |
| `index.html` 的 DOM 级测试 | 为三个 JS 分支引入 playwright 不划算；用源码级断言替代，其强度已在测试文件顶部写明 |
| 引擎的可打断停止 | 已诊断清楚，用户明确「先放一放」。与本单元无交集 |
| `docs/gui-hand-run` 分支的合并 | 独立的未合并 PR，不属本单元 |

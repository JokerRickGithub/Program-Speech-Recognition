# 多语言支持：音频语言选择与按语言标定的幻觉过滤 — 设计文档

> **本文件是 `2026-09-22-live-caption-pipeline-design.md` 的补充规格**，只覆盖音频语言
> 选择、按语言标定的幻觉阈值、以及中文单语模式。原规格的捕获、切句、ASR、翻译、落盘
> 设计继续有效，此处不重复。
> **约束编号接续前两份文档**（原文档到 C30，GUI 规格到 C37），从 C38 起。

**日期：** 2026-09-24
**状态：** 待评审
**关闭项：** 原规格 §10「后续（Phase B）」第 1 条「加俄语」。

---

## 1. 背景与目标

### 问题

当前项目只处理英语音频：`AsrConfig.language` 与 `TranslateConfig.src` 两处都硬编码
`"en"`（`config.py:50`、`config.py:61`），`load_config()` 只从环境变量读凭据，CLI 的
argparse 没有语言选项，GUI 启动器也没有语言选择。底层能力其实都在——`large-v3-turbo`
是多语种模型，Azure 与 Google 翻译层都支持 `ru → zh-Hans`——但没有任何途径把它们接出来。

原规格 §10 已把「加俄语」列为 Phase B，并预告「翻译层已带 `src`/`tgt`，ASR 层放开
`language` 参数，届时不需要改动引擎」。本次实际做下来，那个预告只对了一半：语言参数
确实已经就位，但**按语言标定幻觉过滤阈值**这一块是原设计没有的，而它不能靠放开一个
参数解决。

### 目标

1. CLI 与 GUI 都能选择音频语言，选择被持久化
2. 幻觉过滤阈值按语言各一套，且每一套都有可追溯的标定依据
3. 中文音频走单语模式，输出保证简体

### 为什么阈值必须分语言

`avg_logprob` 的分布随语言漂移很大。用英语标定的阈值去过滤俄语，风险不是「漏放幻觉」
（那只是偶尔多一条编的字幕，看得见），而是**误杀**——真字幕被静默吃掉，用户看到的只是
「字幕怎么时有时无」，会归因到识别质量而不会归因到过滤器。

这个项目对静默失败一贯是零容忍的（降级到 loopback 必须显式播报、启动自检读到静音要
报警）。过滤器静默吞字幕是同类问题，按语言标定是它的应对。

---

## 2. 范围

### 范围内

- `LanguageProfile` 数据模型与三份语言常量（en / ru / zh）
- `load_config(language=...)` 单一入口，保证 `asr.language` 与 `translate.src` 联动
- CLI `--language` 选项
- GUI 启动器的语言下拉 + `gui.json` 持久化
- 中文单语模式（四个渲染器一致）
- 幻觉过滤的丢弃事件（从静默改为可见）
- 标定脚本与 ru / zh 的实测标定

### 范围外（明确不做）

| 不做 | 理由 |
|---|---|
| 目标语言可选 | 用户没提出过「俄译英」这类需求。固定 `zh-Hans`（YAGNI） |
| Whisper 支持的其余 ~96 门语言 | 白名单是封闭的。放开就意味着存在未标定的语言，而那正是要消灭的状态 |
| 自动语言检测（`language=None`） | 段长 0.35–15s，逐段检测不稳；且语言必须**在启动时确定**，因为阈值集随之而定 |
| 运行中切换语言 | 换语言要重建 engine（换阈值集、换解码配置）。捕获中本来就禁止重入，下拉禁用即可 |
| en 的重新标定 | 见 §3 C40 与 §6.4 |
| 网页前端的语言选择 | 网页是只读显示，选择权在 CLI / GUI |

---

## 3. 关键约束

### C38 — 语言集合封闭，未知值拒绝启动

`LANGUAGES` 是白名单，只有 `en` / `ru` / `zh`。未知 code **不得退回默认语言**——
退回就等于静默使用了另一门语言的阈值，与本次要消灭的失败模式同类。必须拒绝启动并列出
可用值。

理由：阈值表要保证「每一行都有人为它负责」。语言集合敞开，就必然出现没有标定依据的行。

### C39 — 源语言两侧由同一份 profile 派生，唯一入口是 `load_config`

`asr.language` 与 `translate.src` 必须来自同一份 `LanguageProfile`。二者不同步的后果是
**用英语模型去听俄语音频**——Whisper 不会报错，它会输出一段通顺但完全是编造的英文。
这是本项目里最危险的一类失败。

`AsrConfig.language` 与 `TranslateConfig.src` 两个字段**保留**（模块边界不变：`WhisperEngine`
只看 `AsrConfig`，不需要知道翻译层存在），但降级为「由 profile 派生的载体」。不变量收在
`load_config()` 这一个入口上，并由测试逐个 profile 锁住。

### C40 — 每门语言的阈值必须有可追溯的标定依据；「沿用上游默认值」是合法产出

每个 profile 必填 `calibrated_on`，写明这组数字的依据。

**「实测后决定不改」是合法的标定产出**，前提是真的测过。这一条是刻意的：它防止为了让
表格看起来填满而编造三套数字。把一个建立在评测集上的默认值，换成建立在一个人 8 分钟
录音上的自定义值，是降级而非升级。

### C41 — 标定以「误杀率 = 0」为唯一硬判据

- **误杀**（真语音被丢弃）：硬要求为 0
- **漏放**（非语音上的幻觉被放过）：尽力而为，不设硬指标

理由是供给与危害的方向恰好一致：正样本充足（ru 172.6s / 38 段，zh 496.4s / 59 段），
且误杀的失败是静默的；负样本天然稀缺（见 C42），而漏放的失败用户看得见。收紧阈值可以
压低漏放，但必然推高误杀——两个指标必须一起看，取拐点。

### C42 — 合成的平坦噪声不得作为负样本

实测（§6.1）：切句器拒绝数字静音、拒绝 -25 dBFS 白噪声、拒绝音乐。**平坦噪声根本到不了
Whisper 面前**，拿它标定等于给一条不存在的路径调参数。

负样本只能来自「真实但会骗过 silero 的音频」——掌声、笑声、带人声的音乐、多人交叠说话。
数量有限，故 C41 把漏放那侧降为尽力而为。

### C43 — 单语模式是会话级属性

「本会话不翻译」是整场会话的属性，不是逐 cue 的属性。四处分渲染器（`output/srt.py`、
`output/markdown.py`、`gui/caption_window.py`、`static/index.html`）必须一致地表现它。

### C44 — 故意不翻译不得表现为「翻译失败」

四处渲染器目前都把 `target is None` 读作「翻译失败」，显示「（未翻译）」灰字
（`caption_window.py:46-49`、`index.html:50-51`）。中文模式下翻译是故意不做的，复用该
状态会导致每句话底下挂一条错误提示。

### C45 — 幻觉丢弃必须可见

命中幻觉过滤的段当前是静默 `continue`（`whisper_engine.py:88-90`），不留任何痕迹。

这不可接受，有两个理由：

1. 与本项目对静默失败的一贯取向矛盾（见 §1）
2. **它是「先按默认值、试用后再调」这一约定能够成立的唯一前提**——误杀的失败方向
   （偏严）在静默状态下用户永远发现不了

### C46 — 中文输出必须为简体

中文 profile 必须带 `initial_prompt` 定向简体。但提示词不是保证，故：**本次标定必须
实测繁体残留率**（zh 的真实运行报告中给出，自动统计，不需人工）。残留率非零时必须补上
**确定性的**繁→简转换——不得依赖网络翻译来做这一步（网络失败会静默退回繁体）。

---

## 4. 架构

### 数据流

```
CLI  --language ru ─┐
                    ├─> load_config(language) ─> LanguageProfile
GUI  语言下拉 ──────┘           │
                    ┌───────────┴───────────┐
                    ↓                       ↓
        AsrConfig(language, 三个阈值)   TranslateConfig(src)
                    │                       │
                    ↓                       ↓
              WhisperEngine           ChainTranslator
                    │                       │
                    └───────> Engine <──────┘
                                │
                    cue / status / error 事件
                                │
        ┌────────────┬──────────┼──────────┬────────────┐
        ↓            ↓          ↓          ↓            ↓
   captions.jsonl  .srt       .md     GUI 悬浮窗    网页
```

`LanguageProfile` 是源语言两侧的唯一权威。`load_config` 是它进入 `Config` 的唯一入口。

### 模块划分

| 模块 | 职责 | 本次变化 |
|---|---|---|
| `config.py` | profile 表、`load_config`、联动不变量 | 新增 `LanguageProfile` + `LANGUAGES` |
| `cli.py` | `--language` 选项 | 新增选项 |
| `gui/launcher.py` | 语言下拉（只摆放，C36） | 新增控件 + `selected_language()` |
| `gui/settings.py` | 持久化 | 新增 `language` 字段 + 白名单校验 |
| `gui/app.py` | 接线 | 把语言传给 `load_config` |
| `asr/whisper_engine.py` | 提示词透传、丢弃记录 | `transcribe` 返回类型变化 |
| `engine.py` | 事件总线、单语模式 | 丢弃事件、`bilingual` 标志 |
| `output/srt.py`、`output/markdown.py` | 派生视图 | 新增 `bilingual` 参数 |
| `gui/caption_window.py`、`static/index.html` | 显示 | 单语分支 |
| `scripts/calibrate.py` | 标定（开发者工具） | 新增 |

---

## 5. 模块详细设计

### 5.1 `config.py` — `LanguageProfile`

```python
@dataclass(frozen=True)
class LanguageProfile:
    code: str                  # 界面与持久化的 id："en" / "ru" / "zh"
    label: str                 # 下拉里显示的名字
    asr_language: str          # -> Whisper 的 language 参数
    translate_src: str | None  # -> 翻译层源语言；**None = 该语言不翻译**
    initial_prompt: str | None # Whisper 提示词
    no_speech_prob_threshold: float
    avg_logprob_threshold: float
    compression_ratio_threshold: float
    calibrated_on: str         # 这组数字的依据；指向标定报告
```

**三个语言代码字段没有合并成一个**，因为它们对应三套不同的词汇表——en/ru 上碰巧重合，
zh 上立刻分叉（Whisper 认 `"zh"`，Azure 的源语言是 `"zh-Hans"`）。合成一个字段迟早要在
某处拆回来。

**`translate_src` 用 `str | None` 而不是另加 `monolingual: bool`**：少一个需要和别的字段
保持同步的状态，就没地方能不同步。「不翻译」由 `None` 表达。

`load_config(language=DEFAULT_LANGUAGE)` 是唯一入口：校验 code 属于 `LANGUAGES`（C38），
用 profile 构造 `AsrConfig` 与 `TranslateConfig`。

### 5.2 界面

**CLI**：`--language ru`，默认 `en`。目标语言不做选项。

**GUI**：进程下拉下方加「音频语言」下拉，三项。`LauncherWindow` 只摆放，加一个
`selected_language()` getter —— 与现有 `open_page_enabled()` 同款（C36：判断不在摆放层）。
`app.py` 读它并交给 `load_config`。

**持久化**：`GuiSettings` 加 `language: str = "en"`；`_coerce` 按白名单校验，未知值单独
退回默认，沿用现有「坏字段只连累自己」的策略（`settings.py:32-44`）。

**捕获中禁用**：换语言要重建 engine，故下拉在 `set_capturing(True)` 时禁用，与现有状态
切换同步。

### 5.3 单语模式

`translate_src is None` 时，`engine._translate` 不调用翻译器，`cue.target` 为 `None`，
且**不发翻译失败事件**（C44）。

**`Cue` 的两个语言字段随之得到确定语义**（JSONL 是权威文件，其 schema 是契约，不能不定义）：

| 字段 | 双语会话 | 单语会话 |
|---|---|---|
| `src_lang` | `profile.asr_language` | `profile.asr_language`（实际识别的语言，**永不为 None**） |
| `tgt_lang` | `cfg.translate.tgt` | **`None`** |

`src_lang` 改为取 `asr_language` 而非 `translate.src`——单语时后者是 `None`，而实际识别的
语言是确定无疑的。`tgt_lang is None` 因此成为「本会话单语」的自描述标志。

这也让 `target is None` 的两种成因在 JSONL 里可区分：`tgt_lang` 有值而 `target` 为空 =
**翻译失败**；`tgt_lang` 本身为 `None` = **本会话不翻译**（C44）。类型上
`Cue.tgt_lang` 由 `str` 放宽为 `str | None`。

会话级 `bilingual` 标志由 engine 在启动的 `status` 事件里给出，四处渲染器据此略去译文栏：

- `srt.render(cues, bilingual=...)` — 单语时不出译文行；C22「cue 内不得有空行」不受影响
- `markdown.render(cues, bilingual=...)` — 同上
- `render_cue_html(cue, font_px, bilingual=...)` — 单语时只出白字那一行，用大字号；
  **不得出现「（未翻译）」**
- `index.html` — 同上

### 5.4 丢弃可见（C45）

幻觉判定在 `WhisperEngine` 内（只有那里拿得到 `no_speech_prob` / `avg_logprob` /
`compression_ratio`），而事件总线在 `Engine` 手里。

**采用：`transcribe` 返回结果对象，携带被丢弃的段（文本 + 三个统计量），由 `Engine`
决定发什么事件。** ASR 层报事实、Engine 拥有事件总线，维持现有分工（原规格 §5.6 的
解耦要求）。

代价：`transcribe` 的返回类型变化，`test_engine.py` 的 `StubAsr` 跟着改。

被丢弃段的文本**必须进事件**——只说「丢了 N 段」不足以让用户判断那是垃圾还是真话，
而判断正是这个事件的全部目的。

### 5.5 `scripts/calibrate.py`

`uv run chrometrans-calibrate --language ru`，与现有两个 console script 并列。
需要 GPU 与样本文件，是开发者工具。

拆成三块（对应 §8 的测试策略）：

1. **负样本生成** — 纯函数（长度 / 电平 / 种子 → `ndarray`）
2. **统计与推荐** — 纯函数（`[(label, nsp, alp, cr)]` → 混淆矩阵 + 推荐阈值）
3. **跑 Whisper** — 重，不进 CI

**脚本不写 `config.py`**：数字由人贴进去、填上 `calibrated_on`、提交。与 C40「阈值是契约」
是同一件事的两面。

---

## 6. 实测发现（2026-09-24，设计依据）

以下结论均为本次实测，探针脚本在 `.superpowers/` 下。

### 6.1 切句器是一道比预想强得多的闸门

用真实 `Segmenter`（含 silero）喂各种非语音：

| 素材 | 输入 | 放行 |
|---|---|---|
| 数字静音 | 20.0s | **0 段** |
| 白噪声 -25 dBFS | 20.0s | **0 段** |
| 白噪声 -35 dBFS | 20.0s | **0 段** |
| 真实底噪（-48.9 dBFS） | 207.3s | **0 段** |
| 俄语真实语音 | 482.8s | 38 段 / 172.6s（35.8%） |
| 中文真实语音 | 528.7s | 59 段 / 496.4s（93.9%） |

**推论**：C42。平坦噪声与音乐根本到不了 Whisper 面前，合成负样本这条路不通。幻觉过滤器
是第二道防线，很少开火——但「很少」不等于「不需要」，短段（最小 0.90s，刚过
`min_speech_ms`）与过渡带仍是它的战场。

### 6.2 俄语素材的 35.8% 是正确行为，不是缺陷

逐 10 秒窗口的电平与 silero 概率对照：

| 区段 | 平均电平 | silero 概率 | 高概率帧占比 |
|---|---|---|---|
| 0–210s | -33 ~ -44 dBFS | 0.62 ~ 0.84 | 61–84% |
| 220–482s | **-25 ~ -32 dBFS** | **0.000** | **0%** |

后半段 4.5 分钟电平比人声段还高 10 dB、恒定在 -25 dBFS 上下，而 silero 概率**恰好为
0.000**（持续 260 秒）。这不是读漏人声，是一段**音乐**（对应素材字幕标注的「不全」——
字幕只覆盖 0–210s 的说话部分）。

中文作为对照组排除了「silero 对俄语有偏见」：中文响的帧里 **98.6%** 被正确判为语音。

### 6.3 正样本量偏小，但够回答「有没有误杀」

ru 38 段 / zh 59 段。对「拟合一个分布」偏少，对「误杀是零计数吗」够用——而 C41 的判据
正是后者。

### 6.4 faster-whisper 的默认值与本项目公式形状一致

`faster_whisper/transcribe.py:274-276` 的默认为 `compression_ratio_threshold=2.4`、
`no_speech_threshold=0.6`（`log_prob_threshold=-1.0` 为其配套）。其判定逻辑
（`transcribe.py:1215-1224`）为：

```
should_skip = no_speech_prob > no_speech_threshold
if avg_logprob > log_prob_threshold: should_skip = False
```

即「`no_speech_prob` 高 **且** `avg_logprob` 不高于门限」。本项目
`is_hallucination`（`whisper_engine.py:36-48`）为「高于门限 **且** 低于门限」，差异仅在
`avg_logprob == 门限` 这一个零测度点上。

**故 en 沿用上游默认值是成立的**（C40）：数字与公式形状都来自上游。

---

## 7. 错误处理

| 情形 | 行为 |
|---|---|
| `--language` 给了未知值 | 拒绝启动，列出可用值（C38） |
| `gui.json` 里 `language` 是未知值或非字符串 | 该字段单独退回 `"en"`，不连累 x/y/宽高 |
| 某 profile 的 `calibrated_on` 为空 | 测试失败（C40） |
| 单语会话 | 不调翻译器，不发翻译失败事件（C44） |
| 幻觉段被丢弃 | 发 `status` 事件，带被丢弃的文本（C45） |
| 中文输出出现繁体 | 标定报告记录残留率；非零则补确定性转换（C46） |

---

## 8. 测试策略

沿用既有约定：`addopts = -m 'not integration and not gui'`，两个 marker 已在
`pyproject.toml:37-43`。ASR 与翻译器一律 stub 注入（`test_engine.py:25-38`），
**本轮新逻辑全部落在这套约定里，跑测试不需要 GPU**。

| 层 | 断言 |
|---|---|
| `config` | 逐 profile：`asr.language` / `translate.src` 等于 profile 字段（C39 的锁）；每门都有非空 `calibrated_on` 与三个阈值（加语言忘标定即红）；未知 code 抛错且消息含可用值；zh 的 `translate_src is None` |
| `engine` | `translate_src is None` 时**不调 translator**（用「被调用即失败」的 stub 锁死）**且不发翻译失败事件**；单语会话 `cue.target is None`；启动 status 带 `bilingual`，两个方向都断言 |
| 渲染（4 处） | `bilingual=False` 时不出译文行，**且 C22 回归仍绿**；`render_cue_html(..., bilingual=False)` **不出现「（未翻译）」**；`bilingual=True` 时现有测试全绿即证明逐字无回归 |
| `cli` | `--language ru` 透传；未知值非零退出并列出可用值；缺省 en |
| `gui` | 下拉三项 + `selected_language()`；捕获中禁用；持久化往返；`_coerce` 对 `"klingon"` 与 `123` 都退 `"en"` 且不连累同 JSON 的其它字段 |
| `whisper_engine` | `initial_prompt` 透传（zh 有值、en/ru 为 None）；**阈值确实来自 cfg**——构造同一条段，断言「en 阈值会丢、ru 阈值不丢」，直接锁住分语言阈值生效 |
| `scripts/calibrate` | 第 1、2 块单测（含零正样本、零负样本、全丢、全不丢）；第 3 块不进 CI |

**第 3 块的替代物是真实运行**：报告提交进仓库
（`docs/superpowers/calibration/2026-09-24-ru.md` / `-zh.md`），内容含材料标识、混淆矩阵、
每条被丢弃段的文本与判定、繁体残留率、最终数字的推导。这里的冒烟测试**就是那次真实运行
本身**，报告是它的证据，`calibrated_on` 指向它。

### `index.html` 的零覆盖

`index.html` 目前**没有任何测试读它**。单语模式要往里加分支，即往无覆盖处新增代码。
处理：加一个**源码级断言测试**——读 `index.html` 文本，断言单语分支存在、双语分支未被
误删。弱，但真实，能挡住「有人把它删了」。

理由：GUI 与网页显示同一份内容不该有两种排法，这条在 `caption_window.py:33` 被明确当
原则写过；为它加一条弱测试，好过留下不一致。项目没有 DOM 测试设施，为三个 JS 分支引入
playwright 不划算。

---

## 9. 风险与未决问题

| 风险 | 影响 | 应对 |
|---|---|---|
| **负样本供给不足**（C42） | 漏放那侧无法充分标定 | C41 已把它降为尽力而为；由 C45 的丢弃事件在生产中兜底 |
| **ru/zh 标定样本只有单人单场** | 阈值可能只对这一个说话人成立 | `calibrated_on` 写明材料，结论的适用范围随之显式受限；后续有更多素材可再标一次 |
| **简体提示词不保证简体** | 中文输出夹繁体 | C46：本次标定实测残留率，非零则补确定性转换 |
| 标定用 GPU 与本机绑定 | 换机器标不出同一组数字 | 阈值是提交进仓库的常量，不依赖运行时环境；标定只在需要改数字时重跑 |
| `transcribe` 返回类型变化 | 影响 `test_engine.py` 的 stub | 已知代价，见 §5.4 |

**已解除**：

- ~~「俄语支持需要改动引擎」~~ —— 翻译层与 ASR 层参数本就就位，实际改动集中在外围
- ~~「底噪可作为负样本」~~ —— 实测证伪，见 §6.1（C42）

---

## 10. 对既有代码的改动清单

| 文件 | 改动 |
|---|---|
| `config.py` | 新增 `LanguageProfile` / `LANGUAGES` / `DEFAULT_LANGUAGE`；`load_config` 加 `language` 参数；`TranslateConfig.src` 改为 `str \| None` |
| `models.py` | `Cue.tgt_lang` 由 `str` 放宽为 `str \| None`（§5.3）；`src_lang` 的来源由 `translate.src` 改为 `asr.language` |
| `cli.py` | 新增 `--language`；`--language` 透传给 `load_config` |
| `asr/whisper_engine.py` | `transcribe` 返回结果对象（含被丢弃段）；`initial_prompt` 透传 |
| `engine.py` | 发丢弃事件；单语模式分支；status 事件带 `bilingual` |
| `output/srt.py` | `render` 加 `bilingual` 参数 |
| `output/markdown.py` | 同上 |
| `gui/caption_window.py` | `render_cue_html` 加 `bilingual` 参数；单语分支 |
| `gui/launcher.py` | 语言下拉 + `selected_language()`；捕获中禁用 |
| `gui/settings.py` | `GuiSettings.language` + 白名单校验 |
| `gui/app.py` | 把语言传给 `load_config` |
| `static/index.html` | 单语分支 |
| `pyproject.toml` | 新增 `chrometrans-calibrate` console script |
| `scripts/calibrate.py` | 新增 |
| `tests/` | 按 §8 逐层新增；`test_engine.py` 的 `StubAsr` 随 §5.4 调整 |
| `docs/superpowers/calibration/` | 新增标定报告 |

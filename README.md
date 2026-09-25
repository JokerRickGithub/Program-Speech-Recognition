# chrometrans

把 Chrome 网课的**实时字幕保存到本地**。

Chrome 自带的 Live Caption 画在浏览器的原生层里，任何扩展都抓不到它的文字。
所以这个项目不抓字幕，而是**自己重做一遍**：按进程抓 Chrome 的声音 → 本地语音识别 →
翻译成中文 → 同时写 JSONL、SRT 与 Markdown，并在本地网页上滚动显示。

## 它做什么

```text
Chrome 音频（按进程，48kHz 立体声）
      ↓  重采样 16kHz 单声道
   静音切句（silero VAD，512 样本/帧，段长上限 15s）
      ↓
faster-whisper large-v3-turbo（本地 GPU，不联网）
      ↓
   英译中（Azure / Google → 免 key 谷歌 → 免 key 微软 Edge）
      ↓
transcripts/<日期_时间>/captions.jsonl   ← 权威数据源
                        captions.srt     ← 派生视图：给播放器跟读
                        captions.md      ← 派生视图：给课后复习
      ↓
http://127.0.0.1:8765/   ← 本地字幕页
```

## 环境要求

- **Windows 10 2004+ 或 Windows 11**（按进程捕获依赖 `AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`）
- **Python 3.13+**
- **NVIDIA 显卡**：ASR 默认跑在 CUDA 上（`device="cuda"`）。

## 安装

```bash
uv sync
```

## 翻译

翻译链按顺序尝试，前一层失败就换下一层：

```text
Azure（要 key）→ Google Cloud（要 key）→ 免 key 谷歌 → 免 key 微软 Edge
```

**不配 key 也有中文。** 免 key 的谷歌通道实测可用（2026-09-23，连发 10 次全部成功）。
但它是个**未公开接口**（`translate.googleapis.com/translate_a/single?client=gtx`，
googletrans 用的就是它），谷歌随时可能改动或限流，所以只当兜底 ——
想稳定长期用，配一个 key 更踏实。最末尾那层微软 Edge 的免 key 通道已经**彻底下线**
（`edge.microsoft.com/translate/auth` 实测 404），留着只是以防它哪天回来。

### 配 Azure（推荐，免费层够用）

1. 在 Azure 门户建一个 **Translator** 资源，定价层选 **F0（免费）**：
   每月 200 万字符，一门网课远远用不完。
2. 建好后在资源的「密钥和终结点」页拿到 **密钥** 和 **区域**。
3. 设好这两个环境变量再启动：

```powershell
$env:AZURE_TRANSLATOR_KEY="把密钥粘这里"
$env:AZURE_TRANSLATOR_REGION="把区域粘这里"   # 例如 eastasia、westus2
uv run chrometrans
```

**区域填错是 401 的最常见原因**——它必须和资源所在区域一致。只有多服务资源才写
`global`，单独建的 Translator 资源写了 `global` 反而会 401。

> `$env:` 只对当前 PowerShell 窗口有效，关掉窗口就没了。想一劳永逸用 `setx`
> （但对已经开着的窗口不生效，得新开一个）。

### 配 Google Cloud

在 Google Cloud 建项目 → 启用 **Cloud Translation API** → 建一个 API key：

```powershell
$env:GOOGLE_TRANSLATE_KEY="你的key"
uv run chrometrans
```

两个都配就两个都试，Azure 优先。

### 怎么确认生效了

看 `captions.jsonl` 里每条字幕的 `target` 字段：有中文就是生效了，是 `null` 就是
所有翻译层都失败了。**终端不会单独为翻译失败报错**——它会安静地退回免 key 那层。

## 音频语言

`--language en|ru|zh`（GUI 在启动器的「音频语言」下拉里选，选择会被记住）。

- `en` / `ru`：识别后翻译成简体中文
- `zh`：**中文单语模式**，不翻译。中文模式下字幕窗与网页只出一行原文，
  不会显示「（未翻译）」—— 那是翻译失败的说法，不是本会话不翻译的说法

语言同时决定 ASR 语言与翻译源语言，二者由同一份 `LanguageProfile` 派生
（`config.py` 的 `LANGUAGES`）。未知语言会拒绝启动并列出可用值。

## 阈值标定

幻觉过滤的三个阈值（`no_speech_prob` / `avg_logprob` / `compression_ratio`）
按语言各一套，写死在 `config.py` 的 `LANGUAGES` 里，每条都带 `calibrated_on`
指向标定依据（`docs/superpowers/calibration/`）。改用一组数字要重跑标定：

```bash
uv run chrometrans-calibrate record --audio <素材> --language ru \
  --out rows.json
# 逐条核对 rows.json，把该标 nonspeech 的行的 label 改掉
uv run chrometrans-calibrate recommend --records rows.json --out report.md
```

标定判据是「误杀率 = 0」（真语音被丢弃的条数必须为零）；漏放率尽力而为，因为
silero 切句器本身就拦掉了绝大多数非语音。

**但在这套栈上，过滤器的两条轴目前都不可达，「误杀率 = 0」有一部分是空转的。**
实测 `no_speech_prob` 恒为 bit-exact 0.0（103 行输出无一例外），使
`nsp > 门限 且 alp < 门限` 这条 AND 整条不可能成立，`avg_logprob` 门限取任何值
都不生效；另一条轴 `compression_ratio` 实测最大 1.707，够不到候选网格下限 2.0。
于是全部 168 组候选阈值都丢不掉任何一行，`dropped.jsonl` 在真实素材上
必然为空。所以「没误杀」不等于「过滤有效」，别拿它当过滤器有效的证据。修这三条轴
是独立立项；权威结论写在 `config.py` 的 `LANGUAGES` 上方那段注释里，改阈值前先读它。

会话目录下会多一个 `dropped.jsonl`：被幻觉过滤丢掉的每一段都记在里面，带原文与
三个统计量。误杀是静默的，所以它必须留痕。

## 运行

```bash
uv run chrometrans
```

然后让 Chrome 播网课，再开一个标签访问 `http://127.0.0.1:8765/`。

终端会打印每一句的原文与译文，并在状态切换时打印 `运行中 / 警告 / 已降级 / 已停止`
（与网页上显示的文案一致）。字幕走 stdout、状态与错误走 stderr，所以
`uv run chrometrans > cues.txt` 能拿到干净的字幕流。

关掉网页不影响录制（显示层是可选的）。

### 命令行参数

| 参数            | 默认           | 说明                                                 |
| --------------- | -------------- | ---------------------------------------------------- |
| `--host`      | `127.0.0.1`  | 只允许回环地址，填别的会直接报错退出                 |
| `--port`      | `8765`       |                                                      |
| `--no-server` | 关             | 不启网页，只识别与落盘                               |
| `--process`   | `chrome.exe` | 要捕获的进程名（按进程捕获，不会混入其他软件的声音） |

### 输出

```text
transcripts/2026-09-23_1430/
├── captions.jsonl   # 每行一条 cue，追加写；崩了也只会丢最后一行
├── captions.srt     # 由 JSONL 渲染，播放器可直接打开
└── captions.md      # 由 JSONL 渲染，课后复习用（带时间戳锚点）
```

**JSONL 才是权威数据源。** SRT 与 Markdown 都随时可以只凭 JSONL 重新生成；
文件被播放器占用时会自动改写到 `captions.next.<后缀>`，不会阻塞录制。

两个派生视图各自独立渲染、各自独立兜错：其中一个写不进去，另一个照常产出。

`captions.md` 长这样 —— 时间戳是对回视频进度的锚点，原文在上、译文在下：

```markdown
**00:01:05** Now we can start writing some C code.

现在我们可以开始写一些 C 代码了。
```

## 图形界面

终端版要先把进程名查出来、再敲进参数里，这一步可以省掉。图形界面把「捕获哪个程序」
变成一个下拉框，另给一个置顶的悬浮字幕窗。

### 使用要求

- 环境要求与终端版相同（Windows 10 2004+、Python 3.13+、NVIDIA 显卡）
- **目标程序必须正在出声，才会出现在下拉框里。** 列表列的是**此刻正在渲染音频的
  进程**，不是「已打开的程序」—— 网课没在放，Chrome 就不在列表里。这不是 bug：
  按窗口去列会选中 Chrome 的主进程，而它**不放声音**，抓到的会是一路静音。
- **不要同时开两份**（图形界面和终端版各开一份也不行）。命名管道名是写死的，
  两份会撞在同一条管道上。
- **点「停止」时若目标程序此刻不出声**（比如视频正暂停），界面可能僵住最多 10 秒，
  随后提示「已放弃等待」；引擎要等到它再出一段音频才真正退出，在那之前重新开始会被
  拒绝。这是已知限制，根因在引擎侧（停止信号只在语音段边界被检查）。

### 使用顺序

1. **先让网课开始播放** —— 目标程序必须在出声
2. 运行图形界面：`uv run chrometrans-gui`
3. 在下拉框里选中正在放课的那个程序，形如 `chrome.exe (PID 12345)`
4. 点「开始」

第 1 步和第 3 步不能颠倒：第 3 步的列表里只会出现第 1 步已经在出声的程序。

## 首次运行

第一次会从 HuggingFace 下载 `large-v3-turbo` 模型（约 **1.6 GB**），之后走本地缓存。

如果下载卡在 0 字节不动（HuggingFace 的 Xet 传输在国内有时会这样），
设一个环境变量再跑：

```bash
export HF_HUB_DISABLE_XET=1
```

## 已知限制

- **最小化后仍能捕获（本机实测）。** 原先担心 Chrome 在窗口被**最小化**（不是被遮挡）
  时会自行暂停播放，2026-09-23 实测最小化后字幕照常出现。但这取决于播放器实现——
  若某门课最小化后没字幕，是播放器自己停了播放，把窗口放出来即可。
- **捕获失败会降级到系统级录音。** 连续重连不到 Chrome 时会退回系统级 loopback，
  此时**会混入 Edge、背景音乐等其他程序的声音**。终端与网页都会明确提示「已降级」——
  看到这个提示就说明**声音隔离已经失效**，请尽快重启 Chrome 后重新运行。
- **源语言限于白名单里的 `en` / `ru` / `zh`。** 其余语种尚未接入；目标语言固定是
  简体中文（见「音频语言」）。
- **长时稳定性尚未验证。** 设计上要求连续跑 2 小时以上不出问题，这一项需要你自己实测
  （见下）。

## 实测状态

**已在本机验证（2026-09-23）：**

1. **端到端联调** ✅ —— Chrome 播英文网课，终端逐句出英文；`captions.jsonl` 正常追加落盘。
2. **后台隔离** ✅ —— Chrome 播网课的同时用 Edge 放别的视频，字幕**只**出网课内容，
   全程未降级。（降级会把 Edge 的声音混进来，网页状态也会变红，一眼可辨。）
3. **最小化行为** ✅ —— Chrome 窗口最小化后仍有字幕（见上「已知限制」）。
4. **图形界面** —— 自动测试 ✅（2026-09-24 实测）：`uv run pytest -m gui` 42 条通过。
   `enumerate_audio_processes()` 实测（11 个音频会话）：稳态单次约 24–28 ms，2 秒轮询
   不卡界面，无需挪线程；冷启动首跑约 126 ms，但那一次在启动路径上（窗口还没显示），
   不在轮询路径上。手工实测（2026-09-24，真实桌面）：跑起来后逐句出中英字幕；拖动、
   缩放、置顶但不抢焦点正常；托盘可用（显示/隐藏字幕窗、打开启动器、停止捕获、退出）；
   退出正常，退出时把窗口位置写进 `%APPDATA%\chrometrans\gui.json`。

**仍需你自己验证：**

1. **长时 soak** —— 连续跑 2 小时以上，确认无音频劣化、无内存持续增长。
2. **翻译凭据** —— 想用 Azure / Google 的 key 的话，确认它真的可用（区域填错会 401）。
   不配也有中文（走免 key 谷歌），只是那层是未公开接口，长期稳定性没保证。

## 开发

```bash
uv run pytest              # 单元测试，不需要 GPU、不需要音频设备
uv run pytest -m integration   # 需要真实进程与音频设备的集成测试
```

测试用注入的假件替代真实音频设备、真实模型和真实网络，因此绝大多数用例是确定性的、离线的。

## 设计文档

- 规格：`docs/superpowers/specs/2026-09-22-live-caption-pipeline-design.md`
- 实现计划：`docs/superpowers/plans/2026-09-22-live-caption-pipeline.md`

## 许可证

MIT，见 [LICENSE](LICENSE)。

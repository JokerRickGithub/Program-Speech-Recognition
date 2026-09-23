# chrometrans

把 Chrome 网课的**实时字幕保存到本地**。

Chrome 自带的 Live Caption 画在浏览器的原生层里，任何扩展都抓不到它的文字。
所以这个项目不抓字幕，而是**自己重做一遍**：按进程抓 Chrome 的声音 → 本地语音识别 →
翻译成中文 → 同时写 JSONL 与 SRT，并在本地网页上滚动显示。

## 它做什么

```text
Chrome 音频（按进程，48kHz 立体声）
      ↓  重采样 16kHz 单声道
   静音切句（silero VAD，512 样本/帧，段长上限 15s）
      ↓
faster-whisper large-v3-turbo（本地 GPU，不联网）
      ↓
   英译中（Azure / Google）
      ↓
transcripts/<日期_时间>/captions.jsonl   ← 权威数据源
                        captions.srt     ← 派生视图，每 20 条重渲
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

## 配置翻译凭据（**必需**）

翻译链是 `Azure → Google → 免 key 兜底`。**免 key 那一层已经失效**（实测 5 次请求
5 次 HTTP 401），所以**没有 key 就等于没有中文翻译**：程序仍会正常识别英文、
正常落盘，但每条字幕的 `target` 都是 `null`，网页上显示为「（未翻译）」。

设好下面任意一个即可：

| 变量                        | 说明                                             |
| --------------------------- | ------------------------------------------------ |
| `AZURE_TRANSLATOR_KEY`    | Azure Translator 的 key（免费层 F0 即可）        |
| `AZURE_TRANSLATOR_REGION` | 上面的 key 对应的区域，**默认 `global`** |
| `GOOGLE_TRANSLATE_KEY`    | Google Cloud Translation 的 key                  |

两个都设就两个都试（Azure 优先）。都没有也能跑，只是没有译文。

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
└── captions.srt     # 由 JSONL 渲染，播放器可直接打开
```

**JSONL 才是权威数据源。** SRT 随时可以只凭 JSONL 重新生成；
`.srt` 被播放器占用时会自动改写到 `captions.next.srt`，不会阻塞录制。

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
- **目前只做英译中。** 俄语等其它语种尚未接入（配置里的 `src`/`tgt` 目前是
  `en` → `zh-Hans`）。
- **长时稳定性尚未验证。** 设计上要求连续跑 2 小时以上不出问题，这一项需要你自己实测
  （见下）。

## 实测状态

**已在本机验证（2026-09-23）：**

1. **端到端联调** ✅ —— Chrome 播英文网课，终端逐句出英文；`captions.jsonl` 正常追加落盘。
2. **后台隔离** ✅ —— Chrome 播网课的同时用 Edge 放别的视频，字幕**只**出网课内容，
   全程未降级。（降级会把 Edge 的声音混进来，网页状态也会变红，一眼可辨。）
3. **最小化行为** ✅ —— Chrome 窗口最小化后仍有字幕（见上「已知限制」）。

**仍需你自己验证：**

1. **长时 soak** —— 连续跑 2 小时以上，确认无音频劣化、无内存持续增长。
2. **翻译凭据** —— 确认 Azure / Google 的 key 真的可用。在此之前每条字幕的 `target`
   都会是 `null`：**没有 key 就没有中文**（见上「配置翻译凭据」）。

## 开发

```bash
uv run pytest              # 单元测试，不需要 GPU、不需要音频设备
uv run pytest -m integration   # 需要真实进程与音频设备的集成测试
```

测试用注入的假件替代真实音频设备、真实模型和真实网络，因此绝大多数用例是确定性的、离线的。

## 设计文档

- 规格：`docs/superpowers/specs/2026-09-22-live-caption-pipeline-design.md`
- 实现计划：`docs/superpowers/plans/2026-09-22-live-caption-pipeline.md`

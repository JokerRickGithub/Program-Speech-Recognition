# 本地实时双语字幕流水线 — 设计文档

- **日期**：2026-09-22
- **状态**：待评审（已经过一轮对抗性事实核查，证伪项已修正）
- **项目**：`chrometrans`

---

## 1. 背景与目标

### 问题

上网课时平台**不提供任何字幕**（无 CC），因此使用 Chrome 自带的实时字幕（Live Caption）做语音识别和翻译。但 Chrome 的实时字幕**无法保存**。

### 为什么不能直接抓 Chrome 的字幕

Chrome 的 Live Caption 由浏览器用本地语音识别（SODA）在**原生 UI 层**渲染，**不在网页 DOM 中**：

- 扩展程序无法通过 DOM 读取它，按 F12 选中只会选中底下的网页元素
- 没有任何官方 API 暴露它

因此「导出 Chrome 实时字幕」这条路**不存在**。可行的做法是自建一条完整的字幕流水线。

### 目标

把网课里老师讲的英文**实时**转成**中英双语字幕**显示出来，并**保存到本地**（SRT 字幕文件 + JSONL 完整记录），全程离线、音频不出本机。

---

## 2. 范围

### 范围内（Phase A）

| 能力 | 说明 |
|---|---|
| 抓取**指定进程**的输出声音 | Windows 按进程 loopback（免管理员）。只录 Chrome，**不混入** Edge / 背景音乐 / 其他程序 |
| 本地语音识别 | faster-whisper + CUDA，英文 |
| 英→中翻译 | 多引擎链式降级 |
| 实时双语显示 | 本地网页 |
| 落盘 | SRT（双语）+ JSONL（权威记录） |

### 范围外（明确不做）

| 不做的 | 原因 |
|---|---|
| 浏览器扩展前端 | Phase B；本设计的 `server.py` 已为其预留接口 |
| 俄语支持 | Phase B 再加；翻译层与 ASR 层已预留语言参数 |
| 说话人分离（diarization） | 非当前需求 |
| 麦克风输入 | 只抓指定应用的输出声音 |
| 云端 ASR | 隐私 + 成本；本机 GPU 资源充足 |
| SRT 内的样式定制 | SRT 支持 `<font color>` 等有限内联标记，足够；仅当需要中英不同**字体/字号**时才需 ASS |

---

## 3. 关键约束

这些约束是本设计的硬边界。**标注「已修正」的条目来自对抗性核查对初版设计的证伪**，实现时不得按初版理解。

### 3.1 音频捕获

| # | 约束 | 证据 |
|---|---|---|
| C1 | ⚠️ **已修正** — **不能假设任何读取模式会自动产生静音帧**。PortAudio 的 WASAPI loopback 在**没有任何应用在渲染音频时**，**回调模式和阻塞读都会停住**（`Pa_IsStreamActive()` 仍返回 1，但 `Read` 永久阻塞、回调不被调用） | PortAudio issue #935 正文 + 维护者 2024-07 与 2026-03 两次确认。初版"阻塞读能拿到静音帧、回调拿不到"的说法**被证伪** |
| C2 | **必须以设备原生采样率打开（通常 48000），再软件重采样到 16k** | 直接请求 16000 会 `OSError [Errno -9997] Invalid sample rate`（本机实测） |
| C3 | **`soxr.ResampleStream` 的 `delay() == 0.0`，输出与一次性重采样完全对齐**；但首块 100ms 只产出 1468 个样本（欠 ~8ms）。**不可在块之间调用 `clear()`**，收尾用 `resample_chunk(empty, last=True)` 排空 | 本机 soxr 1.1.0 实测 |
| C4 | **长时间运行的应用级音频捕获劣化（爆音/咔哒）在 Win11 24H2/25H2 上仍未解决** | OBS issue #8064 曾被归因于已修的 WASAPI bug，但 24H2/25H2 用户仍复现；OBS #13175（32.x，开播约 4 小时）属同类问题。**必须自建缓解并有长时测试**。注：OBS 的「应用程序音频捕获」用的正是 C26–C29 这条同一路径，故此风险对本设计**直接适用** |
| C26 | **按进程捕获不覆盖进程树** | 本机对照实验：抓播放进程本身 `rms=0.035270`（与 0.05 振幅测试音精确吻合）；抓其父进程 `rms=0.000000`（精确全零），且父进程根本不出现在音频进程列表里。**不能传 Chrome 主进程 PID**——必须用 `enumerate_audio_processes()` 找当前正在渲染音频的那个。Chrome 主进程与音频服务子进程**同名**（都叫 `chrome.exe`），只能靠此区分 |
| C27 | **Chrome 音频会话电平波动极大，归一化必须自适应且带静音门限** | 同一 PID 两次实测相差 **20 倍**：一次 `rms≈0.0017`（约 -55 dBFS），另一次 `rms≈0.0354`（约 -29 dBFS）——随内容与音量变化。该库本身**忠实无衰减**（0.05 振幅测试音被精确还原为 rms 0.0353 = 0.05/√2），故这是 Chrome 会话的真实输出。**电平不可预测，固定增益必然失配** |
| C28 | **该库没有内存流式接口，且输出文件被独占** | 导出表仅 11 个函数，无任何 PCM 回调；`PacStartCapture` 的输出文件是音频唯一出口（传 `NULL` = 仅监控、不产出音频）。捕获期间该文件**无法被第二个句柄打开**——20 次尝试全部 `PermissionError`（无 `FILE_SHARE_READ`）。**「tail 增长中的 WAV」方案已被实测否决** |
| C29 | **把命名管道路径当输出文件传入可行——这是唯一可用的流式路径** | 本机实测：DLL 成功连接管道并持续写入。速率 383,232 B/s（理论 384,000 B/s，吻合），块大小 3,840 B = **10 ms**（2ch / 48kHz / float32），到达间隔中位 10.4 ms、p90 10.9 ms。命名管道绕开了 C28 的独占锁限制，**无需落盘即得流式 PCM** |

### 3.2 切句（VAD）

| # | 约束 | 证据 |
|---|---|---|
| C5 | **`VADIterator` 没有 `max_speech_duration_ms` / `min_speech_duration_ms` / `neg_threshold`**，签名只有 5 个参数 | silero-vad 6.2.2 wheel 实际签名 |
| C6 | **超长段的失败方式是「硬报错」，不是「静默丢数据」** ⚠️ **已修正** — 在本设计的调用方式下（原始音频 + `vad_filter=False` + 不传 `clip_timestamps`），**≥30 秒的音频块会抛 `RuntimeError('No clip timestamps found...')`**。"只转写前 30 秒"的静默截断只发生在**调用方自己传 `clip_timestamps`** 的分支 | faster-whisper `transcribe.py:438-443` + issue #1355。初版"静默丢数据"的说法**方向错误，但结论（必须限制段长）不变，且必须在送进 ASR 之前限制** |
| C7 | **`VADIterator` 每次调用必须恰好 512 样本**（16kHz） | 其他长度抛 `ValueError`；不允许 padding / overlap |
| C8 | **`VADIterator` 报的 end 是「晚」不是「早」** ⚠️ **已修正** — 实际公式 `temp_end + speech_pad_samples - window_size`，减窗口几乎被加 pad 抵消，净效果是**比最后语音帧晚约 `speech_pad_ms`**。初版"早一个 32ms 窗口、会削掉尾音"的说法**被证伪** | `utils_vad.py:609` |
| C30 | **本项目不使用 `VADIterator`，改为直接调 silero 模型取「每帧语音概率」，切点规则全部自实现** ⚠️ **已回填** — 初版 §5.2 写的是「`VADIterator` 只负责门控」，但该节**同时要求的超长段切点规则需要逐帧概率**，而 `VADIterator` 恰恰不暴露概率（C5 已说明它的可调参数只有 5 个）。故实现改为 `load_silero_vad()` + 逐帧调用取概率 + 自写 `pick_cut_point`。**C5–C8 的结论依然全部成立**（帧长仍必须恰好 512、report 的 end 仍偏晚、超长段仍必须在上游限制），只是承载它们的对象不是 `VADIterator` | 实现：`src/chrometrans/audio/segmenter.py:39`（`pick_cut_point`）、`:72`/`:87`（模型适配与加载）；`grep -rn "VADIterator" src/` 仅剩一句解释性注释 |

### 3.3 ASR

| # | 约束 | 证据 |
|---|---|---|
| C9 | **每次 `transcribe()` 必须显式传 `vad_filter=False`** | faster-whisper 1.2.1 起默认 `True`；它会对我们切好的段重算 `clip_timestamps`，**拆掉我们切好的边界** |
| C10 | **`vad_filter=True` 时的实际默认值**：`threshold 0.5`、`min_silence_duration_ms 160`、`speech_pad_ms 400`、`max_speech_duration_s = chunk_length(30s)`、`min_speech_duration_ms 0` | `transcribe()` 内部显式覆盖了 `VadOptions` 的类默认值（类默认是 2000ms，**不适用**） |
| C11 | **`vad_filter=True` 时若通过 `VadOptions` 传 `max_speech_duration_s`，会被静默丢弃并强制为 `chunk_length`** | 同上 |
| C12 | **抑制幻觉的有效信号是 `compression_ratio` 的「高」值，方向不能搞反** — gzip 压缩比高 = 重复低熵输出 = 幻觉特征。阈值 `> 2.4`（保守 `> 1.4`）判为重复；静音丢弃用 `no_speech_prob > 0.6 AND avg_logprob < -1.0` | faster-whisper / openai-whisper 默认 `compression_ratio_threshold = 2.4` |
| C13 | **`word_timestamps=True` 的开销是 10–15% 常见、基准中最差约 38%**（非初版说的 5–10%）；且在某些代码路径会强制改为缓冲式（非流式）解码 | faster-whisper issue #45 基准（310.7s vs 225.9s）；维护者确认会跑第二遍对齐 |
| C14 | **`pip install faster-whisper` 在 Windows 上不完整** | 首次推理崩 `RuntimeError: Library cublas64_12.dll is not found or cannot be loaded`。必须装 `nvidia-cublas-cu12` 并把其 `bin` 目录**前置到 PATH**（`os.add_dll_directory` 无效）。cuDNN 不用管，ctranslate2 ≥4.5 自带 cuDNN 9 |
| C15 | **Python 3.13 可用，无需降级** | `ctranslate2` 自 4.6.0 起提供 `cp313-win_amd64` wheel；已在本机完整安装并 GPU 推理跑通 |
| C16 | **Whisper 的 `task='translate'` 只能翻成英文**，且 `large-v3-turbo` **静默忽略**该参数 | 因此中译必须由独立翻译层完成 |

### 3.4 翻译

| # | 约束 | 证据 |
|---|---|---|
| C17 | **免 key 端点不可作为单点依赖** | 微软 JWT auth 端点 `edge.microsoft.com/translate/auth` 在 2026-07 返回 404，一夜之间打断所有集成 |
| C18 | **Azure 官方端点 `api.cognitive.microsofttranslator.com` 从本机可达** ⚠️ **已修正** — 实测返回 401 + 真实 Azure 错误体（约 2.4s）。初版"不可达"的说法**被证伪**。Azure F0 免费额度为 **200 万字符/月**、永久免费、无需信用卡、50k 字符/请求、约 33.3k 字符/分钟限流 | 本机 live 探测 |
| C19 | **免 key 端点在持续负载下会失败**：实测 60 次快速请求 47 次成功（13 次 `SSL UNEXPECTED_EOF_WHILE_READING`）。**必须把传输层错误（SSL EOF / 连接重置）当作可重试（带退避），而不是降级到下一层** | 本机 live 压测。初版 20/20 成功的结果**低估了失败率** |
| C20 | **`html.escape()` 必须与 `html.unescape()` 成对出现，或干脆不转义** | 实测：免 key 端点并不会自动反转 `&lt;`；只转义不反转会让每个 `<` `>` `&` 在字幕里变成可见的 HTML 实体 |
| C21 | Google vs DeepL 的 EN→ZH 差距是**约 1–2 BLEU 且依基准而异**，不是初版引用的 5–8 BLEU | 2026 年 NLLB 基准：Google 35.6 vs DeepL 34.2 |

### 3.5 输出与显示

| # | 约束 | 证据 |
|---|---|---|
| C22 | **SRT 同一 cue 内不能出现空行** | 空行会终止 cue，导致整个文件错位 |
| C23 | **`os.replace()` 会在目标文件被占用时失败**（`OSError` / `PermissionError`，常见为瞬时 `WinError 5`）——**例如播放器正打开着这个 SRT 文件** | `MoveFileExW(MOVEFILE_REPLACE_EXISTING)` 语义。必须**有界重试 + 退避**，或写往另一个路径（如 `captions.next.srt`）后择机切换；同卷替换才是原子的，跨卷会退化为非原子复制 |
| C24 | **只有「真·独占全屏」（DirectX FSE，绕过 DWM）才会盖掉顶层窗口** ⚠️ **已修正** — 浏览器 F11、VLC、PowerPoint 用的都是**无边框全屏**，普通置顶窗口可以正常显示。初版"任何全屏都会灭掉覆盖层"的说法**过于绝对** | 已确认使用场景为窗口化，风险进一步降低 |
| C25 | **`http://localhost` 是安全上下文，`http://192.168.x.x` 不是** | 若将来把服务绑到局域网 IP，依赖安全上下文的浏览器 API 会静默失效。Phase A 只绑 `127.0.0.1` |

---

## 4. 架构

### 数据流

```
Chrome 音频会话（**仅此一路**，不含 Edge / 背景音乐 / 其他程序）
   │  AUDIOCLIENT_PROCESS_LOOPBACK，按进程隔离（C26）
   │  float32 立体声 48kHz，经命名管道流式读取，10ms/块（C29）
   ▼
[capture.py] ── 剥离 WAV 头 + 下混单声道 + soxr 重采样至 16kHz ──► 音频块
   │
   ▼
[segmenter.py] ── silero-vad 门控 + 自有切句规则（§5.2）──► Segment(start, end, audio)
   │
   ▼
[whisper_engine.py] ── faster-whisper, CUDA, 整句识别 ──► Utterance(start, end, text)
   │
   ▼
[translate/] ── ChainTranslator ──► 译文
   │
   ▼
[engine.py] ── 组装为 Cue ──┬──► output/  → captions.jsonl（权威，append+fsync）
                            │            → captions.srt（派生，原子重渲）
                            └──► server.py → WebSocket → 本地网页（实时双语显示）
```

**核心设计原则**：`engine.py` 是纯逻辑编排层，与外壳解耦。

- Phase A 的外壳是 `cli.py` + `server.py`
- Phase B 加 Chrome 扩展时，**只换外壳，引擎一行不动**

### 模块划分

```
chrometrans/
├── pyproject.toml
├── src/chrometrans/
│   ├── audio/
│   │   ├── capture.py         按进程捕获 + 管道读取 + WAV 头剥离 + 下混 + 重采样
│   │   └── segmenter.py       在线切句（自有规则，见 §5.2）
│   ├── asr/
│   │   └── whisper_engine.py  faster-whisper 封装
│   ├── translate/
│   │   ├── base.py            Translator 抽象 + ChainTranslator
│   │   ├── microsoft.py       免 key（Tier 0）+ Azure Key（Tier 1）
│   │   └── google.py          Cloud Key（Tier 2）
│   ├── output/
│   │   ├── jsonl.py           权威记录，append + fsync
│   │   └── srt.py             派生视图，原子重渲
│   ├── engine.py              编排，对外发事件
│   ├── server.py              FastAPI + WebSocket + 静态页
│   ├── config.py              全部可调参数集中于此
│   └── cli.py                 命令行入口
└── tests/
```

---

## 5. 模块详细设计

### 5.1 `audio/capture.py` — 按进程捕获（已实测验证）

**捕获模式：按进程捕获 Chrome 的音频**，不是系统级 loopback。
这样背景音乐、Edge、微信等其他程序的声音都不会混进来。

#### 为什么是「按进程」

使用场景是**同时开着 Chrome 和 Edge、还有背景音乐在播**，只要 Chrome 这一路。
系统级 loopback 把所有声音混在一起，无法分离；按进程捕获在 WASAPI 层就完成了隔离。

**本机实测**（20 秒 × 2 轮 + 对照组）：

| 目标 | 逐秒 RMS 表现 | 结论 |
|---|---|---|
| `chrome.exe` 第 1 轮 | 前 13 秒 0.0010–0.0040，后 7 秒**精确全零** | 信号严格跟随播放状态 |
| `msedge.exe`（对照） | 20 秒**全部精确 0.000000** | 进程隔离生效，无串音 |
| `chrome.exe` 第 2 轮 | 全程 0.0012–0.0028 稳定 | 两轮 RMS 比值 1.16x |

三条推论：

1. **隔离是真实的** —— Edge 进程同样出现在音频列表里，抓它得到的是**精确的零**，
   而不是微弱噪声底（该库在静音时输出精确零，可据此区分「真静音」与「串音」）
2. **信号跟随播放** —— Chrome 停声立即输出全零，不会录到真空
3. **背景音乐不受影响** —— `cloudmusic.exe` 等是独立进程，不进入 Chrome 的捕获流

#### 流式读取：命名管道（C29）

**该库没有内存流接口，且输出文件被独占（C28）** —— 这一点决定了整个模块的写法。
唯一的流式路径是：**创建一个命名管道，把管道名当作输出文件路径传给库**。

```python
import ctypes, threading
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
# CreateNamedPipeW / ConnectNamedPipe / ReadFile 的 restype+argtypes 声明见实现

PIPE_NAME = r"\\.\pipe\chrometrans"

def open_stream(pid: int):
    h = k32.CreateNamedPipeW(
        PIPE_NAME,
        0x00000001,          # PIPE_ACCESS_INBOUND
        0x00000000,          # PIPE_TYPE_BYTE | PIPE_WAIT
        255,                 # PIPE_UNLIMITED_INSTANCES
        1 << 20, 1 << 20,    # 收发缓冲
        0, None)
    if not h or h == wintypes.HANDLE(-1).value:
        raise OSError(f"CreateNamedPipeW 失败: {ctypes.get_last_error()}")

    # 必须先挂起一个 ConnectNamedPipe，DLL 才能连上
    threading.Thread(
        target=lambda: k32.ConnectNamedPipe(wintypes.HANDLE(h), None),
        daemon=True).start()

    cap = ProcessAudioCapture(pid=pid, output_path=PIPE_NAME)   # 管道名当文件路径
    cap.start()
    return h, cap
```

**实测性能**：块大小 3,840 B = **10 ms**（2ch / 48kHz / float32），
到达间隔中位 10.4 ms、p90 10.9 ms，速率 383,232 B/s（理论 384,000 B/s）。

#### 剥离 WAV 头

DLL 会先写一个 **68 字节的 WAV 头**。**不要依赖其中 `data` 块的长度字段** ——
管道是流式写入，写入时总长度未知，该字段不可靠。解析必须按块扫描，只取 `data` 的**起始偏移**：

```python
def split_header(buf: bytes):
    """扫描 RIFF 块，返回 (payload_offset, channels, rate, bits)；未收全则返回 (None,)*4。"""
    off = 12
    fmt = None
    while off + 8 <= len(buf):
        cid, sz = buf[off:off + 4], struct.unpack_from("<I", buf, off + 4)[0]
        if cid == b"fmt ":
            fmt = buf[off + 8:off + 8 + sz]
        elif cid == b"data":
            ch = struct.unpack_from("<H", fmt, 2)[0]
            sr = struct.unpack_from("<I", fmt, 4)[0]
            bits = struct.unpack_from("<H", fmt, 14)[0]
            return off + 8, ch, sr, bits          # 只看偏移，不信 sz
        off += 8 + sz + (sz & 1)
    return None, None, None, None
```

**不要用 Python 的 `wave` 模块**：该头是 `WAVE_FORMAT_EXTENSIBLE`，
`wave.open()` 会直接抛错（本机实测 `wave.Error: unknown extended format:
00000003-0000-0010-8000-00aa00389b71`）。

#### 读取循环

```python
while running:
    ok = k32.ReadFile(wintypes.HANDLE(h), buf, BUFSZ, ctypes.byref(got), None)
    if not ok:
        raise OSError(f"ReadFile 失败: {ctypes.get_last_error()}")
    data = buf.raw[:got.value]
    # 首个非空块负责剥离头部，之后 data 即纯 PCM
    pcm = np.frombuffer(data, np.float32).reshape(-1, ch)
    mono = pcm.mean(axis=1)
    yield resampler.resample_chunk(mono)
```

`ReadFile` 在 `PIPE_WAIT` 下**阻塞到有数据**，不需要轮询或 sleep ——
实测正是靠这个拿到 10 ms 的紧致粒度。

#### 目标 PID 的解析（C26）

**⚠️ 进程树不被覆盖** —— 这是最容易踩的坑：**不能传 Chrome 主进程的 PID**。
Chrome 的音频由子进程渲染，而该库只捕获目标进程自身。
由于 Chrome 主进程与音频服务子进程**同名**，唯一可靠的区分方式就是
「**是否出现在 `enumerate_audio_processes()` 里**」。

```python
def resolve_target(proc_names=("chrome.exe",)) -> int | None:
    cands = [p for p in ProcessAudioCapture.enumerate_audio_processes()
             if p.name.lower() in proc_names]
    if len(cands) == 1:
        return cands[0].pid
    return pick_loudest(cands)      # 多候选时逐个短捕获测 RMS，取真正出声的（见 §7）
```

PID 在 Chrome 重启后会改变，**必须动态解析，不可持久化**。

#### ✅ 静音期停流问题已在按进程路径上解决（C1）

初版设计对「无音频渲染时流是否会停住」存在分歧（PortAudio issue #935 与本地调研结论相反），
因此按最坏情况加了超时 + 补零兜底。

**按进程 + 管道路径的实测把这个分歧解决了**：Chrome 停声期间，管道**持续正常输出精确的零帧**，
速率与块大小完全不变，没有停顿、没有阻塞、没有丢帧。因此这条路**不需要**应用侧的停流兜底。

**C1 的超时保护仍然保留，但只作用于降级路径**（系统级 loopback / PortAudio）——
那个分歧本身并未被解决，只是被绕开了。

#### 增益归一化（C27）

本机实测 Chrome 会话电平约 **-55 dBFS**，比正常语音低约 25 dB。
该库本身**忠实无衰减**（对照实验中 0.05 振幅的测试音被精确还原为 rms 0.0353 = 0.05/√2），
所以这是 Chrome 会话的真实输出电平，必须在送进 ASR 前补偿。

```python
def normalize(chunk, target_rms=0.05, max_gain=20.0, floor_rms=1e-5):
    """把块缩放到目标电平。低于 floor 视为静音，原样返回，绝不放大底噪。"""
    rms = float(np.sqrt(np.mean(chunk ** 2)))
    if rms < floor_rms:
        return chunk
    return chunk * min(target_rms / rms, max_gain)
```

**归一化放在 `segmenter.py`，只作用于 VAD 判定为语音的段**，不放在捕获层 ——
这样既补偿了低电平，又不会把静音底噪放大到满幅（那正是历史上诱发 Whisper 幻觉的原因）。
上面 `floor_rms` 这道静音门限就是这条约束的落点。

其他要点：

- 重采样在**捕获层**完成（48k → 16k），归一化在**切句层**完成
- 重采样器**长驻**，块之间**不可 `clear()`**（C3）
- **长时间运行劣化**（C4）：实现管道与捕获流的重建机制，并做 >2 小时 soak test

#### 重采样

```python
rate = 48000   # 由 WAV 头读出，不可请求 16000（C2）
resampler = soxr.ResampleStream(rate, 16000, 1, dtype="float32")  # 长驻单例，永不 clear()
```

#### 与窗口状态无关

捕获发生在**系统输出层**，与任何窗口的可见性、焦点、遮挡状态无关。网课窗口可以最小化，只要有声音在播放就持续识别。字幕显示是独立的第二个窗口（`localhost` 页面），可单独置顶。

唯一例外见 §9「平台在窗口隐藏时自动暂停播放」。

#### 降级路径：系统级 loopback

按进程捕获在以下情况会失效，此时降级到 **PyAudioWPatch 的系统级 WASAPI loopback**：

- `enumerate_audio_processes()` 找不到目标进程（Chrome 没在放声音、或版本差异）
- 库在本机不受支持（`is_supported()` 为 False，需 Win10 2004+）
- 管道建立失败或中途断裂

**降级能录到声音，但会混入其他程序的声音**（Edge、背景音乐都会进来）。
UI 上必须明确提示当前处于降级模式 —— 否则用户会以为隔离仍然生效，这是最危险的失败方式。

系统级 loopback 走 PortAudio，有正常的流式读取接口，
但 **C1 的静音期停流分歧在此路径上并未解决**，超时 + 补零兜底必须保留。

### 5.2 `audio/segmenter.py` — 自有切句逻辑

**不使用 `VADIterator`**：直接调 silero 模型取**每帧语音概率**，分割规则全部由本模块实现
（C5–C8、C30）。理由见 C30 —— 超长段的切点规则需要逐帧概率，而 `VADIterator` 不暴露它。

| 规则 | 取值 | 说明 |
|---|---|---|
| 最小语音时长 | 350 ms | 低于此长度丢弃（C5：库无此参数） |
| 最大段长 | 15 s | **必须在送进 ASR 之前强断**，且 < 30 s（C6） |
| 断句静音阈值 | 600 ms | 连续静音超过此值即断句 |
| 帧长 | 512 样本 | 固定，不可变（C7） |

**超长段切点选择**：调用 `silero-vad` 6.2.x 的 `get_speech_timestamps_from_probs()`，它实现「最长低概率连续段 >98 ms」规则，在无静音时回退到硬切。

> ⚠️ **torch 依赖陷阱**：`sequence_vad.py` 顶部 `from .utils_vad import ...`，而 `utils_vad.py` 顶部 `import torch` / `import torchaudio`。因此**光 vendor `sequence_vad.py` 仍然会拉进 torch**。要真正脱离 torch，必须同时复制 `sequence_vad.py` **和** 一份去掉 torch 顶层依赖的 `get_speech_timestamps_from_probs`。实现时若发现 torch 体积不可接受，这是唯一的出路。

**段结束位置**（C8 修正后）：库报的 end 已含 padding，**不必**再加修正；仅在「后续帧把 `_last_speech` 推到 `temp_end` 之后」这一个边界情形下取 `max(reported_end, last_speech_end)`。

对外接口：

```python
@dataclass
class Segment:
    index: int
    start: float        # 秒，相对捕获开始
    end: float
    audio: np.ndarray   # float32 单声道 16kHz，长度 < 30s
```

### 5.3 `asr/whisper_engine.py`

```python
WhisperModel("large-v3-turbo", device="cuda", compute_type="int8_float16")

model.transcribe(audio,
    language="en", task="transcribe",
    vad_filter=False,                 # ★ 必须显式传（C9）
    word_timestamps=True,             # 开销 10–15%，最差 ~38%（C13）
    condition_on_previous_text=False,
    beam_size=5)
```

**模型选型依据**（RTX 3060 Laptop 6GB 实测）：

| 配置 | VRAM 峰值 | 10s 音频耗时 | 结论 |
|---|---|---|---|
| **large-v3-turbo + int8_float16** | **2.4 GB** | **0.43 s** | ✅ 采用 |
| large-v3 + float16 | 4890/6144 MiB | 0.42 s | ⚠️ 余量仅 1.2 GB，有 OOM 风险 |
| large-v3 + int8_float16 | — | 4.54 s | int8 仅省显存，**不提速**（反而更慢） |

ASR 约 23× 快于实时，**不是流水线瓶颈**，因此 `beam_size=5` 保精度而非砍到 1。

**启动期依赖处理**（C14）：进程启动时把 `nvidia-cublas-cu12` 的 `bin` 目录前置到 `PATH`。

**幻觉过滤**（C12，方向不要搞反）：

```python
if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:  → 丢弃（静音/噪声）
if seg.compression_ratio > 2.4:                          → 丢弃（重复性幻觉）
```

**时间戳归一**：Whisper 返回段内相对时间，需加 `segment.start` 还原为绝对时间，并用 word timestamps 把 cue 边界对齐到词。

### 5.4 `translate/`

```python
class Translator(Protocol):
    async def translate(self, texts: list[str], src: str, tgt: str) -> list[str]: ...
```

`ChainTranslator` 按序尝试：

| 层级 | 实现 | 说明 |
|---|---|---|
| Tier 0 | `MicrosoftTranslator`（免 key） | 支持一次 50 行批量。**best-effort**，见下 |
| Tier 1 | `MicrosoftTranslator`（Azure Key） | **F0 免费 200 万字符/月、永久免费、无需信用卡**（C18，已验证可达） |
| Tier 2 | `GoogleTranslator`（Cloud Key） | 备选 |

实现要点：

- **绝不能只有 Tier 0**（C17）。⚠️ **2026-09-23 修正**：用户一个 key 都不配时，这条
  字面要求做不到 —— 链上必然只剩免 key 层。决定是**不假装满足，改为满足它的本意**：
  当初 C17 是「微软 auth 端点一夜 404 打断所有集成」写下的，痛点是**静默**，所以
  没配 key 时启动即由 `keyless_notice()` 明确告知中文依赖的是未公开端点。想真正
  满足 C17 就得配 key
- **传输层错误必须重试而不是降级**（C19）：`SSL UNEXPECTED_EOF_WHILE_READING`、连接重置等属瞬时抖动，直接 `except Exception` 掉到下一层会让**每一次瞬时抖动都静默降级**。必须对传输错误做**有界重试 + 退避**，只有确定性失败（4xx）才降级
- **转义必须成对**（C20）：若对 payload 做 `html.escape()`，**必须在返回后 `html.unescape()`**，否则每个 `<` `>` `&` 都会在字幕里显示成 HTML 实体。更简单的做法是**不转义**
- 批量映射严格按序号回填，避免译文错位到别的时间轴
- 接口已带 `src`/`tgt`，加俄语时无需改动实现

### 5.5 `output/`

**JSONL 是权威源，SRT 是派生视图。**

```
transcripts/2026-09-22_1420/
├── captions.jsonl     权威：每 cue 一行，append + flush + fsync
└── captions.srt       派生：每 ~20 cue 重渲一次
```

JSONL 单行结构：

```json
{"id": 1, "start": 3.12, "end": 6.48, "source": "The derivative of this function is continuous", "target": "这个函数的导数是连续的", "src_lang": "en", "tgt_lang": "zh-Hans"}
```

因为是**整句识别**（不做流式），cue 一经写出即为终态，无需修订记录。

SRT 双语格式：

```
1
00:00:03,120 --> 00:00:06,480
The derivative of this function is continuous
这个函数的导数是连续的

```

- 英文第 1 行、中文第 2 行，中间**单个换行**
- **cue 内绝不放空行**（C22），空行只出现在 cue 之间
- **原子替换必须带重试**（C23）：目标是 `captions.srt`，而用户可能正用播放器打开它 → `os.replace()` 会抛 `WinError 5`。做法是有界重试 + 退避，失败则写往 `captions.next.srt` 并记日志，不阻塞流水线
- 崩溃场景下最多丢失最近 ~20 条 cue 的 SRT 视图，JSONL 内容完好，可随时重渲

### 5.6 `engine.py`

编排全流程，对外只发三种事件：

```python
{"event": "cue",    "data": {"id": 1, "start": 3.12, "end": 6.48,
                             "source": "...", "target": "..."}}
{"event": "status", "data": {"state": "running", "model": "large-v3-turbo",
                             "device": "cuda"}}
{"event": "error",  "data": {"message": "..."}}
```

- 与显示层、存储层完全解耦
- 单条 cue 的处理失败**不得中断流水线**，转为 `error` 事件后继续

### 5.7 `server.py`

FastAPI + WebSocket + 静态页。

- **仅监听 `127.0.0.1`**（C25），不对外暴露
- 选 WebSocket 而非 SSE 的理由是**需要双向控制**（前端发启停/换语言命令），而非延迟差异——本机回环下两者的服务端推送延迟都是亚毫秒级
- 前端消费 `cue` 事件追加卡片并自动滚动
- 显示双语：英文较小、中文较大（可读性优先）

> Phase B 的 Chrome 扩展将连接同一套 WebSocket 接口，无需改动引擎。

### 5.8 `config.py`

所有可调参数集中一处：音频设备与块大小、切句阈值、模型与 compute_type、幻觉过滤阈值、翻译链顺序与 key、输出目录。

---

## 6. 错误处理

| 场景 | 处理 |
|---|---|
| 静音期底层停流（C1） | 读取超时 → 应用侧补静音帧，切句器时序不卡死 |
| 音频设备被拔/切换 | 捕获线程重建流，发 `status` 事件，不退出 |
| 长时运行音频劣化（C4） | 定期重建流；soak test 验证 |
| cuBLAS / CUDA 不可用 | 启动时明确报错并给出修复指引，不静默退化到 CPU |
| 段长 ≥ 30s | **不可能发生**——切句层已强制 15s 上限（C6） |
| 翻译传输抖动 | 有界重试 + 退避；只有确定性失败才降级（C19） |
| 翻译整条链全失败 | 该 cue 的 `target` 置 `null`，原文照常显示与保存 |
| 单段 ASR 失败 | 记录并跳过，继续后续段 |
| SRT 文件被占用（C23） | 重试退避，失败则写 `captions.next.srt` 并记日志 |
| 网页未打开 | 照常识别与落盘（显示层是可选的） |
| 磁盘写入失败 | 发 `error` 事件并停止，避免静默丢字幕 |

---

## 7. 测试策略

| 层 | 方式 | 覆盖内容 |
|---|---|---|
| `segmenter` | 单元 / 合成音频 | 切点位置、15s 强断、350ms 短噪声过滤、512 样本帧长约束、**归一化门限：静音块绝不被放大**（C27） |
| `output/srt` | 单元 | 时间码格式、**断言 cue 内无空行**（C22）、目标被占用时的重试路径（C23） |
| `output/jsonl` | 单元 | 增量写入、崩溃后可重渲出等价 SRT |
| `translate` | 单元 / mock HTTP | 批量序号映射、**传输错误重试而非降级**（C19）、**escape/unescape 成对**（C20）、链式降级路径 |
| `asr` | 单元 / mock | 相对→绝对时间还原、`vad_filter=False` 确被传入、幻觉过滤方向正确（C12） |
| `audio/capture` | **集成（需真设备）** | 见下「捕获层专项测试」 |
| 长时 soak | 集成 | >2 小时连续运行：内存、音频劣化（C4）、SRT 重渲稳定性 |

纯逻辑层（切句 / SRT / 翻译 / 时间归一）全部可离线单测，**不依赖显卡与真实网课**。

### 捕获层专项测试

这一层已完成一轮 spike 验证（结论见 §3.1 C26–C29 与 §5.1）。
但**下列行为尚未覆盖，是「已实测」之外仍需在实现期补齐的部分**：

| 项 | 为什么必须测 |
|---|---|
| **PID 动态重解析** | 关掉 Chrome 再打开，PID 会变。必须验证能自动找回，而不是静默录到空 |
| **多候选消歧** | 若同时出现多个 `chrome.exe` 候选，`pick_loudest` 必须选对 |
| **管道断开恢复** | Chrome 退出 / 崩溃时管道的表现**未知**，需确认能重建而非崩溃 |
| **>2 小时 soak** | C4：按进程路径与 OBS 的「应用程序音频捕获」同源，长时劣化风险直接适用 |
| **静音期不卡死（长时）** | 已在短测中观察到正确行为（持续输出零帧），但需在长时运行中复现确认 |

> **实现第一步**：先写一个只做「解析 PID → 建管道 → 打印 RMS」的小脚本，
> 在真实网课上跑通端到端，再往下写。

---

## 8. 依赖

```toml
requires-python = ">=3.13"          # 保持不变（C15）

dependencies = [
  "process-audio-capture>=1.0.0",   # ★ 主路径：按进程捕获 Chrome（C26–C29）
  "pyaudiowpatch>=0.2.12.8",        # 降级路径：系统级 WASAPI loopback
  "soxr",
  "numpy",
  "faster-whisper>=1.2.1",
  "ctranslate2>=4.6",               # 显式 pin，文档化 cp313 下界
  "nvidia-cublas-cu12",             # ★ 不加则首次推理即崩（C14）
  "silero-vad>=6.2",                # 注意：会连带引入 torch / torchaudio
  "fastapi",
  "uvicorn[standard]",
  "websockets",
  "httpx",
]
```

首次运行需下载 Whisper 模型（`large-v3-turbo`，约 1.6 GB）。若 HuggingFace Xet 传输卡在 0 字节，需禁用 Xet。

---

## 9. 风险与未决问题

| 风险 | 影响 | 应对 |
|---|---|---|
| **管道方案依赖库的未文档化行为** | 库升级后管道可能不再被接受，捕获整体失效 | 「把管道名当输出路径」并非公开 API（文档只提 WAV 文件）。必须 **pin 版本**，并在启动时自检（建管道 → 起捕获 → 读到第一块数据），失败即降级 |
| `process-audio-capture` 是闭源 DLL + 单一维护者 | 停更或与新 Windows 不兼容 | 降级路径（§5.1）始终可用；`proc-tap` 作备选实现 |
| **降级后用户误以为隔离仍生效** | 背景音乐 / Edge 混进字幕而不自知 | UI 必须显著提示当前捕获模式，启动时明确播报「按进程」还是「全系统」 |
| PID 解析选错目标 | 录到静音或录到错误的程序 | 启动自检：解析后先测 RMS，持续静音即报警（§7） |
| 长时会话音频劣化在 24H2/25H2 仍未解决 | 2 小时后音频变噪，字幕质量崩 | 定期重建流 + soak test（C4） |
| 免 key 翻译端点在持续负载下约 20% 请求失败 | 字幕缺译文 | 传输错误重试 + Tier 1 官方兜底（C19） |
| **平台在窗口隐藏时自动暂停播放** | 最小化网课窗口后音频停止，字幕中断 | 各平台行为不一，**需实测**；若会暂停则保持窗口可见（可置于其他窗口之后而不最小化） |
| `silero-vad` 连带引入 torch | 依赖体积大 | 若不可接受，vendor `sequence_vad.py` + 去掉 torch 依赖的切点函数（§5.2） |
| `large-v3-turbo` 在静音/噪声上的幻觉 | 出现无中生有的字幕 | 自有 VAD 切句 + `condition_on_previous_text=False` + `compression_ratio` 过滤（C12） |
| 长时间运行内存增长 | 崩溃丢字幕 | JSONL 持续 fsync；soak test 巡检 |
| PotPlayer 等播放器对文件的占用 | SRT 更新失败（WinError 5） | 有界重试 + 备用路径（C23）；实现后在目标机器实测 |
| 中文/英文混讲（俄语课暂不涉及） | 单语模型识别错乱 | Phase B 再处理；当前固定 `language="en"` |

**已解除的风险**：

- **全屏遮挡**（C24）—— 仅真·独占全屏会遮挡，且当前场景为窗口化
- **静音期底层停流**（C1）—— 在按进程 + 管道路径上经实测已不存在；该风险仅残留在降级路径
- **「只录 Chrome、不混入 Edge 与背景音乐」** —— 经实测达成（C26）

---

## 10. 后续（Phase B）

本设计已为以下扩展预留接口，届时**不需要改动引擎**：

1. **加俄语**：翻译层已带 `src`/`tgt`，ASR 层放开 `language` 参数
2. **Chrome 扩展前端**：连接 `server.py` 已有的 WebSocket，字幕叠在网课页面上
3. ~~按进程捕获~~ —— **已提前到 Phase A**：本轮 spike 验证后成为主捕获路径（§5.1）
4. **调用方自定义**：引擎与外壳解耦，可再加 GUI 或其他前端

---

## 附录 A：对抗性核查的修正记录

| 初版说法 | 核查结论 | 修正后 |
|---|---|---|
| 阻塞读能拿到静音帧、回调用不了 | ❌ 证伪，两者都会停流 | C1：应用侧补静音帧，并列为必须实测项 |
| 超 30s 段被静默截断 | ❌ 方向错误，实为硬 `RuntimeError` | C6：仍是必须限长，但必须在 ASR 前限 |
| VAD 报的 end 早 32ms、会削尾音 | ❌ 证伪，实为晚 `speech_pad_ms` | C8 |
| Google/微软官方端点不可达 | ❌ 证伪，Azure 端点实测可达 | C18：Azure F0 作为可靠 Tier 1 |
| 免 key 端点 20/20 成功 | ❌ 低估，实测 47/60 | C19：必须重试传输错误 |
| 任何全屏都会灭掉覆盖层 | ❌ 过于绝对 | C24：仅真·独占全屏 |
| `word_timestamps` 开销 5–10% | ❌ 低估 | C13：10–15%，最差 ~38% |
| Google 领先 DeepL 5–8 BLEU | ❌ 无来源支持 | C21：约 1–2 BLEU，依基准而异 |
| 保持的设计未被证伪 | ✅ | JSONL 权威源、原子重渲、词级时间戳对齐边界、VAD 门控架构 |

---

## 附录 B：捕获方案 spike 的修正记录（2026-09-22）

附录 A 的核查把「系统级 loopback 捕获」当作**既定前提**，未质疑捕获方式本身。
本机 spike 推翻了该前提下的若干隐含假设 —— 这些假设当时已写进正文，故一并修正：

| 初版说法 | 实测结论 | 修正后 |
|---|---|---|
| 按进程捕获是「Phase A 的可选增强」 | ❌ 需求本就要求只录 Chrome，且实测完全可行 | 提为主路径（C29） |
| 抓 `chrome.exe` 即可 | ❌ 进程树不被覆盖，抓父进程得**精确全零** | C26：必须用 `enumerate_audio_processes()` 解析真正在渲染的那个 PID |
| 静音期需要「超时 + 补零」兜底 | ✅ 在按进程路径上并不必要 —— 静音期持续正常输出零帧 | C1 降级为仅适用于降级路径 |
| （初版未考虑）库的音频出口 | ⚠️ 仅有文件输出，且捕获期间文件被**独占** | C28：「tail 增长中的 WAV」实测否决 |
| （初版未考虑）如何拿到流式 PCM | — | C29：命名管道作为输出路径，实测 10 ms 粒度、低延迟 |
| （初版未考虑）Chrome 的输出电平 | ⚠️ 约 **-55 dBFS**，比正常语音低 25 dB | C27：ASR 前必须做**带静音门限**的归一化 |

**本轮 spike 未解决的**：管道在 Chrome 退出 / 崩溃时的行为（§7 列为待测项）。

# 图形界面：程序选择器与置顶悬浮字幕窗 — 设计文档

> **本文件是 `2026-09-22-live-caption-pipeline-design.md` 的补充规格**，只覆盖新增的 GUI
> 子系统。原规格的捕获、切句、ASR、翻译、落盘设计继续有效，此处不重复。
> **约束编号接续原文档**（原文档到 C30），避免出现两个互不相干的 C 空间。

**日期：** 2026-09-24
**状态：** 待评审

---

## 1. 背景与目标

### 问题

Phase A 已经能跑通（终端 + `http://127.0.0.1:8765/`），但**换捕获目标要记命令行参数**：

```powershell
uv run chrometrans --process <进程名>.exe
```

用户想抓的不止 Chrome，还有腾讯会议。而「腾讯会议的可执行文件到底叫什么」正是用户
不该需要知道的事 —— 只有先查出来，才能把这行命令敲对。

更糟的是 `--process` 收的是**进程名**。Chrome 的同名进程有多个（主进程与音频服务子进程
同名，见 C26），所以 `find_target_pid` 只能在同名候选里"逐个试听、挑最响的"去**猜**。
用户在界面上明明能看见是哪个程序在放，程序却在猜。

### 目标

1. 用**下拉框**列出当前正在渲染音频的进程，**选好再启动** —— 像 OBS 的"应用程序音频捕获"。
2. 一个**无边框、置顶、可拖动、可改大小**的悬浮字幕窗，像 Chrome 自带的实时字幕。
3. 终端版与网页版**原样保留**，一行都不删。

---

## 2. 范围

### 范围内

- 启动器窗口：列程序、选程序、开始 / 停止
- 悬浮字幕窗：置顶、拖动、缩放、字号、不透明度
- 系统托盘：显示/隐藏悬浮窗、停止捕获、退出
- 窗口位置 / 大小 / 字号 / 不透明度的持久化
- 引擎新增一条「正在加载模型」状态事件（§5.8）

### 范围外（明确不做）

| 不做 | 原因 |
|---|---|
| **打包 exe** | 原规格 §2 已论证：产物数 GB，且要求使用者有 N 卡 |
| **浏览器扩展** | 扩展没有按进程抓音频的能力，而 Chrome 实时字幕画在原生层、扩展也抓不到。扩展最多只能当显示端，主程序仍须是独立程序 |
| **运行中热切换捕获目标** | 采用 pick-then-start。改选目标 = 停止再启动 |
| **同时捕获多个程序** | 一次只抓一个。开两份也不行 —— `pipe_name` 是写死的，两份会撞在同一条命名管道上（§9） |
| **试听 / 自动挑最响** | v1 不做，理由见 §9 |
| **录制音频文件** | 与原项目目标无关 |

---

## 3. 关键约束

### 3.1 程序选择

| 编号 | 约束 | 依据 |
|---|---|---|
| C31 | **捕获目标只能从 `ProcessAudioCapture.enumerate_audio_processes()` 的结果里选**，绝不能列"有窗口的进程" | C26 已实测：Chrome 渲染音频的进程与显示窗口的进程**不是同一个**，且两者同名。按窗口列会拿到主进程 PID，然后管道照常工作、只是**永远读到静音** —— 这正是原规格 §9 点名的"最隐蔽的失败模式" |
| C32 | 该列表**只含此刻正在渲染音频的进程**。目标没出声时它不在列表里 | 库的语义如此，OBS 行为相同。但必须在界面上说清，否则用户会以为程序坏了 |
| C33 | 用户选中的 PID 若已失效（目标程序重启过），**回退到按进程名重新解析**，不得直接降级 | 不回退的话，腾讯会议重启一次就会耗尽重连次数并降到系统级捕获，把**所有**程序的声音混进来。一次重新解析的代价远小于此 |

### 3.2 线程与界面

| 编号 | 约束 | 依据 |
|---|---|---|
| C34 | **Qt 事件循环独占主线程**；`engine.run()`、模型加载、捕获、翻译一律在引擎线程 | Qt 要求事件循环在主线程；`engine.run()` 是阻塞的，放主线程界面必然卡死 |
| C35 | **跨线程只走 Qt 信号**，不得跨线程直接操作控件 | Qt 控件只在主线程安全 |
| C36 | **业务逻辑不得写在 Qt 类里**：进程枚举、显示标签、PID 回退规则、字幕缓存全部放无 Qt 依赖的模块 | 这个项目最有价值的资产是那 140 个测试。逻辑一旦长进控件里就测不到了，而控件测试又脆又慢 |
| C37 | 悬浮窗**置顶但不得抢焦点** | 置顶窗抢走键盘焦点会让用户没法在别处打字 —— 而用户看网课时正是要在别处打字 |

### 3.3 沿用既有约束

| 编号 | 约束 |
|---|---|
| C25 | 网页仍只绑回环地址。GUI **不得**放宽这一条 |
| C19 | 翻译的传输层错误重试而非降级 —— GUI 不改变翻译链的任何行为 |
| 原规格 §5.1 | 「已降级」必须显式告知。GUI **不得**因为是图形界面就淡化这条提示：降级意味着声音隔离已经失效，这是整个设计里最危险的失败方式 |

---

## 4. 架构

### 数据流

```text
用户在启动器选中 AudioProcess(pid, name)
        ↓
make_resolver(proc)                 ← 钉住 PID，失效则按名字重解析（C33）
        ↓
CaptureSource(resolve_pid=…)        ← 既有注入点，source.py 一行不改
        ↓
Engine.run()                        ← 跑在引擎线程（C34）
        ↓  on_event(dict)
EventRelay.emit_event()             ← 引擎线程调用；顺带写入 Transcript
        ↓  Qt 信号（自动队列连接，跨线程投递，C35）
CaptionWindow / LauncherWindow      ← 主线程
```

### 模块划分

| 文件 | 职责 | Qt 依赖 | 单测 |
|---|---|---|---|
| `gui/processes.py` | 枚举音频进程、生成显示标签、构造 PID 回退解析器 | **无** | 全覆盖 |
| `gui/transcript.py` | 悬浮窗的字幕模型（有上限） | **无** | 全覆盖 |
| `gui/settings.py` | 窗口几何 / 字号 / 不透明度的读写 | **无** | 全覆盖 |
| `gui/relay.py` | 引擎事件 → Qt 信号 | 有（薄） | 冒烟 |
| `gui/caption_window.py` | 悬浮窗 | 有 | 冒烟 |
| `gui/launcher.py` | 启动器窗口 | 有 | 冒烟 |
| `gui/app.py` | 入口 `main()`、托盘、接线、引擎线程 | 有 | —— |
| `pipeline.py` | `segment_source`（从 `cli.py` 迁出，见 §5.9） | 无 | 既有测试迁移 |

C36 的落地方式：**上表前四行（`processes` / `transcript` / `settings` / `pipeline`）不 import
Qt，承载全部逻辑；后四行里，三个窗口/桥接文件只做摆放，`app.py` 只做接线 —— 都不做判断。**

---

## 5. 模块详细设计

### 5.1 `gui/processes.py` — 枚举与标签

```python
@dataclass(frozen=True)
class AudioProcess:
    pid: int
    name: str

    @property
    def label(self) -> str:
        """下拉框里显示的文字。"""
        return f"{self.name} (PID {self.pid})"


def list_audio_processes() -> list[AudioProcess]:
    """枚举当前正在渲染音频的进程（C31）。"""


def pid_alive(pid: int) -> bool:
    """进程是否还活着。原生调 OpenProcess + GetExitCodeProcess。"""


def make_resolver(pinned: AudioProcess, *, alive=pid_alive,
                  find=find_target_pid) -> Callable[[], int]:
    """钉住用户选中的 PID；它没了就按名字重新解析（C33）。"""
```

`list_audio_processes()` **让异常直接抛出去**，不吞。因为"枚举失败"和"没有程序在发声"
是两件事，界面必须给出不同的话：前者说明枚举本身坏了，后者是正常的、可以用"先让网课
开始播放"来引导。吞掉异常会让这两件事长得一模一样。

`make_resolver` 的两个依赖都可注入，因此**回退规则可以完全离线单测**，不需要真的杀进程。

`pid_alive` 用 ctypes 实现（`OpenProcess` + `GetExitCodeProcess`），与 `capture.py` 现有的
ctypes 风格一致，**不引入 psutil**。

### 5.2 `gui/transcript.py` — 字幕模型

```python
class Transcript:
    """悬浮窗的字幕模型。有上限：2 小时的课会累积上千条。"""

    def __init__(self, cap: int = 300) -> None: ...
    def add(self, cue: dict) -> None: ...
    def cues(self) -> list[dict]: ...
```

上限是必须的：一节 2 小时的课可能累积上千条字幕，全部留在控件里就是稳定增长的内存。
300 条足够往回翻很久。

模型与控件分离（C36）意味着**上限与顺序可以离线单测**，不需要起 Qt。

### 5.3 `gui/settings.py` — 持久化

```python
@dataclass
class GuiSettings:
    x: int | None = None
    y: int | None = None
    width: int = 900
    height: int = 220
    font_px: int = 22
    opacity: float = 0.9


def settings_path() -> Path:
    """%APPDATA%/chrometrans/gui.json；取不到 APPDATA 时退回用户主目录。"""


def load(path: Path | None = None) -> GuiSettings: ...
def save(s: GuiSettings, path: Path | None = None) -> None: ...
```

**任何失败都退回默认值，绝不抛。** 配置文件损坏、目录只读、字段类型不对 —— 全都不该
让程序起不来。保存失败同理：界面照常用，只是下次记不住位置。

**恢复窗口位置时必须检查它还在不在当前屏幕上**：用户拔掉副屏后，保存的坐标会落在
一个不存在的显示器上，窗口就再也找不到了。落在屏幕外就居中。

### 5.4 `gui/relay.py` — 事件桥

```python
class EventRelay(QObject):
    cue = Signal(dict)
    status = Signal(dict)
    error = Signal(str)
    finished = Signal()

    def __init__(self, cap: int = 300) -> None: ...
    def emit_event(self, event: dict) -> None:      # 引擎线程调用
    def transcript(self) -> Transcript: ...
```

`emit_event` 先 `transcript.add(cue)` 再发信号 —— 这样悬浮窗被隐藏后重新显示时，
可以直接从模型重建内容，不需要额外的补看机制。

信号由引擎线程发出、在主线程接收，Qt 的自动连接会走队列投递，天然满足 C35。

### 5.5 `gui/caption_window.py` — 悬浮窗

窗口标志：

```python
Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
| Qt.Tool | Qt.WindowDoesNotAcceptFocus
```

- `Tool`：不占任务栏、不进 Alt+Tab —— 它是个叠加层，不是主窗口
- `WindowDoesNotAcceptFocus`：满足 C37

交互：

- **拖动**：`mousePressEvent` 记下按下点与窗口左上角的偏移，`mouseMoveEvent` 里 `move()`
- **缩放**：右下角放一个 `QSizeGrip`
- **内容**：一个只读的富文本视图；英文灰字在上、中文白字在下
- **自动滚动**：默认粘在底部。**用户手动往上滚之后就不再抢回焦点** —— 正在回看前文时
  被新字幕拽到底部是很烦的。滚回底部后恢复自动跟随
- **右键菜单**：字号 ±、不透明度 ±、隐藏窗口
- **关闭按钮**：隐藏窗口（不退出程序），从托盘可以再叫出来

### 5.6 `gui/launcher.py` — 启动器

- 下拉框：列出 `AudioProcess.label`
- **刷新**按钮，以及一个 2 秒的自动刷新定时器（仅在未捕获时运行）
- 空列表时把提示放大：**"没有检测到正在发声的程序 —— 先让网课开始播放，再点刷新"**
  （C32 的落地）
- 枚举失败时显示**另一句**话：`"枚举进程失败：<原因>"`
- 开始 / 停止按钮
- 状态行：`正在加载模型 / 运行中 / 警告 / 已降级 / 已停止`（与原规格 §5.1 的语义一致）
- 一个"同时开启网页"复选框，**默认不勾**

开始后：启动器隐藏到托盘，悬浮窗出现。停止后：反过来。

### 5.7 `gui/app.py` — 入口与生命周期

```python
def main(argv: list[str] | None = None) -> int:
```

- 建 `QApplication`（主线程，C34）
- 建 `EventRelay`，把 `relay.emit_event` 作为引擎的 `on_event`
- 建两个窗口与托盘
- 选好程序点开始：**起一个引擎线程跑 `engine.run()`**
- 停止：`engine.stop()`；等线程收敛（设超时，见 §6）
- 退出：先停引擎，再退 Qt

`app.py` 不引入命令行参数解析的全部复杂度：只保留 `--host` / `--port`（且**沿用 `cli.py`
现有的回环校验**，C25）。详细的 `--process` 等参数仍归 `chrometrans`（终端版）。

### 5.8 引擎新增「正在加载模型」事件

`Engine.run()` 现在直接 `self._asr.load()`，而模型加载要几十秒且**不发任何事件**。
在终端上这表现为一段沉默，可以接受；在 GUI 上则是**窗口看起来卡死了**。

```python
self._emit({"event": "status", "data": {
    "state": "loading", "message": f"正在加载模型 {self._cfg.asr.model}…"}})
self._asr.load()
```

配套要改三处，否则新状态会被静默吞掉：

1. `cli.py` 的 `print_event` —— 它按 `state` 分支，`loading` 会掉进空档
2. `static/index.html` 的 `setStatus` —— 同理
3. 两处的测试

### 5.9 `segment_source` 迁出 `cli.py`

`segment_source` 现在住在 `cli.py`，但 GUI 也要用它 —— 让 `gui/` 去 import `cli.py` 是错的
方向（CLI 是终端前端，不是共享层）。迁到新模块 `chrometrans/pipeline.py`，并加一个可选参数：

```python
def segment_source(cfg, emit, process_names, resolve_pid=None):
```

`resolve_pid` 直接透传给 `CaptureSource`（那个注入点本来就存在）。`cli.py` 不传，行为不变。

---

## 6. 错误处理

| 情况 | 行为 |
|---|---|
| 枚举进程失败 | 启动器显示"枚举进程失败：<原因>"，与"没有程序在发声"**区分开** |
| 列表为空 | 显示引导语（"先让网课开始播放，再点刷新"） |
| 目标 PID 失效 | 按名字重新解析（C33）；名字也找不到才走重连与降级 |
| 降级到系统级捕获 | 状态行**显著**提示，且带原因。不得淡化（原规格 §5.1） |
| 模型加载失败 | 引擎线程捕获异常 → 状态行报错 → 回到未捕获状态，可重试 |
| 停止时引擎线程不收敛 | `stop()` 后等待有上限；超时就**放弃等待并提示**，不让界面永久卡在"正在停止"。引擎线程是守护线程，进程退出不会挂住 |
| 设置文件损坏 | 静默退回默认值 |

---

## 7. 测试策略

沿用已有的注入式做法：**不需要 GPU、不需要音频设备、不需要真实网络。**

**默认跑的（无 Qt）：**

- `tests/test_gui_processes.py` —— 标签格式；枚举成功/失败；**回退规则**：PID 活着就用它、
  死了就按名字重解析、两个都失败要抛 `CaptureError`（C33 的核心，注入假的 `alive`/`find`）
- `tests/test_gui_transcript.py` —— 上限生效、超出后丢最旧的、顺序保持
- `tests/test_gui_settings.py` —— 往返读写、缺文件、文件损坏、字段类型不对、目录只读
- `tests/test_pipeline.py` —— 从 `test_cli.py` 迁过来的既有用例 + 新的 `resolve_pid` 透传

**需要 Qt 的冒烟测试**（加 `gui` marker，默认排除，与现有的 `integration` 同一处理方式）：

- 用 `QT_QPA_PLATFORM=offscreen` 构造两个窗口，喂一条 cue 进去断言文字出现
- 断言悬浮窗的窗口标志包含 `WindowDoesNotAcceptFocus`（C37 的回归）

`pyproject.toml` 的 marker 列表要加 `gui`。

---

## 8. 依赖

新增**一个**：`pyside6`。

不引入 psutil（`pid_alive` 用 ctypes 自写，见 §5.1）。Qt 的体积（约 120 MB）相对于本项目
已在拉的 torch + CUDA 运行库可以忽略。

`pyproject.toml` 增加脚本入口：

```toml
[project.scripts]
chrometrans = "chrometrans.cli:main"
chrometrans-gui = "chrometrans.gui.app:main"
```

---

## 9. 风险与未决问题

| 风险 | 说明与缓解 |
|---|---|
| **`enumerate_audio_processes()` 的耗时** | 实测（本机 11 个音频会话）：稳态单次约 24–28 ms，2 秒轮询（占周期约 1.3%）不卡界面，**无需挪线程**。冷启动首跑约 126 ms，但只出现一次、发生在启动路径（启动器的首次 `refresh()`，此时窗口未显示、`app.exec()` 尚未进入），不在轮询路径上。代价随音频会话数增长，到数十个会话的量级再考虑挪线程 |
| **PySide6 与 torch 的 Qt 插件共存** | 实测（本机 Python 3.13 / Windows 11）：PySide6 6.11.2 / Qt 6.11.2 与 torch 2.14.0+cpu（仅 silero-vad 引入）共存，真实 windows 平台插件正常加载，**无需设置 `QT_PLUGIN_PATH`**，§5.7 不加任何代码 |
| **多显示器拔插** | 保存的坐标可能落在已断开的屏幕上。已写入 §5.3：恢复时校验可见性，否则居中 |
| **两份实例会撞管道名** | `CaptureConfig.pipe_name` 是固定的 `\\.\pipe\chrometrans`。同时开两份会互相干扰。v1 不处理（范围外），但要在 README 里写一句 |
| **悬浮窗盖不住独占全屏** | 置顶窗口能盖住普通全屏，盖不住独占全屏。网课是窗口化的，风险低 |
| **不做"试听挑最响"** | 理论上同名多候选时用户要自己试。实践中 Chrome 通常只有一个进程在渲染音频，且 `find_target_pid` 仍作为**回退路径**保留着挑最响的能力。若实际使用中发现选错频繁，再补 |
| **PID 回退用"名字"而非"可执行文件路径"** | 同名不同程序（罕见）会回退到错的进程。缓解：回退后仍有启动自检（原规格 §9）会因读到静音而报警 |
| **停止时目标不出声会僵住** | 目标程序此刻不出声（如视频暂停）时点「停止」，引擎线程卡在音频读里，`Engine.stop()` 只在语音段边界被检查，界面在 `stop_and_wait` 的 `thread.join(STOP_TIMEOUT_S=10)` 上冻满 10 秒，随后提示「已放弃等待」；期间 `running()` 仍为真，重新开始会被拒绝。这是已知限制，真正的修法要改 `Engine.stop()` / `segment_source` 的契约（给停止一个能打断读的通道），留给下一轮 |

---

## 10. 对既有代码的改动清单

| 文件 | 改动 |
|---|---|
| `pyproject.toml` | 加 `pyside6`；加 `chrometrans-gui` 入口；加 `gui` marker |
| `engine.py` | `asr.load()` 前发 `loading` 状态（§5.8） |
| `cli.py` | `print_event` 处理 `loading`；`segment_source` 改为从 `pipeline.py` 导入 |
| `static/index.html` | `setStatus` 处理 `loading` |
| `pipeline.py` | **新建**，`segment_source` 迁入并加 `resolve_pid` 参数（§5.9） |
| `tests/test_cli.py` | `segment_source` 的用例迁往 `tests/test_pipeline.py` |
| `README.md` | 加 GUI 用法、管道名冲突的提醒 |

**`audio/source.py`、`audio/capture.py`、`translate/`、`output/` 一行不改。**

---

## 附录 A：为什么不用 tkinter

tkinter 是标准库、零依赖，置顶（`-topmost`）、无边框（`overrideredirect`）、拖动也都能
拼出来。但"置顶 + 无边框 + 可缩放"三者同时成立需要手工处理尺寸柄与命中测试，高 DPI
要自己调 `SetProcessDpiAwareness`，中文字体渲染也发虚。这个项目已经在拉几 GB 的
torch + CUDA，为了省 120 MB 去手工拼这些不划算。

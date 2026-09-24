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

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
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
    （C41 硬要求为 0），就必须在网格里找能救回来的候选，哪怕没有负样本。
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
    tied = sum(1 for item in survivors if item[0] == chosen_c.false_pass)
    if tied == 1:
        note = "在误杀 = 0 的候选里漏放最少；平手时取离基准最近的。"
    else:
        # 全平手：漏放最少的候选不止一组，选谁只看离基准多远 —— 那不是测出来的
        # 优势，别把它读成「过滤有效」。为什么一组都拦不住，实测原因在 config.py
        # 里 LANGUAGES 上方那段（三条过滤轴在这套栈上不可达）。
        note = (
            f"误杀 = 0 的候选里，漏放最少的不止一组：{tied} 组并列漏放 "
            f"{chosen_c.false_pass} 条，取离基准最近的只是平手时图省事，"
            "不是测出来的优势——这些数字在这份素材上分不出高下。"
            "为什么见 config.py 里 LANGUAGES 上方那段实测结论"
            "（三条过滤轴在这套栈上不可达）。")
    return Recommendation(baseline, chosen, base, chosen_c, note)


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

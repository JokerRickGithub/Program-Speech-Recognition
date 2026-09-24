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

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
    assert c.false_pass_rate == 0.0

    c = Confusion(true_speech=5, false_drop=0, false_pass=0, true_nonspeech=0)
    assert c.negatives == 0
    assert c.false_drop_rate == 0.0
    assert c.false_pass_rate is None, "没有负样本时漏放率是「测不出来」，不是 0"


def test_recommend_keeps_the_baseline_when_there_are_no_negatives():
    """C41：负样本为空时漏放无法度量，凭正样本单侧收紧是在看不见的那一侧下注。"""
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


def test_recommend_discloses_when_every_candidate_ties():
    """全网格平手时，结论照实说：选基准是图省事，不是测出来的优势。

    zh 那 1 条 initial_prompt 回声就是活例子 —— 全网格 168 组都漏放那 1 条、
    没一组真的拦得住。结论若只说「漏放最少」，读者会读出并不存在的优势。
    """
    rows = [
        _row("真语音", 0.05, -0.2, 1.2),
        _row("拦不住的回声", 0.0, -0.8, 0.7, NONSPEECH),
    ]

    rec = recommend(rows)

    assert rec.chosen_confusion.false_drop == 0
    assert rec.chosen_confusion.false_pass == 1
    assert "分不出高下" in rec.note
    assert "168 组并列漏放 1 条" in rec.note
    assert "图省事" in rec.note


def test_recommend_keeps_old_wording_when_the_grid_discriminates():
    """网格真能分高下时，结论仍用原措辞 —— 只有全平手才需要补充说明。

    两条负样本各只能被一组阈值拦下，且只有同一组（nsp=0.3 / alp=-0.6 / cr=2.0）
    两条都拦得下：它的漏放严格低于其余所有候选，网格确实分出了高下。
    """
    rows = [
        _row("真语音", 0.05, -0.2, 1.2),
        _row("只有 cr=2.0 拦得住", 0.0, -0.1, 2.2, NONSPEECH),
        _row("只有 nsp=0.3/alp=-0.6 拦得住", 0.35, -0.7, 0.5, NONSPEECH),
    ]

    rec = recommend(rows)

    assert rec.chosen_confusion.false_drop == 0
    assert rec.chosen_confusion.false_pass == 0, "这组阈值两条负样本都拦得下"
    assert rec.note == "在误杀 = 0 的候选里漏放最少；平手时取离基准最近的。"


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
    from chrometrans.audio.segmenter import Segment
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

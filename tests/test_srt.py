from chrometrans.models import Cue
from chrometrans.output.srt import format_timestamp, render


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


def test_render_never_splits_a_cue_on_messy_text():
    """C22 的真正风险：ASR 文本自带换行、或整段为空白时，cue 内出现空行 → 整个文件错位。

    注意断言方式：不能用 `split("\\n\\n")` 逐块检查 —— 一旦真的错位，切出来的每块
    仍然没有空行，那样写在错误的实现下也会通过。必须按「块数 == cue 数」来断言。
    """
    cues = [_cue(1, 0.0, 1.0, src="hello\n\nworld", tgt="   "),
            _cue(2, 1.0, 2.0, src="", tgt=None)]
    text = render(cues)
    blocks = [b for b in text.split("\n\n") if b.strip()]
    assert len(blocks) == len(cues)
    for block, cue in zip(blocks, cues):
        assert block.splitlines()[0] == str(cue.id)


def test_render_omits_missing_translation():
    text = render([_cue(1, 0.0, 1.0, tgt=None)])
    assert text == ("1\n00:00:00,000 --> 00:00:01,000\nhello\n\n")


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

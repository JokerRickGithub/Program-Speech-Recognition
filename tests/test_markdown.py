from chrometrans.models import Cue
from chrometrans.output.markdown import format_clock, render


def _cue(i, start, end, src="hello", tgt="你好"):
    return Cue(id=i, start=start, end=end, source=src, target=tgt,
               src_lang="en", tgt_lang="zh-Hans")


def test_format_clock_rounds_to_whole_seconds():
    """复习用不到毫秒，HH:MM:SS 就够了。"""
    assert format_clock(3.12) == "00:00:03"
    assert format_clock(3661.5) == "01:01:02"
    assert format_clock(0.0) == "00:00:00"


def test_header_states_languages_count_and_span():
    text = render([_cue(1, 3.0, 6.0), _cue(2, 9.0, 12.0)])
    assert text.startswith("# 网课字幕\n")
    assert "en → zh-Hans" in text
    assert "2 条" in text
    assert "00:00:03 → 00:00:12" in text


def test_source_line_carries_the_timestamp_and_target_follows():
    """原文在上、译文在下 —— 与 SRT 和屏幕上的顺序保持一致。"""
    text = render([_cue(1, 0.0, 1.0, "The derivative is continuous",
                        "这个导数是连续的")])
    assert "**00:00:00** The derivative is continuous" in text
    assert text.index("The derivative is continuous") < text.index("这个导数是连续的")


def test_missing_translation_is_omitted_not_invented():
    """翻译整链失败时 target 是 None，不能编一句出来，也不能留个空行。"""
    text = render([_cue(1, 0.0, 1.0, tgt=None)])
    body = text.split("---\n", 1)[1].strip()
    assert body == "**00:00:00** hello"


def test_blank_line_inside_asr_text_does_not_fork_a_cue():
    """ASR 文本自带空行时不能被原样保留。

    Markdown 里空行是段落分隔符：留着它，一条字幕就被劈成两块，
    上半截没有时间戳 —— 复习时对不上视频进度。
    """
    text = render([_cue(1, 0.0, 1.0, src="hello\n\nworld", tgt=None)])
    assert text.count("**00:00:00**") == 1
    assert "**00:00:00** hello world" in text


def test_whitespace_only_source_keeps_the_timestamp_as_an_anchor():
    """原文是空白（ASR 偶尔会这样）时留个光秃秃的时间戳，也不能把译文吞掉。

    时间戳是复习时对视频进度的锚点，丢了就再也对不上；译文更是唯一有信息量的东西。
    """
    text = render([_cue(1, 0.0, 1.0, src="   ", tgt="你好")])
    body = text.split("---\n", 1)[1].strip()
    assert body == "**00:00:00**\n\n你好"


def test_empty_cue_list_renders_empty():
    """空会话不该产出一个只有标题的孤儿文件。"""
    assert render([]) == ""


def test_cue_order_is_preserved():
    text = render([_cue(1, 5.0, 6.0, src="first"), _cue(2, 1.0, 2.0, src="second")])
    body = text.split("---\n", 1)[1]
    assert body.index("first") < body.index("second"), "按写入顺序排，不按时间重排"

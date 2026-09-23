import pytest


def _cue(i):
    return {"id": i, "start": float(i), "end": float(i) + 1,
            "source": f"s{i}", "target": f"t{i}",
            "src_lang": "en", "tgt_lang": "zh-Hans"}


def test_cues_come_back_in_the_order_they_were_added():
    from chrometrans.gui.transcript import Transcript

    t = Transcript()
    t.add(_cue(1))
    t.add(_cue(2))

    assert [c["id"] for c in t.cues()] == [1, 2]


def test_oldest_cues_are_dropped_once_the_cap_is_reached():
    """一节 2 小时的课能累积上千条，全留着就是稳定增长的内存。"""
    from chrometrans.gui.transcript import Transcript

    t = Transcript(cap=3)
    for i in range(1, 6):
        t.add(_cue(i))

    assert [c["id"] for c in t.cues()] == [3, 4, 5]


def test_length_never_exceeds_the_cap():
    from chrometrans.gui.transcript import Transcript

    t = Transcript(cap=2)
    for i in range(10):
        t.add(_cue(i))

    assert len(t) == 2


def test_empty_transcript_returns_an_empty_list():
    from chrometrans.gui.transcript import Transcript

    assert Transcript().cues() == []


def test_cues_returns_a_copy_so_callers_cannot_mutate_the_model():
    from chrometrans.gui.transcript import Transcript

    t = Transcript()
    t.add(_cue(1))
    t.cues().clear()

    assert len(t) == 1


def test_a_non_positive_cap_is_rejected_loudly():
    """cap=0 会让每条字幕都立刻消失，静默接受只会让人对着空窗口发懵。"""
    from chrometrans.gui.transcript import Transcript

    with pytest.raises(ValueError):
        Transcript(cap=0)


def test_clear_empties_the_model():
    """重新开始捕获时要能从零开始。"""
    from chrometrans.gui.transcript import Transcript

    t = Transcript()
    t.add(_cue(1))
    t.add(_cue(2))
    t.clear()

    assert len(t) == 0
    assert t.cues() == []


def test_add_reports_when_it_dropped_the_oldest():
    """控件要靠这个返回值决定要不要跟着裁自己那份 —— 只裁模型等于没裁。"""
    from chrometrans.gui.transcript import Transcript

    t = Transcript(cap=2)
    assert t.add(_cue(1)) is False
    assert t.add(_cue(2)) is False
    assert t.add(_cue(3)) is True

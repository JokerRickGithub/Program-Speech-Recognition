from chrometrans.models import Cue


def test_cue_roundtrip():
    cue = Cue(id=1, start=3.12, end=6.48,
              source="The derivative is continuous",
              target="这个导数是连续的",
              src_lang="en", tgt_lang="zh-Hans")
    assert Cue.from_dict(cue.to_dict()) == cue


def test_cue_allows_null_target():
    """翻译整链失败时 target 为 None，原文照常保存（spec §6）。"""
    cue = Cue(id=2, start=0.0, end=1.0, source="hello", target=None,
              src_lang="en", tgt_lang="zh-Hans")
    assert Cue.from_dict(cue.to_dict()).target is None


def test_is_monolingual_reads_the_target_language():
    """C44 的判据：tgt_lang 为 None 是「本会话不翻译」，
    tgt_lang 有值而 target 为空是「翻译失败」。两者必须分得开。"""
    from chrometrans.models import Cue, is_monolingual

    mono = Cue(id=1, start=0.0, end=1.0, source="你好", target=None,
               src_lang="zh", tgt_lang=None)
    failed = Cue(id=2, start=0.0, end=1.0, source="hi", target=None,
                 src_lang="en", tgt_lang="zh-Hans")

    assert is_monolingual(mono) is True
    assert is_monolingual(failed) is False


def test_is_monolingual_accepts_the_dict_form():
    """事件、JSONL、GUI 三处流动的都是 dict —— 判据必须认得它。"""
    from chrometrans.models import is_monolingual

    assert is_monolingual({"source": "你好", "target": None,
                           "tgt_lang": None}) is True
    assert is_monolingual({"source": "hi", "target": "你好",
                           "tgt_lang": "zh-Hans"}) is False


def test_a_missing_key_is_not_monolingual():
    """键缺失说明喂进来的东西有问题，不能当成「本会话不翻译」。

    这条判据是有方向的：把畸形输入当成单语，会把一条其实存在的译文悄悄吞掉，
    而屏幕上什么异常都看不出来。退回双语渲染（显示「（未翻译）」）至少是可见的。
    既有的 test_gui_caption_window.py 里手工构造的 cue dict 正是这个形状，
    它们必须照旧通过。
    """
    from chrometrans.models import is_monolingual

    assert is_monolingual({"source": "hello", "target": None}) is False
    assert is_monolingual({"source": "hello", "target": "你好"}) is False

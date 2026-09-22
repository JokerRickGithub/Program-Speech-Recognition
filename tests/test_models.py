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

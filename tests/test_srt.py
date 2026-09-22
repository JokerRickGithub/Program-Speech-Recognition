import os

import pytest

from chrometrans.models import Cue
from chrometrans.output.srt import atomic_write, format_timestamp, render


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


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "captions.srt"
    assert atomic_write(target, "content", retries=3, base_delay=0.0) is True
    assert target.read_text(encoding="utf-8") == "content"


def test_atomic_write_retries_on_permission_error(tmp_path, monkeypatch):
    """C23：播放器占用文件时 os.replace 抛 WinError 5，必须重试。"""
    import chrometrans.output.srt as srt_mod

    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "拒绝访问")
        return real_replace(src, dst)

    monkeypatch.setattr(srt_mod.os, "replace", flaky)
    target = tmp_path / "captions.srt"
    assert atomic_write(target, "content", retries=5, base_delay=0.0) is True
    assert calls["n"] == 3
    assert target.read_text(encoding="utf-8") == "content"


def test_atomic_write_gives_up_after_retries(tmp_path, monkeypatch):
    import chrometrans.output.srt as srt_mod

    def always_fail(src, dst):
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(srt_mod.os, "replace", always_fail)
    target = tmp_path / "captions.srt"
    assert atomic_write(target, "content", retries=2, base_delay=0.0) is False
    assert not target.exists()


def test_fallback_writes_next_file_when_target_is_locked(tmp_path, monkeypatch):
    """spec §5.5：主目标写不进去时改投 captions.next.srt，不阻塞流水线。"""
    import chrometrans.output.srt as srt_mod

    target = tmp_path / "captions.srt"
    real_replace = os.replace

    def only_target_fails(src, dst):
        if str(dst).endswith("captions.srt"):
            raise PermissionError(5, "拒绝访问")
        return real_replace(src, dst)

    monkeypatch.setattr(srt_mod.os, "replace", only_target_fails)
    written = srt_mod.write_with_fallback(target, "content",
                                          retries=1, base_delay=0.0)

    assert written == tmp_path / "captions.next.srt"
    assert written.read_text(encoding="utf-8") == "content"


def test_fallback_prefers_target_when_free(tmp_path):
    import chrometrans.output.srt as srt_mod

    target = tmp_path / "captions.srt"
    assert srt_mod.write_with_fallback(target, "content",
                                       retries=1, base_delay=0.0) == target
    assert not (tmp_path / "captions.next.srt").exists()


def test_fallback_returns_none_when_both_paths_fail(tmp_path, monkeypatch):
    import chrometrans.output.srt as srt_mod

    def always_fail(src, dst):
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(srt_mod.os, "replace", always_fail)
    assert srt_mod.write_with_fallback(tmp_path / "captions.srt", "content",
                                       retries=1, base_delay=0.0) is None

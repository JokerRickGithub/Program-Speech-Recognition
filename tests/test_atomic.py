import os
from pathlib import Path

from chrometrans.output.atomic import atomic_write, fallback_path, write_with_fallback


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "captions.srt"
    assert atomic_write(target, "content", retries=3, base_delay=0.0) is True
    assert target.read_text(encoding="utf-8") == "content"


def test_atomic_write_retries_on_permission_error(tmp_path, monkeypatch):
    """C23：播放器占用文件时 os.replace 抛 WinError 5，必须重试。"""
    import chrometrans.output.atomic as atomic_mod

    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "拒绝访问")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_mod.os, "replace", flaky)
    target = tmp_path / "captions.srt"
    assert atomic_write(target, "content", retries=5, base_delay=0.0) is True
    assert calls["n"] == 3
    assert target.read_text(encoding="utf-8") == "content"


def test_atomic_write_gives_up_after_retries(tmp_path, monkeypatch):
    import chrometrans.output.atomic as atomic_mod

    def always_fail(src, dst):
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(atomic_mod.os, "replace", always_fail)
    target = tmp_path / "captions.srt"
    assert atomic_write(target, "content", retries=2, base_delay=0.0) is False
    assert not target.exists()


def test_fallback_name_follows_the_suffix():
    """改投名从后缀推出来。

    写死 `.srt` 的话，Markdown 视图被占用时会去覆盖 SRT 的退路文件，
    两个视图抢同一个名字。
    """
    assert fallback_path(Path("captions.srt")).name == "captions.next.srt"
    assert fallback_path(Path("captions.md")).name == "captions.next.md"


def test_fallback_writes_next_file_when_target_is_locked(tmp_path, monkeypatch):
    """spec §5.5：主目标写不进去时改投 captions.next.srt，不阻塞流水线。"""
    import chrometrans.output.atomic as atomic_mod

    target = tmp_path / "captions.srt"
    real_replace = os.replace

    def only_target_fails(src, dst):
        if str(dst).endswith("captions.srt"):
            raise PermissionError(5, "拒绝访问")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_mod.os, "replace", only_target_fails)
    written = write_with_fallback(target, "content", retries=1, base_delay=0.0)

    assert written == tmp_path / "captions.next.srt"
    assert written.read_text(encoding="utf-8") == "content"


def test_fallback_writes_next_markdown_when_target_is_locked(tmp_path, monkeypatch):
    """Markdown 视图走同一套退路，但落到自己的名字上。"""
    import chrometrans.output.atomic as atomic_mod

    target = tmp_path / "captions.md"
    real_replace = os.replace

    def only_target_fails(src, dst):
        if str(dst).endswith("captions.md"):
            raise PermissionError(5, "拒绝访问")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_mod.os, "replace", only_target_fails)
    written = write_with_fallback(target, "content", retries=1, base_delay=0.0)

    assert written == tmp_path / "captions.next.md"
    assert not (tmp_path / "captions.next.srt").exists(), "不能占用 SRT 的退路名字"


def test_fallback_prefers_target_when_free(tmp_path):
    assert write_with_fallback(tmp_path / "captions.srt", "content",
                               retries=1, base_delay=0.0) == tmp_path / "captions.srt"
    assert not (tmp_path / "captions.next.srt").exists()


def test_fallback_returns_none_when_both_paths_fail(tmp_path, monkeypatch):
    import chrometrans.output.atomic as atomic_mod

    def always_fail(src, dst):
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(atomic_mod.os, "replace", always_fail)
    assert write_with_fallback(tmp_path / "captions.srt", "content",
                               retries=1, base_delay=0.0) is None

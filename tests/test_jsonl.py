from datetime import datetime

from chrometrans.models import Cue
from chrometrans.output.jsonl import JsonlWriter, session_dir


def _cue(i):
    return Cue(id=i, start=float(i), end=float(i) + 1.5,
               source=f"line {i}", target=f"第 {i} 行",
               src_lang="en", tgt_lang="zh-Hans")


def test_session_dir_is_timestamped(tmp_path):
    d = session_dir(tmp_path, datetime(2026, 9, 22, 14, 20))
    assert d == tmp_path / "2026-09-22_1420"
    assert d.is_dir()


def test_append_and_read_back(tmp_path):
    path = tmp_path / "captions.jsonl"
    w = JsonlWriter(path)
    w.open()
    for i in (1, 2, 3):
        w.append(_cue(i))
    w.close()

    assert JsonlWriter(path).read_all() == [_cue(1), _cue(2), _cue(3)]


def test_survives_crash_without_close(tmp_path):
    """不调 close() 也必须已落盘 —— 每行 append + flush + fsync。"""
    path = tmp_path / "captions.jsonl"
    w = JsonlWriter(path)
    w.open()
    w.append(_cue(1))
    w.append(_cue(2))
    # 故意不 close()

    assert [c.id for c in JsonlWriter(path).read_all()] == [1, 2]


def test_null_target_roundtrips(tmp_path):
    path = tmp_path / "captions.jsonl"
    w = JsonlWriter(path)
    w.open()
    w.append(Cue(id=1, start=0.0, end=1.0, source="hi", target=None,
                 src_lang="en", tgt_lang="zh-Hans"))
    w.close()
    assert JsonlWriter(path).read_all()[0].target is None


def test_unicode_is_not_escaped(tmp_path):
    path = tmp_path / "captions.jsonl"
    w = JsonlWriter(path)
    w.open()
    w.append(_cue(1))
    w.close()
    assert "第 1 行" in path.read_text(encoding="utf-8")

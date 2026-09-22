from datetime import datetime

import numpy as np

from chrometrans.audio.segmenter import Segment
from chrometrans.config import Config, OutputConfig, TranslateConfig
from chrometrans.engine import Engine


class StubTranslator:
    name = "stub"

    def __init__(self, fail=False):
        self.fail = fail

    async def translate(self, texts, src, tgt):
        if self.fail:
            raise RuntimeError("boom")
        return [f"译:{t}" for t in texts]


def _engine(tmp_path, translator, segments, utterance="hello world"):
    events = []

    class StubAsr:
        def load(self): pass
        def transcribe(self, seg):
            from chrometrans.asr.whisper_engine import Utterance
            return Utterance(start=seg.start, end=seg.end, text=utterance)

    eng = Engine(
        cfg=Config(output=OutputConfig(output_root=tmp_path),
                   translate=TranslateConfig()),
        on_event=events.append,
        asr=StubAsr(),
        translator=translator,
        segments=segments,
    )
    return eng, events


def test_emits_cue_and_writes_both_formats(tmp_path):
    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    eng, events = _engine(tmp_path, StubTranslator(), segs)

    eng.run()

    cues = [e for e in events if e["event"] == "cue"]
    assert len(cues) == 1
    assert cues[0]["data"]["source"] == "hello world"
    assert cues[0]["data"]["target"] == "译:hello world"

    session = next(tmp_path.iterdir())
    assert (session / "captions.jsonl").exists()
    assert (session / "captions.srt").exists()
    assert "译:hello world" in (session / "captions.srt").read_text(encoding="utf-8")


def test_translation_failure_keeps_source(tmp_path):
    """spec §6：翻译整链失败时 target 置 null，原文照常保存。"""
    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    eng, events = _engine(tmp_path, StubTranslator(fail=True), segs)

    eng.run()

    cue = [e for e in events if e["event"] == "cue"][0]["data"]
    assert cue["target"] is None
    assert cue["source"] == "hello world"
    assert any(e["event"] == "error" for e in events)


def test_asr_failure_does_not_stop_pipeline(tmp_path):
    """单段 ASR 失败必须跳过并继续（spec §6）。"""
    segs = [Segment(i, float(i), float(i) + 1, np.zeros(16000, dtype=np.float32))
            for i in range(3)]

    class HalfBrokenAsr:
        def __init__(self): self.n = 0
        def load(self): pass
        def transcribe(self, seg):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("cuda oops")
            from chrometrans.asr.whisper_engine import Utterance
            return Utterance(start=seg.start, end=seg.end, text=f"line {self.n}")

    events = []
    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=events.append, asr=HalfBrokenAsr(),
                 translator=StubTranslator(), segments=segs)
    eng.run()

    cues = [e for e in events if e["event"] == "cue"]
    assert len(cues) == 2, "第 2 段失败不应影响第 1、3 段"
    assert any(e["event"] == "error" for e in events)


def test_emits_status_event(tmp_path):
    segs = []
    eng, events = _engine(tmp_path, StubTranslator(), segs)
    eng.run()
    assert any(e["event"] == "status" for e in events)


def test_srt_written_even_for_a_short_session(tmp_path):
    """默认每 20 条才重渲一次；收尾必须补渲，否则短会话根本没有 SRT。"""
    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    eng, _ = _engine(tmp_path, StubTranslator(), segs)
    eng.run()

    session = next(tmp_path.iterdir())
    srt = session / "captions.srt"
    assert srt.exists()
    assert srt.read_text(encoding="utf-8").startswith("1\n")


def test_srt_can_be_rerendered_from_jsonl_alone(tmp_path):
    """spec §7：崩溃后仅凭 JSONL 就能重渲出等价 SRT。"""
    from chrometrans.output.jsonl import JsonlWriter
    from chrometrans.output.srt import render

    segs = [Segment(i, float(i), float(i) + 1, np.zeros(16000, dtype=np.float32))
            for i in range(3)]
    eng, _ = _engine(tmp_path, StubTranslator(), segs)
    eng.run()

    session = next(tmp_path.iterdir())
    assert (session / "captions.srt").read_text(encoding="utf-8") == render(
        JsonlWriter(session / "captions.jsonl").read_all())


def test_restart_in_same_minute_does_not_duplicate_cue_ids(tmp_path, monkeypatch):
    """同分钟内重启：目录复用、JSONL 追加，cue_id 必须续号（R21）。"""
    import chrometrans.engine as engine_mod

    class FrozenDatetime:
        @classmethod
        def now(cls):
            return datetime(2026, 9, 23, 10, 30, 0)

    monkeypatch.setattr(engine_mod, "datetime", FrozenDatetime)

    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    _engine(tmp_path, StubTranslator(), segs)[0].run()
    _engine(tmp_path, StubTranslator(), segs)[0].run()

    assert len(list(tmp_path.iterdir())) == 1, "两次会话应落在同一个分钟目录"

    from chrometrans.output.jsonl import JsonlWriter

    session = next(tmp_path.iterdir())
    cues = JsonlWriter(session / "captions.jsonl").read_all()
    assert [c.id for c in cues] == [1, 2]


def test_render_failure_does_not_escape(tmp_path):
    """重渲抛异常时不得逃出 _render_srt（R22）。

    它跑在 run() 的 finally 里：异常逃出去会吞掉 stopped 事件、让捕获进程崩掉。
    一行 start=null 的 JSONL 就能构造出这种输入 —— read_all 只吞
    ValueError/TypeError，而 Cue(**d) 键齐全时构造是成功的，炸点在 render()。
    """
    from chrometrans.output.jsonl import JsonlWriter

    session = tmp_path / "2026-09-23_1030"
    session.mkdir()
    jsonl = session / "captions.jsonl"
    jsonl.write_text(
        '{"id": 1, "start": null, "end": 2.0, "source": "x", '
        '"target": null, "src_lang": "en", "tgt_lang": "zh"}\n',
        encoding="utf-8")

    events = []
    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=events.append, asr=None, translator=None, segments=[])
    eng._render_srt(session, JsonlWriter(jsonl))     # 不得抛

    assert any(e["event"] == "error" for e in events)


def test_writer_close_failure_does_not_kill_the_teardown(tmp_path, monkeypatch):
    """close() 抛异常也必须走到 stopped（R22 的保护不能被它上一行废掉）。"""
    from chrometrans.output.jsonl import JsonlWriter

    def boom(self):
        raise OSError("磁盘满了")

    monkeypatch.setattr(JsonlWriter, "close", boom)

    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    eng, events = _engine(tmp_path, StubTranslator(), segs)
    eng.run()          # 不得抛

    states = [e["data"].get("state") for e in events]
    assert "stopped" in states, "close() 失败也必须发 stopped 事件"
    session = next(tmp_path.iterdir())
    assert (session / "captions.srt").exists(), "close() 失败也必须照常重渲 SRT"

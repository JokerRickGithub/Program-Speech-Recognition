from datetime import datetime

import numpy as np

from chrometrans.audio.segmenter import Segment
from chrometrans.config import AsrConfig, Config, OutputConfig, TranslateConfig
from chrometrans.engine import Engine


class StubTranslator:
    name = "stub"

    def __init__(self, fail=False):
        self.fail = fail

    async def translate(self, texts, src, tgt):
        if self.fail:
            raise RuntimeError("boom")
        return [f"译:{t}" for t in texts]


def _stub_asr(utterance: str):
    from chrometrans.asr.whisper_engine import TranscribeResult, Utterance

    class StubAsr:
        def load(self): pass

        def transcribe(self, seg):
            return TranscribeResult(
                utterance=Utterance(start=seg.start, end=seg.end, text=utterance))

    return StubAsr()


def _engine(tmp_path, translator, segments, utterance="hello world"):
    events = []

    eng = Engine(
        cfg=Config(output=OutputConfig(output_root=tmp_path),
                   translate=TranslateConfig()),
        on_event=events.append,
        asr=_stub_asr(utterance),
        translator=translator,
        segments=segments,
    )
    return eng, events


def test_emits_cue_and_writes_all_three_artifacts(tmp_path):
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
    assert (session / "captions.md").exists()
    assert "译:hello world" in (session / "captions.srt").read_text(encoding="utf-8")
    assert "译:hello world" in (session / "captions.md").read_text(encoding="utf-8")


def test_translation_failure_keeps_source(tmp_path):
    """spec §6：翻译整链失败时 target 置 null，原文照常保存。

    「保留原文」是行为，报不报是另一回事：用户报的「英语课很多话直接没有翻译」
    （2026-09-25）在此之前终端上一个字都没有 —— 有没有译文看得见，为什么没有
    看不见。所以这里连带断言原因也在事件里。
    """
    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    eng, events = _engine(tmp_path, StubTranslator(fail=True), segs)

    eng.run()

    cue = [e for e in events if e["event"] == "cue"][0]["data"]
    assert cue["target"] is None
    assert cue["source"] == "hello world"

    errors = [e["data"]["message"] for e in events if e["event"] == "error"]
    assert errors, "翻译失败必须报出来"
    assert any("翻译失败" in m for m in errors)
    assert any("boom" in m for m in errors), "要带上原因，否则用户不知道该查什么"


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
            from chrometrans.asr.whisper_engine import TranscribeResult, Utterance
            return TranscribeResult(
                utterance=Utterance(start=seg.start, end=seg.end,
                                    text=f"line {self.n}"))

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


def test_markdown_can_be_rerendered_from_jsonl_alone(tmp_path):
    """课后补渲靠的就是这条：写 md 渲染器只是为了复习，历史会话不能重录一遍。"""
    from chrometrans.output.jsonl import JsonlWriter
    from chrometrans.output.markdown import render

    segs = [Segment(i, float(i), float(i) + 1, np.zeros(16000, dtype=np.float32))
            for i in range(3)]
    eng, _ = _engine(tmp_path, StubTranslator(), segs)
    eng.run()

    session = next(tmp_path.iterdir())
    assert (session / "captions.md").read_text(encoding="utf-8") == render(
        JsonlWriter(session / "captions.jsonl").read_all())


def test_markdown_written_even_for_a_short_session(tmp_path):
    """默认每 20 条才重渲一次；收尾必须补渲，否则短会话根本没有 Markdown。"""
    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    eng, _ = _engine(tmp_path, StubTranslator(), segs)
    eng.run()

    session = next(tmp_path.iterdir())
    md = session / "captions.md"
    assert md.exists()
    assert md.read_text(encoding="utf-8").startswith("# 网课字幕\n")


def _broken_cues_session(tmp_path):
    """造一份 render 会炸的 JSONL：start 是 null，format_* 里做算术就抛。"""
    from chrometrans.output.jsonl import JsonlWriter

    session = tmp_path / "2026-09-23_1030"
    session.mkdir()
    jsonl = session / "captions.jsonl"
    jsonl.write_text(
        '{"id": 1, "start": null, "end": 2.0, "source": "x", '
        '"target": null, "src_lang": "en", "tgt_lang": "zh"}\n',
        encoding="utf-8")
    return session, JsonlWriter(jsonl)


def test_render_failure_does_not_escape(tmp_path):
    """重渲抛异常时不得逃出 _render_srt（R22）。

    它跑在 run() 的 finally 里：异常逃出去会吞掉 stopped 事件、让捕获进程崩掉。
    一行 start=null 的 JSONL 就能构造出这种输入 —— read_all 只吞
    ValueError/TypeError，而 Cue(**d) 键齐全时构造是成功的，炸点在 render()。
    """
    session, writer = _broken_cues_session(tmp_path)

    events = []
    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=events.append, asr=None, translator=None, segments=[])
    eng._render_srt(session, writer)     # 不得抛

    assert any(e["event"] == "error" for e in events)


def test_both_views_report_and_neither_escapes_when_both_fail(tmp_path):
    """两个视图各自兜错：同一次重渲里两份都炸，也要各报各的、都不逃出去。"""
    session, writer = _broken_cues_session(tmp_path)

    events = []
    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=events.append, asr=None, translator=None, segments=[])
    eng._render_views(session, writer)   # 不得抛

    messages = [e["data"]["message"] for e in events if e["event"] == "error"]
    assert any("captions.srt" in m for m in messages), "SRT 的失败要单独报"
    assert any("captions.md" in m for m in messages), "Markdown 的失败要单独报"


def test_one_broken_view_does_not_block_the_other(tmp_path, monkeypatch):
    """Markdown 渲不出来时 SRT 仍须写出，且报错要指名道姓。

    后半句才是这条测试真正能分辨的东西：SRT 排在前面，所以"md 崩了 srt 还在"
    这件事连把两个视图塞进同一个 try 的写法也能满足。而"哪个视图坏了"必须说
    清楚 —— 共用一个 try 时只会抛一句笼统的「重渲失败」，你无从知道丢的是
    播放器用的 srt 还是复习用的 md。
    """
    import chrometrans.engine as engine_mod
    from chrometrans.output.jsonl import JsonlWriter

    session = tmp_path / "2026-09-23_1030"
    session.mkdir()
    (session / "captions.jsonl").write_text(
        '{"id": 1, "start": 0.0, "end": 1.0, "source": "x", '
        '"target": "y", "src_lang": "en", "tgt_lang": "zh"}\n',
        encoding="utf-8")

    def boom(cues):
        raise RuntimeError("markdown 炸了")

    monkeypatch.setattr(engine_mod, "render_markdown", boom)

    events = []
    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=events.append, asr=None, translator=None, segments=[])
    eng._render_views(session, JsonlWriter(session / "captions.jsonl"))

    assert (session / "captions.srt").exists(), "markdown 失败不得连累 SRT"
    assert not (session / "captions.md").exists()

    messages = [e["data"]["message"] for e in events if e["event"] == "error"]
    assert any("captions.md" in m for m in messages), "报错必须指明是哪个视图坏了"
    assert not any("captions.srt" in m for m in messages), "SRT 这份是好的，不该被牵连"


def test_broken_srt_does_not_block_markdown(tmp_path, monkeypatch):
    """反向也成立：SRT 被占用 / 炸掉时 markdown 照常产出。"""
    import chrometrans.engine as engine_mod
    from chrometrans.output.jsonl import JsonlWriter

    session = tmp_path / "2026-09-23_1030"
    session.mkdir()
    (session / "captions.jsonl").write_text(
        '{"id": 1, "start": 0.0, "end": 1.0, "source": "x", '
        '"target": "y", "src_lang": "en", "tgt_lang": "zh"}\n',
        encoding="utf-8")

    def boom(cues):
        raise RuntimeError("srt 炸了")

    monkeypatch.setattr(engine_mod, "render_srt", boom)

    events = []
    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=events.append, asr=None, translator=None, segments=[])
    eng._render_views(session, JsonlWriter(session / "captions.jsonl"))

    assert (session / "captions.md").exists(), "SRT 失败不得连累 markdown"
    assert not (session / "captions.srt").exists()
    assert any(e["event"] == "error" for e in events)


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


def test_loading_status_is_emitted_before_the_model_is_loaded(tmp_path):
    """模型加载要几十秒。GUI 上不发这条事件，窗口看起来就是卡死的。"""
    events = []
    order = []

    class RecordingAsr:
        def load(self):
            order.append("load")

        def transcribe(self, segment):
            from chrometrans.asr.whisper_engine import TranscribeResult
            return TranscribeResult(utterance=None)

    class RecordingTranslator:
        async def translate(self, texts, src, tgt):
            return [None] * len(texts)

    def emit(event):
        events.append(event)
        if event["event"] == "status":
            order.append(event["data"]["state"])

    cfg = Config(output=OutputConfig(output_root=tmp_path))
    engine = Engine(cfg=cfg, on_event=emit, asr=RecordingAsr(),
                    translator=RecordingTranslator(), segments=())
    engine.run()

    loading = [e for e in events
               if e["event"] == "status" and e["data"]["state"] == "loading"]
    assert loading, "必须发出 loading 状态"
    assert "large-v3-turbo" in loading[0]["data"]["message"], "要说是哪个模型"
    assert order == ["loading", "load", "running", "stopped"], \
        "loading 必须在 asr.load() 之前发出，否则用户看到的是几十秒卡死"


def test_dropped_segments_are_reported_with_their_text(tmp_path):
    """C45：丢弃必须可见，且要带着文本。

    只说「丢了 1 段」用户判断不了那是垃圾还是真话 —— 而判断正是这个事件的全部
    目的。
    """
    from chrometrans.asr.whisper_engine import Dropped, TranscribeResult, Utterance

    events = []

    class DroppingAsr:
        def load(self): pass

        def transcribe(self, seg):
            return TranscribeResult(
                utterance=Utterance(start=seg.start, end=seg.end, text="留下的话"),
                dropped=(Dropped(text="编出来的话", no_speech_prob=0.95,
                                 avg_logprob=-1.8, compression_ratio=1.1),))

    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=events.append, asr=DroppingAsr(),
                 translator=StubTranslator(),
                 segments=[Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))])
    eng.run()

    drops = [e["data"] for e in events
             if e["event"] == "status" and e["data"].get("state") == "dropped"]
    assert len(drops) == 1
    assert "编出来的话" in drops[0]["text"]
    assert drops[0]["compression_ratio"] == 1.1, "三个统计量要一并带上"
    assert [e["data"]["source"] for e in events if e["event"] == "cue"] == ["留下的话"]


def test_dropped_segments_land_in_dropped_jsonl(tmp_path):
    """事件是瞬时的（字幕窗、网页的状态栏都会被下一句顶走），而 C45 要支撑的
    「试用完再回来调阈值」是事后行为 —— 只在事件里报等于没报。"""
    import json as jsonlib

    from chrometrans.asr.whisper_engine import Dropped, TranscribeResult

    class AllDroppedAsr:
        def load(self): pass

        def transcribe(self, seg):
            return TranscribeResult(utterance=None, dropped=(
                Dropped(text="编的", no_speech_prob=0.9, avg_logprob=-1.7,
                        compression_ratio=1.2),))

    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=lambda e: None, asr=AllDroppedAsr(),
                 translator=StubTranslator(),
                 segments=[Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))])
    eng.run()

    session = next(tmp_path.iterdir())
    lines = (session / "dropped.jsonl").read_text(encoding="utf-8").splitlines()
    assert [jsonlib.loads(l)["text"] for l in lines] == ["编的"]


def test_a_clean_session_writes_no_dropped_file(tmp_path):
    """没丢东西就不该多一个空文件 —— 空文件会被读成「丢了但没记下来」。"""
    eng, _ = _engine(tmp_path, StubTranslator(),
                     [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))])
    eng.run()

    assert not (next(tmp_path.iterdir()) / "dropped.jsonl").exists()


def test_dropped_text_in_the_event_is_bounded(tmp_path):
    """幻觉可以长到整段。事件要过 WebSocket，不该被一条文本撑爆。"""
    from chrometrans.asr.whisper_engine import Dropped, TranscribeResult

    long_text = "啊" * 5000
    events = []

    class LongDropAsr:
        def load(self): pass

        def transcribe(self, seg):
            return TranscribeResult(utterance=None, dropped=(
                Dropped(text=long_text, no_speech_prob=0.9, avg_logprob=-1.7,
                        compression_ratio=1.2),))

    eng = Engine(cfg=Config(output=OutputConfig(output_root=tmp_path)),
                 on_event=events.append, asr=LongDropAsr(),
                 translator=StubTranslator(),
                 segments=[Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))])
    eng.run()

    drop = [e["data"] for e in events if e["data"].get("state") == "dropped"][0]
    assert len(drop["message"]) < 500, "事件里的文本要截断"
    assert len(drop["text"]) == 5000, "落盘的原文不截断"


def test_monolingual_session_never_calls_the_translator(tmp_path):
    """C44：单语会话不调翻译器，也不发翻译失败事件。

    「被调用即失败」比断言结果更硬 —— 结果是 None 也可能是翻译失败换来的，
    而那正是要区分的东西。
    """
    class ExplodingTranslator:
        name = "boom"

        async def translate(self, texts, src, tgt):
            raise AssertionError("单语会话不得调用翻译器（C44）")

    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    events = []
    eng = Engine(
        cfg=Config(output=OutputConfig(output_root=tmp_path),
                   asr=AsrConfig(language="zh"),
                   translate=TranslateConfig(src=None, tgt="zh-Hans")),
        on_event=events.append, asr=_stub_asr("这句话"),
        translator=ExplodingTranslator(), segments=segs)

    eng.run()

    cue = [e["data"] for e in events if e["event"] == "cue"][0]
    assert cue["source"] == "这句话"
    assert cue["target"] is None
    assert cue["tgt_lang"] is None, "tgt_lang 为 None = 本会话不翻译（spec §5.3）"
    assert cue["src_lang"] == "zh", "src_lang 取实际识别的语言，永不为 None"
    assert not [e for e in events if e["event"] == "error"], \
        "故意不翻译不得表现为翻译失败（C44）"


def test_target_language_is_recorded_in_bilingual_sessions(tmp_path):
    """反向也要锁：双语会话的 tgt_lang 必须保持有值，否则上一条就成了
    「不管什么会话都写 None」。"""
    segs = [Segment(1, 0.0, 2.0, np.zeros(32000, dtype=np.float32))]
    eng, events = _engine(tmp_path, StubTranslator(), segs)

    eng.run()

    cue = [e["data"] for e in events if e["event"] == "cue"][0]
    assert cue["tgt_lang"] == "zh-Hans"
    assert cue["target"] == "译:hello world"


def test_running_status_reports_bilingual_both_ways(tmp_path):
    """会话级标志由启动的 status 事件给出（spec §5.3）—— 两个方向都断言。"""
    segs = []

    def bilingual_flag(cfg):
        events = []
        Engine(cfg=cfg, on_event=events.append, asr=_stub_asr("x"),
               translator=StubTranslator(), segments=segs).run()
        return [e["data"]["bilingual"] for e in events
                if e["event"] == "status" and e["data"].get("state") == "running"][0]

    assert bilingual_flag(Config(output=OutputConfig(output_root=tmp_path / "a"))) is True
    assert bilingual_flag(Config(output=OutputConfig(output_root=tmp_path / "b"),
                                 translate=TranslateConfig(src=None))) is False


def test_dropped_view_failure_does_not_escape(tmp_path):
    """R22 同理：这条路径跑在 run() 的 finally 里，抛出去会吞掉 stopped 事件。

    今天 json.dumps 在 Dropped 的字段类型下抛不出来，但那份不变量住在另一个模块
    —— 把保证做成结构性的，而不是文档性的。
    """
    eng, events = _engine(tmp_path, StubTranslator(), [])
    eng._dropped = [{"text": object()}]

    eng.run()

    assert any(e["event"] == "status" and e["data"].get("state") == "stopped"
               for e in events), "stopped 事件必须发得出来"
    assert any(e["event"] == "error" and "dropped.jsonl" in e["data"]["message"]
               for e in events)
    assert not (next(tmp_path.iterdir()) / "dropped.jsonl").exists()


def test_dropped_write_failure_reports_the_truth(tmp_path, monkeypatch):
    """写入失败时那句话也必须是真的：丢弃记录只在内存里，没有 JSONL 副本可重渲。"""
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("chrometrans.engine.write_with_fallback", boom)
    eng, events = _engine(tmp_path, StubTranslator(), [])
    eng._dropped = [{"text": "编的", "no_speech_prob": 0.9,
                     "avg_logprob": -1.7, "compression_ratio": 1.2}]

    eng.run()

    msgs = [e["data"]["message"] for e in events
            if e["event"] == "error" and "dropped.jsonl" in e["data"]["message"]]
    assert msgs, "写入失败必须说出来"
    assert "可随时重渲" not in msgs[0], "丢弃记录不在 JSONL 里，这句话是假的"
    assert "只在内存" in msgs[0]
    assert any(e["event"] == "status" and e["data"].get("state") == "stopped"
               for e in events), "重渲失败不得吞掉 stopped 事件"

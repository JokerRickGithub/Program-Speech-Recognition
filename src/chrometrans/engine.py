"""流水线编排。与显示层、存储层完全解耦（spec §5.6）。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from chrometrans.audio.segmenter import Segment
from chrometrans.config import Config
from chrometrans.models import Cue
from chrometrans.output.atomic import write_with_fallback
from chrometrans.output.jsonl import JsonlWriter, session_dir
from chrometrans.output.markdown import render as render_markdown
from chrometrans.output.srt import render as render_srt


class Engine:
    """把各模块串起来，对外只发 cue / status / error 三种事件。

    依赖以参数注入，便于在无 GPU、无音频设备的环境下单测。
    """

    def __init__(self, cfg: Config, on_event: Callable[[dict], None],
                 asr, translator, segments: Iterable[Segment],
                 segment_iter_factory=None):
        self._cfg = cfg
        self._emit = on_event
        self._asr = asr
        self._translator = translator
        self._segments = segments
        self._segment_iter_factory = segment_iter_factory
        self._running = False

    # ---- 对外 ----

    def run(self) -> None:
        self._running = True
        self._asr.load()

        session = session_dir(self._cfg.output.output_root, datetime.now())
        writer = JsonlWriter(session / "captions.jsonl")
        writer.open()

        self._emit({"event": "status", "data": {
            "state": "running", "model": self._cfg.asr.model,
            "device": self._cfg.asr.device,
            "session": str(session)}})

        # 同分钟内重启会复用 session 目录，而 JSONL 是追加写的（R21）：
        # cue_id 必须从既有内容续上，否则同一份文件里会出现重复 id，
        # SRT 里会出现两条 1 号字幕。
        cue_id = max((c.id for c in writer.read_all()), default=0)
        source: Iterable[Segment] = (
            self._segment_iter_factory() if self._segment_iter_factory
            else self._segments
        )

        try:
            for segment in source:
                if not self._running:
                    break
                utterance = self._transcribe(segment)
                if utterance is None:
                    continue

                cue_id += 1
                target = self._translate(utterance.text)
                cue = Cue(id=cue_id, start=utterance.start, end=utterance.end,
                          source=utterance.text, target=target,
                          src_lang=self._cfg.translate.src,
                          tgt_lang=self._cfg.translate.tgt)

                try:
                    writer.append(cue)
                except OSError as exc:
                    self._emit({"event": "error",
                                "data": {"message": f"字幕落盘失败，已停止：{exc}"}})
                    break

                self._emit({"event": "cue", "data": cue.to_dict()})
                self._render_views_if_due(session, writer, cue_id)
        finally:
            try:
                writer.close()
            except Exception as exc:
                self._emit({"event": "error", "data": {
                    "message": f"字幕文件收尾失败（JSONL 可能缺最后几行）：{exc}"}})
            # 收尾必须再渲一次：否则不足 srt_rerender_every 条的短会话
            # （上一节课只录到几十句）根本不会产生 SRT / Markdown 文件
            self._render_views(session, writer)

        self._emit({"event": "status", "data": {"state": "stopped"}})

    def stop(self) -> None:
        self._running = False

    # ---- 内部 ----

    def _transcribe(self, segment: Segment):
        try:
            return self._asr.transcribe(segment)
        except Exception as exc:
            # 单段失败不得中断流水线（spec §6）
            self._emit({"event": "error", "data": {
                "message": f"第 {segment.index} 段识别失败：{exc}"}})
            return None

    def _translate(self, text: str) -> str | None:
        try:
            results = asyncio.run(self._translator.translate(
                [text], self._cfg.translate.src, self._cfg.translate.tgt))
        except Exception as exc:
            self._emit({"event": "error", "data": {"message": f"翻译失败：{exc}"}})
            return None
        return results[0] if results else None

    def _render_views_if_due(self, session: Path, writer: JsonlWriter,
                             cue_id: int) -> None:
        if cue_id % self._cfg.output.srt_rerender_every != 0:
            return
        self._render_views(session, writer)

    def _render_views(self, session: Path, writer: JsonlWriter) -> None:
        """从 JSONL 重渲全部派生视图。

        逐视图独立兜错：一个视图渲不出来（文件被占用、数据畸形）不能让另一个
        视图也缺一块 —— 两者的失败原因毫不相干，没理由互相连累。
        """
        self._render_one(session, "captions.srt", render_srt, writer)
        self._render_one(session, "captions.md", render_markdown, writer)

    def _render_srt(self, session: Path, writer: JsonlWriter) -> None:
        """只渲 SRT。

        保留这个入口是为了单测能直接喂畸形 JSONL 验证「重渲绝不抛」的契约（R22）。
        """
        self._render_one(session, "captions.srt", render_srt, writer)

    def _render_one(self, session: Path, filename: str, renderer,
                    writer: JsonlWriter) -> None:
        """渲染单个派生视图。失败不阻塞流水线（JSONL 才是权威源）。"""
        try:
            # read_all() 放在 try 里面：JSONL 读不出来（被独占、文件损坏）同样
            # 不能逃出去 —— 这条路径跑在 run() 的 finally 里。
            written = write_with_fallback(
                session / filename, renderer(writer.read_all()),
                retries=self._cfg.output.replace_retries,
                base_delay=self._cfg.output.replace_base_delay_s)
        except Exception as exc:
            # 重渲绝不能抛：它跑在 run() 的 finally 里，抛出去会吞掉
            # stopped 事件并让整个捕获进程崩掉（R22）。JSONL 完好，随时可重渲。
            self._emit({"event": "error", "data": {
                "message": f"{filename} 重渲失败，本次跳过（JSONL 完好，可随时重渲）：{exc}"}})
            return
        if written is None:
            self._emit({"event": "error", "data": {
                "message": f"{filename} 两处路径都写不进去，本次跳过（JSONL 完好，可随时重渲）"}})
        elif written.name != filename:
            self._emit({"event": "error", "data": {
                "message": f"{filename} 被占用，已改写到 {written.name}"}})

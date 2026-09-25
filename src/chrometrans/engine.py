"""流水线编排。与显示层、存储层完全解耦（spec §5.6）。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from chrometrans.audio.segmenter import Segment
from chrometrans.config import Config, is_bilingual
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
        self._dropped: list[dict] = []
        # 单语模式是会话级属性，由 translate.src 是否为 None 派生 —— 不另设
        # 一个需要和它保持同步的开关（spec §5.1 对 translate_src 就是这么做的）。
        self._bilingual = is_bilingual(cfg)

    # ---- 对外 ----

    def run(self) -> None:
        self._running = True
        # 模型加载要几十秒（首次还要下载 1.6 GB）。不发这条事件，GUI 上就是
        # 一个没反应的窗口 —— 用户会以为程序挂了而不是在加载。
        self._emit({"event": "status", "data": {
            "state": "loading",
            "message": f"正在加载模型 {self._cfg.asr.model}…"}})
        self._asr.load()

        session = session_dir(self._cfg.output.output_root, datetime.now())
        writer = JsonlWriter(session / "captions.jsonl")
        writer.open()

        self._emit({"event": "status", "data": {
            "state": "running", "model": self._cfg.asr.model,
            "device": self._cfg.asr.device,
            "session": str(session),
            "bilingual": self._bilingual}})

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
                result = self._transcribe(segment)
                if result is None:
                    continue
                self._report_drops(result.dropped)
                utterance = result.utterance
                if utterance is None:
                    continue

                cue_id += 1
                target = self._translate(utterance.text)
                cue = Cue(id=cue_id, start=utterance.start, end=utterance.end,
                          source=utterance.text, target=target,
                          # src_lang 取实际识别的语言而非 translate.src：
                          # 单语时后者是 None，而识别的语言是确定无疑的（spec §5.3）
                          src_lang=self._cfg.asr.language,
                          tgt_lang=(self._cfg.translate.tgt
                                    if self._bilingual else None))

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
        """返回 TranscribeResult；单段失败时返回 None（不得中断流水线，spec §6）。"""
        try:
            return self._asr.transcribe(segment)
        except Exception as exc:
            # 单段失败不得中断流水线（spec §6）
            self._emit({"event": "error", "data": {
                "message": f"第 {segment.index} 段识别失败：{exc}"}})
            return None

    def _report_drops(self, dropped) -> None:
        """C45：丢弃必须可见。

        静默 continue 有两个后果：与本项目对静默失败的一贯取向矛盾（降级要播报、
        启动自检读到静音要报警），而且「先按默认值、试用后再调」这条约定会失效
        —— 偏严的误杀用户永远发现不了，只会觉得字幕时有时无。
        """
        for item in dropped:
            record = {"text": item.text,
                      "no_speech_prob": item.no_speech_prob,
                      "avg_logprob": item.avg_logprob,
                      "compression_ratio": item.compression_ratio}
            self._dropped.append(record)
            self._emit({"event": "status", "data": {
                "state": "dropped",
                "message": f"丢弃疑似幻觉：{_preview(item.text)}",
                **record}})

    def _translate(self, text: str) -> str | None:
        """单语会话直接返回 None，**不调翻译器**（C44）。

        这里不是「翻译失败」，是「本会话不翻译」。区分靠 Cue.tgt_lang：
        单语时它是 None，双语时是有值的语言码。所以这里也不发任何事件 ——
        单语会话里一条 error 都不该有。
        """
        if not self._bilingual:
            return None
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
        self._render_drops(session)
        self._render_one(session, "captions.srt", render_srt, writer)
        self._render_one(session, "captions.md", render_markdown, writer)

    def _render_drops(self, session: Path) -> None:
        """把被丢弃的段写成 dropped.jsonl。

        与另外两个视图同规矩：逐视图独立兜错，失败不阻塞流水线。
        """
        if not self._dropped:
            return
        try:
            body = "".join(json.dumps(r, ensure_ascii=False) + "\n"
                           for r in self._dropped)
        except Exception as exc:
            # 与 _render_one 同理：这条路径跑在 run() 的 finally 里。
            # 但这里**不能**说「JSONL 完好，可随时重渲」—— 丢弃记录只在内存里
            # （self._dropped），JSONL 中没有它们的副本，所以这句要说实话。
            self._emit({"event": "error", "data": {
                "message": f"dropped.jsonl 写出失败，本次跳过"
                           f"（丢弃记录只在内存里，JSONL 中没有它们的副本）：{exc}"}})
            return
        self._write_view(session, "dropped.jsonl", body,
                         recovery_note="（丢弃记录只在内存里，JSONL 中没有它们的副本）")

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
            body = renderer(writer.read_all())
        except Exception as exc:
            # 重渲绝不能抛：它跑在 run() 的 finally 里，抛出去会吞掉
            # stopped 事件并让整个捕获进程崩掉（R22）。JSONL 完好，随时可重渲。
            self._emit({"event": "error", "data": {
                "message": f"{filename} 重渲失败，本次跳过（JSONL 完好，可随时重渲）：{exc}"}})
            return
        self._write_view(session, filename, body)

    def _write_view(self, session: Path, filename: str, body: str, *,
                    recovery_note: str = "（JSONL 完好，可随时重渲）") -> None:
        """原子写一个派生文件，失败只报不抛。

        `recovery_note` 说明这次失败会丢掉什么。默认那句对 srt / markdown 成立
        —— 内容都在 JSONL 里，随时可重渲；但**对 dropped.jsonl 不成立**，丢弃
        记录只在内存里。这条注释必须说实话，否则用户以为自己什么都没丢。
        """
        try:
            written = write_with_fallback(
                session / filename, body,
                retries=self._cfg.output.replace_retries,
                base_delay=self._cfg.output.replace_base_delay_s)
        except Exception as exc:
            self._emit({"event": "error", "data": {
                "message": f"{filename} 重渲失败，本次跳过{recovery_note}：{exc}"}})
            return
        if written is None:
            self._emit({"event": "error", "data": {
                "message": f"{filename} 两处路径都写不进去，本次跳过{recovery_note}"}})
        elif written.name != filename:
            self._emit({"event": "error", "data": {
                "message": f"{filename} 被占用，已改写到 {written.name}"}})


def _preview(text: str, limit: int = 200) -> str:
    """事件里的文本要截断：幻觉可以长到整段，而事件要走 WebSocket。

    落盘的 `dropped.jsonl` 不截断 —— 那是给人逐条看的。
    """
    return text if len(text) <= limit else text[:limit] + "…"

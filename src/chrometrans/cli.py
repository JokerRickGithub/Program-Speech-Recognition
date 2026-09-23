"""命令行入口。"""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from dataclasses import replace
from typing import Callable, Iterator

from chrometrans.audio.segmenter import Segment, Segmenter
from chrometrans.audio.source import CaptureSource
from chrometrans.config import Config, load_config
from chrometrans.engine import Engine
from chrometrans.translate.base import ChainTranslator
from chrometrans.translate.google import GoogleFreeTranslator, GoogleTranslator
from chrometrans.translate.microsoft import MicrosoftTranslator

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="chrometrans", description="Chrome 网课实时字幕")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-server", action="store_true",
                        help="不启动网页，只识别与落盘")
    parser.add_argument("--process", default="chrome.exe")
    args = parser.parse_args(argv)
    if args.host not in LOOPBACK_HOSTS:
        parser.error(f"只允许绑定回环地址（C25），收到：{args.host}")
    return args


def build_translator_chain(cfg) -> ChainTranslator:
    """Tier 1 Azure → Tier 2 Google(key) → 免 key 兜底（谷歌 → 微软 Edge）。

    带 key 的层放最前面：放最后只会让每次翻译先白吃一个失败请求。
    免 key 这两层的实测结论（2026-09-23）不同，所以次序有别：谷歌那条可用，
    微软 Edge 那条（`edge.microsoft.com/translate/auth`）已 404 下线。死的那层
    放最后，只有谷歌也失败时才会为它多花一次请求。

    ⚠️ 不配 key 时这条链**只剩免 key 层**，也就是 C17 明令禁止的「免 key 端点
    作为单点依赖」—— 谷歌那个端点同样是未公开的，可能像微软 Edge 的 auth 端点
    一样一夜之间 404。这里做不到在没 key 的情况下满足 C17 的字面要求，所以改为
    满足它的本意：**别让它静默发生**。没配 key 时 `keyless_notice()` 会在启动时
    明确告知。想真正满足 C17 就得配 key。
    """
    providers = []
    if cfg.azure_key:
        providers.append(MicrosoftTranslator(api_key=cfg.azure_key,
                                             region=cfg.azure_region,
                                             timeout_s=cfg.timeout_s))
    if cfg.google_key:
        providers.append(GoogleTranslator(api_key=cfg.google_key,
                                          timeout_s=cfg.timeout_s))
    providers.append(GoogleFreeTranslator(timeout_s=cfg.timeout_s))
    providers.append(MicrosoftTranslator(api_key=None, region=cfg.azure_region,
                                         timeout_s=cfg.timeout_s))
    return ChainTranslator(providers, cfg)


def keyless_notice(cfg) -> str | None:
    """没配 key 时返回一句提醒，配了就返回 None。

    C17 要求免 key 端点不可作为单点依赖。不配 key 时链上确实只剩免 key 层，
    而那个谷歌端点是未公开的 —— 约束的本意是别让这件事**静默**发生
    （当初就是微软的 auth 端点一夜 404 打断了所有集成）。所以这里把它说出来。
    """
    if cfg.azure_key or cfg.google_key:
        return None
    return ("没有配置翻译 key：中文将由免 key 谷歌通道提供。"
            "那是未公开接口，随时可能失效或被限流（C17）。"
            "想稳定请设 AZURE_TRANSLATOR_KEY 或 GOOGLE_TRANSLATE_KEY。")


def segment_source(cfg: Config, emit: Callable[[dict], None],
                   process_names: tuple[str, ...]
                   ) -> Callable[[], Iterator[Segment]]:
    """音频 → 语音段。重连与降级由 CaptureSource 负责（Task 12）。"""
    def factory() -> Iterator[Segment]:
        capture_cfg = replace(cfg.capture, process_names=process_names)
        source = CaptureSource(capture_cfg, emit)
        segmenter = Segmenter(cfg.segmenter)
        try:
            for chunk in source.chunks():
                if chunk.size:
                    yield from segmenter.feed(chunk)
        finally:
            # 不能在 finally 里 yield：生成器被关闭时 GeneratorExit 会撞上这里的
            # yield → RuntimeError，且下面的 stop() 会被整个跳过。
            source.stop()
        # 只有正常耗尽才排空尾部；Ctrl+C 提前关闭时不会走到这里，
        # 正在说的那半句丢掉是预期行为（它本来就不完整）。
        yield from segmenter.flush()
    return factory


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config()

    notice = keyless_notice(cfg.translate)
    if notice:
        print(f"! {notice}", file=sys.stderr)

    from chrometrans.asr.whisper_engine import WhisperEngine
    from chrometrans.server import EventBus, create_app

    bus = EventBus()
    loop: asyncio.AbstractEventLoop | None = None

    if not args.no_server:
        import uvicorn

        def _serve() -> None:
            nonlocal loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            # loop="asyncio" 阻止 uvicorn 另起一个循环，否则下面的 loop 指向的
            # 是个空转的循环，所有事件都会被投递到没人消费的地方
            config = uvicorn.Config(create_app(bus), host=args.host,
                                    port=args.port, log_level="warning",
                                    loop="asyncio")
            loop.run_until_complete(uvicorn.Server(config).serve())

        threading.Thread(target=_serve, daemon=True).start()
        print(f"字幕页： http://{args.host}:{args.port}/")

    def emit(event: dict) -> None:
        print_event(event)
        publish(bus, loop, event)

    engine = Engine(cfg=cfg, on_event=emit,
                    asr=WhisperEngine(cfg.asr),
                    translator=build_translator_chain(cfg.translate),
                    segments=(),
                    segment_iter_factory=segment_source(
                        cfg, emit, (args.process,)))

    try:
        engine.run()
    except KeyboardInterrupt:
        engine.stop()
    return 0


def print_event(event: dict) -> None:
    """字幕走 stdout，状态与错误走 stderr。

    分开是为了 `uv run chrometrans > cues.txt` 能拿到干净的字幕流。
    """
    data = event.get("data", {})
    if event["event"] == "cue":
        print(f"[{data['start']:6.2f}] {data['source']}")
        if data.get("target"):
            print(f"         {data['target']}")
        return

    if event["event"] == "error":
        print(f"! {data.get('message')}", file=sys.stderr)
        return

    # 状态事件以前只做给网页看，终端全程静默 —— 于是「到底抓没抓到、有没有
    # 降级」只能靠另外开网页确认，而「启动自检读到静音」这条连网页之外无迹可寻。
    # 这里把四个状态都打出来，文案与 index.html 的 setStatus() 逐字一致，
    # 免得同一个状态在两个地方有两种说法。
    state = data.get("state")
    if state == "running":
        if data.get("model"):
            print(f"— 运行中 · {data['model']} · {data['device']}", file=sys.stderr)
        else:
            print(f"— 运行中 · 按进程捕获（PID {data.get('pid')}）", file=sys.stderr)
    elif state in ("degraded", "warning"):
        print(f"! {data.get('message')}", file=sys.stderr)
    elif state == "stopped":
        print("— 已停止", file=sys.stderr)


def publish(bus, loop: asyncio.AbstractEventLoop | None, event: dict) -> None:
    """把事件投给 uvicorn 的事件循环。

    引擎跑在主线程、uvicorn 跑在后台线程，**两者的循环不同** —— 必须用
    `run_coroutine_threadsafe` 跨线程投递。`create_task` 在这里会静默丢事件，
    网页永远看不到字幕。
    """
    if loop is None or not loop.is_running():
        return                       # 服务还没起来，丢弃即可
    try:
        asyncio.run_coroutine_threadsafe(bus.publish(event), loop)
    except RuntimeError:
        pass                         # 循环正在关闭


if __name__ == "__main__":
    raise SystemExit(main())

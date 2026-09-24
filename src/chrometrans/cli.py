"""命令行入口。"""
from __future__ import annotations

import argparse
import sys

from chrometrans.config import DEFAULT_LANGUAGE, LANGUAGES, load_config
from chrometrans.engine import Engine
from chrometrans.pipeline import segment_source
from chrometrans.serving import start_server
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
    # choices 由 LANGUAGES 生成：加语言时这里不用跟着改，而未知值由 argparse
    # 直接拒绝并列全可用值 —— 正是 C38 要的那句话。
    parser.add_argument("--language", default=DEFAULT_LANGUAGE,
                        choices=sorted(LANGUAGES),
                        help="音频语言；同时决定 ASR 语言与翻译源语言（C39）")
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config(args.language)

    notice = keyless_notice(cfg.translate)
    if notice:
        print(f"! {notice}", file=sys.stderr)

    from chrometrans.asr.whisper_engine import WhisperEngine
    from chrometrans.server import EventBus

    bus = EventBus()
    publish = (lambda event: None) if args.no_server else start_server(
        bus, args.host, args.port)
    if not args.no_server:
        print(f"字幕页： http://{args.host}:{args.port}/")

    def emit(event: dict) -> None:
        print_event(event)
        publish(event)

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
        # 模式由会话级的 bilingual 派生（Task 6）。中文单语会话下「不翻译」是
        # 设计如此 —— 不写出来，用户会以为翻译坏了（C44）。文案与 index.html
        # 的 setStatus() 一致。
        mode = data.get("bilingual")
        suffix = "" if mode is None else (" · 翻译中" if mode else " · 不翻译")
        if data.get("model"):
            print(f"— 运行中 · {data['model']} · {data['device']}{suffix}",
                  file=sys.stderr)
        else:
            print(f"— 运行中 · 按进程捕获（PID {data.get('pid')}）{suffix}",
                  file=sys.stderr)
    elif state == "loading":
        print(f"— {data.get('message')}", file=sys.stderr)
    elif state in ("degraded", "warning", "dropped"):
        # dropped 与这两个同类：都是「有东西没按预期走」。C45 要求它可见，
        # 而终端是唯一不会被下一句字幕顶走的地方 —— 它还有 scrollback。
        print(f"! {data.get('message')}", file=sys.stderr)
    elif state == "stopped":
        print("— 已停止", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

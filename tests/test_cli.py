import pytest

from chrometrans.cli import (
    build_translator_chain,
    keyless_notice,
    parse_args,
    print_event,
)
from chrometrans.config import TranslateConfig


def test_parse_args_defaults():
    args = parse_args([])
    assert args.host == "127.0.0.1"
    assert args.no_server is False


def test_parse_args_rejects_non_loopback_host():
    """C25：只允许绑回环地址。"""
    with pytest.raises(SystemExit):
        parse_args(["--host", "0.0.0.0"])


def test_only_free_tiers_when_no_keys():
    """没 key 也必须有能用的兜底：免 key 谷歌。

    2026-09-25 移除了原来排在最后的免 key 微软层（那条路已 404 下线）—— 留着它
    等于每次全体失败都先白等一次 404，而失败本来就慢。
    """
    cfg = TranslateConfig(azure_key=None, google_key=None)
    chain = build_translator_chain(cfg)
    assert [p.name for p in chain._providers] == ["google-free"]


def test_keyless_notice_fires_exactly_when_there_is_no_key():
    """C17 的字面要求在没 key 时做不到，那就满足它的本意：别静默。"""
    assert keyless_notice(TranslateConfig(azure_key=None, google_key=None))
    assert keyless_notice(TranslateConfig(azure_key="A", google_key=None)) is None
    assert keyless_notice(TranslateConfig(azure_key=None, google_key="G")) is None
    assert keyless_notice(TranslateConfig(azure_key="A", google_key="G")) is None
    assert keyless_notice(
        TranslateConfig(azure_key=None, google_key=None, yandex_key="Y")) is None


def test_full_chain_when_all_keys_present():
    cfg = TranslateConfig(azure_key="A", google_key="G")
    chain = build_translator_chain(cfg)
    assert [p.name for p in chain._providers] == [
        "microsoft-azure", "google", "google-free"]


def test_yandex_is_a_tier_before_the_keyless_fallback():
    """带 key 的层永远排在免 key 之前 —— 放后面就是每次先白吃一个失败请求。"""
    cfg = TranslateConfig(azure_key=None, google_key=None,
                          yandex_key="Y", yandex_folder_id="F")
    chain = build_translator_chain(cfg)
    assert [p.name for p in chain._providers] == ["yandex", "google-free"]


def test_yandex_needs_only_the_key_not_the_folder():
    """服务账号的 API key 不需要 folderId（目录由账号本身决定，见 yandex.py）。

    要是把 folder_id 也当成必填，配了 key 却忘了它的人会得到一个静默缺席的通道。
    """
    cfg = TranslateConfig(azure_key=None, google_key=None, yandex_key="Y",
                          yandex_folder_id=None)
    assert [p.name for p in build_translator_chain(cfg)._providers] == [
        "yandex", "google-free"]


def test_print_event_reports_every_status_transition(capsys):
    """四个状态都要在终端可见。

    回归：以前只认 error/degraded，于是「启动自检读到静音」（warning）和
    「已停止」在终端上是彻底静默的 —— 用户分不清"在正常工作"和"根本没在录"。
    """
    print_event({"event": "status", "data": {
        "state": "running", "model": "large-v3-turbo", "device": "cuda"}})
    print_event({"event": "status", "data": {
        "state": "running", "capture": "process", "pid": 4321}})
    print_event({"event": "status", "data": {
        "state": "warning", "message": "启动自检未通过（读到的是静音）"}})
    print_event({"event": "status", "data": {
        "state": "degraded", "message": "已降级到系统级捕获：会混入背景音乐"}})
    print_event({"event": "status", "data": {
        "state": "loading", "message": "正在加载模型 large-v3-turbo…"}})
    print_event({"event": "status", "data": {"state": "stopped"}})

    err = capsys.readouterr().err
    assert "运行中" in err and "large-v3-turbo" in err
    assert "PID 4321" in err, "第二种 running 事件要报按进程捕获的 PID"
    assert "启动自检未通过" in err, "warning 以前是静默的"
    assert "已降级到系统级捕获" in err
    assert "正在加载模型" in err, "模型加载期间的几十秒不能是静默的"
    assert "已停止" in err


def test_print_event_keeps_stdout_clean_for_cues(capsys):
    """字幕走 stdout、状态走 stderr，好让 `> cues.txt` 拿到干净的流。"""
    print_event({"event": "cue", "data": {
        "start": 1.5, "source": "hello", "target": "你好"}})
    print_event({"event": "status", "data": {"state": "stopped"}})

    out, err = capsys.readouterr()
    assert "hello" in out and "你好" in out
    assert "已停止" not in out, "状态不能污染 stdout"
    assert "已停止" in err


def test_parse_args_language_defaults_to_english():
    from chrometrans.config import DEFAULT_LANGUAGE

    assert parse_args([]).language == DEFAULT_LANGUAGE


def test_parse_args_rejects_unknown_language_and_lists_the_choices(capsys):
    """C38：未知值拒绝启动并列出可用值，不得静默退回默认语言。"""
    with pytest.raises(SystemExit) as exc:
        parse_args(["--language", "klingon"])

    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "klingon" in err
    assert "ru" in err and "zh" in err, "要列出可用值"


def test_language_choices_come_from_the_profile_table():
    """加语言时 CLI 不该还要单独改一处。"""
    from chrometrans.config import LANGUAGES

    for code in LANGUAGES:
        assert parse_args(["--language", code]).language == code


def test_main_forwards_the_language_choice(monkeypatch):
    """最危险的是 main 忘了把 args.language 传下去 —— 那就退回英语模型听俄语
    （C39）。这条测的就是那一行。

    只需要挡掉 Engine 一个：`WhisperEngine(cfg.asr)` 构造时不加载模型、
    `build_translator_chain` 不联网、`segment_source` 返回的是个还没被调用的
    工厂 —— 真正的重活都在 Engine.run 里，而 NoopEngine 不跑它。
    """
    import chrometrans.cli as cli
    from chrometrans.config import Config

    seen = {}

    def fake_load(language=cli.DEFAULT_LANGUAGE):
        seen["language"] = language
        return Config()

    class NoopEngine:
        def __init__(self, **kwargs): pass
        def run(self): pass
        def stop(self): pass

    monkeypatch.setattr(cli, "load_config", fake_load)
    monkeypatch.setattr(cli, "Engine", NoopEngine)

    assert cli.main(["--no-server", "--language", "ru"]) == 0
    assert seen["language"] == "ru"


def test_print_event_reports_dropped_audio(capsys):
    """排空缓冲溢出丢掉的音频必须报出来。

    与 C45 要求丢弃幻觉可见同一个取向：不报的话用户只看到字幕少了几句，而原因
    （翻译卡顿 / 机器跟不上）无处可查。终端是唯一不会被下一句字幕顶走的地方。
    """
    print_event({"event": "status", "data": {
        "state": "audio_dropped",
        "message": "处理跟不上，已丢弃约 3.2 秒音频（这几句字幕会缺）"}})

    err = capsys.readouterr().err
    assert "丢弃约 3.2 秒音频" in err
    assert "这几句字幕会缺" in err, "要说清代价，不然用户不知道这意味着什么"


def test_print_event_reports_dropped_segments(capsys):
    """C45：丢弃在终端上必须是可见的。

    与 warning / degraded 同类：都是「有东西没按预期走」。
    """
    print_event({"event": "status", "data": {
        "state": "dropped", "message": "丢弃疑似幻觉：谢谢观看", "text": "谢谢观看",
        "no_speech_prob": 0.95, "avg_logprob": -1.8, "compression_ratio": 1.1}})

    out, err = capsys.readouterr()
    assert "丢弃疑似幻觉" in err
    assert "谢谢观看" in err
    assert "谢谢观看" not in out, "状态不能污染 stdout"


def test_running_line_reports_the_session_mode(capsys):
    """C44：单语会话下「不翻译」是设计如此，值得说出来。

    C43 要求三个显示面逐字一致 —— 中间点、空格、后缀，错一个都是违约，
    子串检查发现不了。所以这里锁整行相等，与 GUI 测试同法；网页那条锁在
    tests/test_static_index.py 的 test_the_running_line_reports_the_session_mode。
    """
    print_event({"event": "status", "data": {
        "state": "running", "model": "large-v3-turbo", "device": "cuda",
        "bilingual": True}})
    err = capsys.readouterr().err.strip()
    assert err == "— 运行中 · large-v3-turbo · cuda · 翻译中"

    print_event({"event": "status", "data": {
        "state": "running", "model": "large-v3-turbo", "device": "cuda",
        "bilingual": False}})
    err = capsys.readouterr().err.strip()
    assert err == "— 运行中 · large-v3-turbo · cuda · 不翻译"

    print_event({"event": "status", "data": {
        "state": "running", "pid": 123, "bilingual": False}})
    err = capsys.readouterr().err.strip()
    assert err == "— 运行中 · 按进程捕获（PID 123） · 不翻译"


def test_running_line_says_nothing_about_mode_when_the_key_is_missing(capsys):
    """键缺失时什么也不追加 —— 把「不知道」说成「不翻译」是最危险的静默说谎。"""
    print_event({"event": "status", "data": {
        "state": "running", "model": "large-v3-turbo", "device": "cuda"}})

    err = capsys.readouterr().err.strip()
    assert err == "— 运行中 · large-v3-turbo · cuda"


def test_keyless_notice_is_silent_for_a_monolingual_session():
    """C44：单语会话不翻译，提醒「没配 key」是凭空造出的失败面；而且原文案把
    中文说成谷歌翻出来的目标，在 zh profile 里中文是源语言，那句话是假的。
    """
    from dataclasses import replace

    from chrometrans.config import load_config

    base = replace(load_config("zh").translate, azure_key=None, google_key=None)
    assert keyless_notice(base) is None, "单语会话不该提醒没配 key"
    assert keyless_notice(replace(base, src="en")) is not None, "会翻译就得提醒（C17）"

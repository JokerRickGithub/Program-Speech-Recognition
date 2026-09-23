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
    """没 key 也必须有能用的兜底：免 key 谷歌排在前，微软 Edge 那条已下线排最后。"""
    cfg = TranslateConfig(azure_key=None, google_key=None)
    chain = build_translator_chain(cfg)
    assert [p.name for p in chain._providers] == ["google-free", "microsoft-free"]


def test_keyless_notice_fires_exactly_when_there_is_no_key():
    """C17 的字面要求在没 key 时做不到，那就满足它的本意：别静默。"""
    assert keyless_notice(TranslateConfig(azure_key=None, google_key=None))
    assert keyless_notice(TranslateConfig(azure_key="A", google_key=None)) is None
    assert keyless_notice(TranslateConfig(azure_key=None, google_key="G")) is None
    assert keyless_notice(TranslateConfig(azure_key="A", google_key="G")) is None


def test_full_chain_when_all_keys_present():
    cfg = TranslateConfig(azure_key="A", google_key="G")
    chain = build_translator_chain(cfg)
    assert [p.name for p in chain._providers] == [
        "microsoft-azure", "google", "google-free", "microsoft-free"]


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

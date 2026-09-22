import pytest

from chrometrans.cli import build_translator_chain, parse_args
from chrometrans.config import TranslateConfig


def test_parse_args_defaults():
    args = parse_args([])
    assert args.host == "127.0.0.1"
    assert args.no_server is False


def test_parse_args_rejects_non_loopback_host():
    """C25：只允许绑回环地址。"""
    with pytest.raises(SystemExit):
        parse_args(["--host", "0.0.0.0"])


def test_free_tier_only_when_no_keys():
    cfg = TranslateConfig(azure_key=None, google_key=None)
    chain = build_translator_chain(cfg)
    assert len(chain._providers) == 1
    assert chain._providers[0].name == "microsoft-free"


def test_full_chain_when_all_keys_present():
    cfg = TranslateConfig(azure_key="A", google_key="G")
    chain = build_translator_chain(cfg)
    assert [p.name for p in chain._providers] == [
        "microsoft-azure", "google", "microsoft-free"]


def test_publish_drops_events_before_the_server_is_up():
    """uvicorn 还没起来时直接丢弃，绝不阻塞引擎线程。"""
    from chrometrans.cli import publish
    from chrometrans.server import EventBus

    publish(EventBus(), None, {"event": "cue", "data": {}})   # 不应抛异常

import httpx
import pytest

from chrometrans.translate.base import (
    TransientTranslationError,
    TranslationError,
)
from chrometrans.translate.microsoft import MicrosoftTranslator


def _client_returning(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_free_tier_builds_request_correctly(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json=[{"translations": [{"text": "你好"}]}])

    t = MicrosoftTranslator(api_key=None, region="global", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))

    import asyncio
    out = asyncio.run(t.translate(["hello"], "en", "zh-Hans"))
    assert out == ["你好"]
    assert "api-edge.cognitive.microsofttranslator.com" in seen["url"]
    assert "from=en" in seen["url"] and "to=zh-Hans" in seen["url"]


def test_azure_tier_uses_key_and_region(monkeypatch):
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=[{"translations": [{"text": "你好"}]}])

    t = MicrosoftTranslator(api_key="SECRET", region="eastasia", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))

    import asyncio
    asyncio.run(t.translate(["hello"], "en", "zh-Hans"))
    assert seen["headers"]["ocp-apim-subscription-key"] == "SECRET"
    assert seen["headers"]["ocp-apim-subscription-region"] == "eastasia"


def test_4xx_raises_deterministic_error(monkeypatch):
    def handler(request):
        return httpx.Response(400, json={"error": "bad"})

    t = MicrosoftTranslator(api_key="K", region="global", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))

    import asyncio
    with pytest.raises(TranslationError):
        asyncio.run(t.translate(["hello"], "en", "zh-Hans"))


def test_transport_error_raises_transient(monkeypatch):
    """C19：传输层错误必须是 transient，才能被重试而不是降级。"""
    def handler(request):
        raise httpx.ConnectError("connection reset")

    t = MicrosoftTranslator(api_key="K", region="global", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))

    import asyncio
    with pytest.raises(TransientTranslationError):
        asyncio.run(t.translate(["hello"], "en", "zh-Hans"))


def test_preserves_order_across_batch(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[
            {"translations": [{"text": "一"}]},
            {"translations": [{"text": "二"}]},
            {"translations": [{"text": "三"}]},
        ])

    t = MicrosoftTranslator(api_key="K", region="global", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))

    import asyncio
    out = asyncio.run(t.translate(["a", "b", "c"], "en", "zh-Hans"))
    assert out == ["一", "二", "三"]


def test_does_not_escape_payload(monkeypatch):
    """C20：转义必须成对或干脆不做。本实现选择不转义。"""
    from chrometrans.translate.microsoft import build_payload

    assert build_payload(["a < b & c"]) == [{"Text": "a < b & c"}]

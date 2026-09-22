import asyncio

import httpx
import pytest

from chrometrans.translate.base import TransientTranslationError
from chrometrans.translate.google import GoogleTranslator


def _client_returning(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_translates_batch_preserving_order(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={
            "data": {"translations": [{"translatedText": "一"},
                                      {"translatedText": "二"}]}
        })

    t = GoogleTranslator(api_key="K", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    out = asyncio.run(t.translate(["a", "b"], "en", "zh-Hans"))

    assert out == ["一", "二"]
    assert seen["body"].count("q=") == 2


def test_language_code_is_google_style(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"data": {"translations": [{"translatedText": "x"}]}})

    t = GoogleTranslator(api_key="K", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    asyncio.run(t.translate(["a"], "en", "zh-Hans"))

    assert "zh-CN" in seen["body"], "Google 用 zh-CN，不是 zh-Hans"


def test_transport_error_is_transient(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("reset")

    t = GoogleTranslator(api_key="K", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    with pytest.raises(TransientTranslationError):
        asyncio.run(t.translate(["a"], "en", "zh-Hans"))

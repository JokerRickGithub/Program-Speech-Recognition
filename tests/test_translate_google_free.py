import asyncio

import httpx
import pytest

from chrometrans.translate.base import (
    TransientTranslationError,
    TranslationError,
)
from chrometrans.translate.google import GoogleFreeTranslator


def _client_returning(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _gtx_body(*pairs):
    """gtx 的响应形状：[[[译文, 原文, ...], ...], null, "en", ...]。"""
    return [[[zh, en, None, None, 3] for zh, en in pairs], None, "en"]


def test_joins_segments_of_one_text(monkeypatch):
    """长句会被切成多段，必须拼回一句完整译文。"""
    def handler(request):
        return httpx.Response(200, json=_gtx_body(
            ("因此，将有九场", "So there are going to be"),
            ("一小时的讲座。", "nine one-hour lectures.")))

    t = GoogleFreeTranslator(timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    out = asyncio.run(t.translate(["So there are going to be nine one-hour lectures."],
                                  "en", "zh-Hans"))

    assert out == ["因此，将有九场一小时的讲座。"]


def test_one_request_per_text_because_gtx_ignores_extra_q(monkeypatch):
    """实测：重复的 q 参数只有第一个生效，所以必须一条文本一个请求。"""
    seen = []

    def handler(request):
        seen.append(dict(httpx.QueryParams(request.url.query.decode())))
        return httpx.Response(200, json=_gtx_body(("译", "src")))

    t = GoogleFreeTranslator(timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    asyncio.run(t.translate(["a", "b", "c"], "en", "zh-Hans"))

    assert len(seen) == 3, "三条文本必须是三个请求，不能指望一个请求里塞三个 q"
    assert [s["q"] for s in seen] == ["a", "b", "c"], "顺序必须保持"


def test_uses_get_with_gtx_client_and_google_language_code(monkeypatch):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["params"] = dict(httpx.QueryParams(request.url.query.decode()))
        return httpx.Response(200, json=_gtx_body(("译", "src")))

    t = GoogleFreeTranslator(timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    asyncio.run(t.translate(["a"], "en", "zh-Hans"))

    assert seen["method"] == "GET"
    assert seen["params"]["client"] == "gtx"
    assert seen["params"]["tl"] == "zh-CN", "Google 用 zh-CN，不是 zh-Hans"


def test_blank_text_is_not_sent_and_returns_empty(monkeypatch):
    """空/纯空白不该白白打一次网络请求。"""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=_gtx_body(("", "")))

    t = GoogleFreeTranslator(timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    out = asyncio.run(t.translate(["", "   "], "en", "zh-Hans"))

    assert out == ["", ""]
    assert calls == [], "纯空白必须短路，不该发请求"


def test_transport_error_is_transient(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("reset")

    t = GoogleFreeTranslator(timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    with pytest.raises(TransientTranslationError):
        asyncio.run(t.translate(["a"], "en", "zh-Hans"))


def test_an_empty_connect_error_still_says_something(monkeypatch):
    """实测的形状就是 `ConnectError('')`：str() 为空，报出来会是「翻译失败：」。

    走系统代理时 6 次里 2 次如此（2026-09-25 探针）。异常类名是唯一还能给的线索。
    """
    def handler(request):
        raise httpx.ConnectError("")

    t = GoogleFreeTranslator(timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    with pytest.raises(TransientTranslationError) as excinfo:
        asyncio.run(t.translate(["a"], "en", "zh-Hans"))

    assert str(excinfo.value).strip() == "ConnectError"


def test_500_and_429_are_transient_but_404_is_not(monkeypatch):
    def make(status):
        def handler(request):
            return httpx.Response(status, text="nope")
        return handler

    for status in (500, 429):
        t = GoogleFreeTranslator(timeout_s=5.0)
        monkeypatch.setattr(t, "_make_client",
                            lambda h=make(status): _client_returning(h))
        with pytest.raises(TransientTranslationError):
            asyncio.run(t.translate(["a"], "en", "zh-Hans"))

    t = GoogleFreeTranslator(timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(make(404)))
    with pytest.raises(TranslationError):
        asyncio.run(t.translate(["a"], "en", "zh-Hans"))


def test_unrecognised_body_is_a_translation_error(monkeypatch):
    """接口是未公开的，形状变了要报得清楚，不能抛 IndexError 之类的原始异常。"""
    def handler(request):
        return httpx.Response(200, json={"surprise": True})

    t = GoogleFreeTranslator(timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    with pytest.raises(TranslationError):
        asyncio.run(t.translate(["a"], "en", "zh-Hans"))

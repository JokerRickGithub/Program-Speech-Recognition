import asyncio
import json

import httpx
import pytest

from chrometrans.translate.base import (
    TransientTranslationError,
    TranslationError,
)
from chrometrans.translate.yandex import ENDPOINT, YandexTranslator


def _client_returning(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _capture(monkeypatch, t, handler):
    """把请求体与请求头都录下来，断言才有据可依。"""
    seen = {}

    def wrapper(request):
        seen["json"] = json.loads(request.content.decode())
        seen["headers"] = dict(request.headers)
        seen["url"] = str(request.url)
        return handler(request)

    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(wrapper))
    return seen


def _ok(*texts):
    def handler(request):
        return httpx.Response(200, json={
            "translations": [{"text": t, "detectedLanguageCode": "en"}
                             for t in texts]})
    return handler


def test_translates_a_batch_in_one_request(monkeypatch):
    """v2 的 texts 是数组，一次请求多条 —— 不必像免 key 谷歌那样一句一调。"""
    t = YandexTranslator(api_key="K", folder_id="F", timeout_s=5.0)
    seen = _capture(monkeypatch, t, _ok("你好", "世界"))

    out = asyncio.run(t.translate(["hello", "world"], "en", "zh-Hans"))

    assert out == ["你好", "世界"]
    assert seen["url"].startswith(ENDPOINT)
    assert seen["json"]["texts"] == ["hello", "world"]
    assert seen["json"]["sourceLanguageCode"] == "en"
    assert seen["json"]["targetLanguageCode"] == "zh", "Yandex 用 zh，不是 zh-Hans"
    assert seen["json"]["folderId"] == "F"


def test_sends_the_api_key_authorization_header(monkeypatch):
    t = YandexTranslator(api_key="secret-key", folder_id="F", timeout_s=5.0)
    seen = _capture(monkeypatch, t, _ok("你好"))

    asyncio.run(t.translate(["hello"], "en", "zh-Hans"))

    assert seen["headers"]["authorization"] == "Api-Key secret-key"


def test_folder_id_is_omitted_when_not_given(monkeypatch):
    """服务账号的 API key 不需要 folderId —— 文档说那种情况下由账号自己的目录决定。

    发一个空 folderId 过去可能被当成非法值，所以「没给就别发」比「发个空的」安全。
    """
    t = YandexTranslator(api_key="K", folder_id=None, timeout_s=5.0)
    seen = _capture(monkeypatch, t, _ok("你好"))

    asyncio.run(t.translate(["hello"], "en", "zh-Hans"))

    assert "folderId" not in seen["json"]


def test_transport_error_is_transient(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("")

    t = YandexTranslator(api_key="K", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    with pytest.raises(TransientTranslationError) as excinfo:
        asyncio.run(t.translate(["a"], "en", "zh-Hans"))
    assert str(excinfo.value).strip(), "空消息也要能报出点什么"


def test_429_and_5xx_are_transient_but_401_is_not(monkeypatch):
    """401/403 是「key 不对」，重试多少次都一样 —— 该降级，不该耗在重试上（C19）。"""
    def make(status):
        def handler(request):
            return httpx.Response(status, text="nope")
        return handler

    for status in (429, 500, 503):
        t = YandexTranslator(api_key="K", timeout_s=5.0)
        monkeypatch.setattr(t, "_make_client",
                            lambda h=make(status): _client_returning(h))
        with pytest.raises(TransientTranslationError):
            asyncio.run(t.translate(["a"], "en", "zh-Hans"))

    t = YandexTranslator(api_key="K", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(make(401)))
    with pytest.raises(TranslationError) as excinfo:
        asyncio.run(t.translate(["a"], "en", "zh-Hans"))
    assert not isinstance(excinfo.value, TransientTranslationError)
    assert "401" in str(excinfo.value)


def test_count_mismatch_is_a_translation_error(monkeypatch):
    """条数对不上宁可换引擎，也不能错位。"""
    t = YandexTranslator(api_key="K", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client",
                        lambda: _client_returning(_ok("只有一条")))
    with pytest.raises(TranslationError):
        asyncio.run(t.translate(["a", "b"], "en", "zh-Hans"))


def test_unrecognised_body_is_a_translation_error(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"surprise": True})

    t = YandexTranslator(api_key="K", timeout_s=5.0)
    monkeypatch.setattr(t, "_make_client", lambda: _client_returning(handler))
    with pytest.raises(TranslationError):
        asyncio.run(t.translate(["a"], "en", "zh-Hans"))


def test_empty_input_makes_no_request():
    t = YandexTranslator(api_key="K", timeout_s=5.0)
    assert asyncio.run(t.translate([], "en", "zh-Hans")) == []


def test_name_is_yandex():
    """链里按 name 分辨 provider，报错时也靠它指名道姓。"""
    assert YandexTranslator(api_key="K").name == "yandex"

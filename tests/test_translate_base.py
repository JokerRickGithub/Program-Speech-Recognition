import asyncio

import pytest

from chrometrans.config import TranslateConfig
from chrometrans.translate.base import (
    ChainTranslator,
    TransientTranslationError,
    TranslationError,
    describe_transport_error,
    retry_transient,
)


class StubTranslator:
    def __init__(self, name, behaviour):
        self.name = name
        self.behaviour = behaviour
        self.calls = 0

    async def translate(self, texts, src, tgt):
        self.calls += 1
        return self.behaviour(texts)


def run(coro):
    return asyncio.run(coro)


def test_chain_uses_first_provider_on_success():
    a = StubTranslator("a", lambda t: [f"A:{x}" for x in t])
    b = StubTranslator("b", lambda t: [f"B:{x}" for x in t])
    chain = ChainTranslator([a, b], TranslateConfig())

    out = run(chain.translate(["hello"], "en", "zh-Hans"))
    assert out == ["A:hello"]
    assert b.calls == 0


def test_chain_falls_through_on_deterministic_failure():
    def boom(texts):
        raise TranslationError("400 bad request")

    a = StubTranslator("a", boom)
    b = StubTranslator("b", lambda t: [f"B:{x}" for x in t])
    chain = ChainTranslator([a, b], TranslateConfig())

    assert run(chain.translate(["hello"], "en", "zh-Hans")) == ["B:hello"]


def test_transient_error_is_retried_not_downgraded():
    """C19：SSL EOF / 连接重置属瞬时抖动，直接降级会让每次抖动都静默换引擎。"""
    attempts = {"n": 0}

    def flaky(texts):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientTranslationError("SSL UNEXPECTED_EOF_WHILE_READING")
        return [f"A:{x}" for x in texts]

    a = StubTranslator("a", flaky)
    b = StubTranslator("b", lambda t: [f"B:{x}" for x in t])
    chain = ChainTranslator([a, b], TranslateConfig(max_retries=3))

    assert run(chain.translate(["hello"], "en", "zh-Hans")) == ["A:hello"]
    assert b.calls == 0, "瞬时错误耗尽重试前不得降级"


def test_chain_raises_when_every_provider_fails():
    """全军覆没必须抛，且要说清是谁、为什么。

    旧行为是返回 [None, None] —— 于是「翻译挂了」与「本句无需翻译」在返回值上
    长得一模一样，而唯一知道原因的这一层把原因丢了。用户报的「英语课很多话直接
    没有翻译」，在终端上就是一个字都没有（2026-09-25）。

    抛异常的代价只是通知方式变了：engine 照旧把该 cue 的译文存 None、原文照常
    落盘，用户看到的东西和从前一样，只是这回它会说一声。
    """
    def boom(texts):
        raise TranslationError("nope")

    chain = ChainTranslator([StubTranslator("a", boom),
                             StubTranslator("b", boom)], TranslateConfig())
    with pytest.raises(TranslationError) as excinfo:
        run(chain.translate(["x", "y"], "en", "zh-Hans"))

    message = str(excinfo.value)
    assert "a" in message and "b" in message, "要指名道姓，否则用户不知道该换哪条"
    assert "nope" in message, "原因也要带上"


def test_chain_raises_when_there_is_no_provider_at_all():
    """链是空的同样是失败，不能装成「这句不用翻译」。"""
    chain = ChainTranslator([], TranslateConfig())
    with pytest.raises(TranslationError):
        run(chain.translate(["x"], "en", "zh-Hans"))


def test_chain_reports_a_mismatched_count_as_a_reason():
    """条数对不上是「换下一家」而不是「成功」，全都不对时要报出来。"""
    chain = ChainTranslator(
        [StubTranslator("a", lambda t: [f"A:{x}" for x in t] + ["多余的"])],
        TranslateConfig())
    with pytest.raises(TranslationError) as excinfo:
        run(chain.translate(["x"], "en", "zh-Hans"))
    assert "条" in str(excinfo.value)


def test_describe_transport_error_handles_an_empty_message():
    """httpx 的 ConnectError('') 的 str() 是**空串**。

    2026-09-25 的探针里走代理 6 次有 2 次就是这个，于是「翻译失败：」后面什么
    都没有 —— 报错报了个寂寞。
    """
    import httpx

    assert describe_transport_error(httpx.ConnectError("")) == "ConnectError"
    assert describe_transport_error(
        httpx.ConnectError("reset by peer")) == "ConnectError: reset by peer"


def test_chain_returns_empty_for_empty_input():
    chain = ChainTranslator([], TranslateConfig())
    assert run(chain.translate([], "en", "zh-Hans")) == []


def test_retry_transient_gives_up_and_reraises():
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise TransientTranslationError("boom")

    with pytest.raises(TransientTranslationError):
        run(retry_transient(always_fail, max_retries=3, base_delay=0.0))
    assert calls["n"] == 3

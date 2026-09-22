import asyncio

import pytest

from chrometrans.config import TranslateConfig
from chrometrans.translate.base import (
    ChainTranslator,
    TransientTranslationError,
    TranslationError,
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


def test_chain_returns_none_per_item_when_all_fail():
    def boom(texts):
        raise TranslationError("nope")

    chain = ChainTranslator([StubTranslator("a", boom)], TranslateConfig())
    assert run(chain.translate(["x", "y"], "en", "zh-Hans")) == [None, None]


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

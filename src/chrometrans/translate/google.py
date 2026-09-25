"""Google Cloud Translation v2：Tier 2 兜底（spec §5.4）。"""
from __future__ import annotations

import httpx

from chrometrans.translate.base import (
    TransientTranslationError,
    TranslationError,
    describe_transport_error,
)

ENDPOINT = "https://translation.googleapis.com/language/translate/v2"

# Google 与 Microsoft 的语言代码不同（zh-Hans -> zh-CN）
_LANG_MAP = {"zh-Hans": "zh-CN", "zh-Hant": "zh-TW"}


def _google_lang(code: str) -> str:
    return _LANG_MAP.get(code, code)


class GoogleTranslator:
    def __init__(self, api_key: str, timeout_s: float = 10.0):
        self._key = api_key
        self._timeout_s = timeout_s
        self.name = "google"

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout_s)

    async def translate(self, texts: list[str], src: str, tgt: str) -> list[str]:
        if not texts:
            return []

        body = [("key", self._key), ("source", _google_lang(src)),
                ("target", _google_lang(tgt)), ("format", "text")]
        body += [("q", t) for t in texts]

        try:
            async with self._make_client() as client:
                resp = await client.post(
                    ENDPOINT,
                    content=str(httpx.QueryParams(body)).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            raise TransientTranslationError(describe_transport_error(exc)) from exc

        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientTranslationError(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise TranslationError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        items = resp.json()["data"]["translations"]
        if len(items) != len(texts):
            raise TranslationError(
                f"返回条数不匹配：请求 {len(texts)}，返回 {len(items)}")
        return [item["translatedText"] for item in items]


# 免 key 的谷歌翻译。**与上面那个是两回事**：上面是官方 Cloud Translation v2，要 key；
# 这个走的是 googletrans 一直在用的未公开端点，不需要 key。
# 2026-09-23 实测：本端点可用并译出正确中文；而 Edge 那条免 key 路
# （edge.microsoft.com/translate/auth）已 404 彻底下线，别再指望它。
# 未公开接口随时可能变或被限流，所以只当兜底 —— 带 key 的层永远排在它前面（C17）。
FREE_ENDPOINT = "https://translate.googleapis.com/translate_a/single"


class GoogleFreeTranslator:
    """免 key 谷歌。响应是嵌套数组，不是 JSON 对象，解析方式与上面完全不同。"""

    def __init__(self, timeout_s: float = 10.0):
        self._timeout_s = timeout_s
        self.name = "google-free"

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout_s)

    async def translate(self, texts: list[str], src: str, tgt: str) -> list[str]:
        if not texts:
            return []

        # 实测：重复的 q 参数只有第一个生效，后面的直接被丢掉，所以一条文本一个请求。
        # 引擎本来就是一句一调（engine.py 的 _translate），这里不会额外放大请求数。
        async with self._make_client() as client:
            return [await self._translate_one(client, t, src, tgt) for t in texts]

    async def _translate_one(self, client: httpx.AsyncClient, text: str,
                             src: str, tgt: str) -> str:
        if not text.strip():
            return ""                      # 纯空白没必要白打一次网络

        params = {"client": "gtx", "sl": _google_lang(src),
                  "tl": _google_lang(tgt), "dt": "t", "q": text}
        try:
            resp = await client.get(FREE_ENDPOINT, params=params)
        except httpx.HTTPError as exc:
            raise TransientTranslationError(describe_transport_error(exc)) from exc

        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientTranslationError(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise TranslationError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        # 长句会被切成多段，拼回一句完整译文
        try:
            segments = resp.json()[0]
            return "".join(seg[0] for seg in segments if seg and seg[0])
        except Exception as exc:
            # 未公开接口，形状变了要报得清楚，别把 IndexError 漏给上层
            raise TranslationError(f"响应结构不认识：{resp.text[:200]}") from exc

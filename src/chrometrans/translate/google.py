"""Google Cloud Translation v2：Tier 2 兜底（spec §5.4）。"""
from __future__ import annotations

import httpx

from chrometrans.translate.base import (
    TransientTranslationError,
    TranslationError,
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
            raise TransientTranslationError(str(exc)) from exc

        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientTranslationError(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise TranslationError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        items = resp.json()["data"]["translations"]
        if len(items) != len(texts):
            raise TranslationError(
                f"返回条数不匹配：请求 {len(texts)}，返回 {len(items)}")
        return [item["translatedText"] for item in items]

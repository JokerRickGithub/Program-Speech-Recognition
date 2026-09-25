"""Microsoft Translator：Tier 0 免 key + Tier 1 Azure Key（spec §5.4）。"""
from __future__ import annotations

import httpx

from chrometrans.translate.base import (
    TransientTranslationError,
    TranslationError,
    describe_transport_error,
)

FREE_ENDPOINT = "https://api-edge.cognitive.microsofttranslator.com/translate"
AZURE_ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate"
API_VERSION = "3.0"


def build_payload(texts: list[str]) -> list[dict]:
    """构建请求体。刻意不做 html.escape（C20：转义必须成对，不做最省事）。"""
    return [{"Text": t} for t in texts]


class MicrosoftTranslator:
    def __init__(self, api_key: str | None = None, region: str = "global",
                 timeout_s: float = 10.0):
        self._key = api_key
        self._region = region
        self._timeout_s = timeout_s
        self.name = "microsoft-azure" if api_key else "microsoft-free"

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout_s)

    @property
    def _endpoint(self) -> str:
        return AZURE_ENDPOINT if self._key else FREE_ENDPOINT

    async def translate(self, texts: list[str], src: str, tgt: str) -> list[str]:
        if not texts:
            return []

        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Ocp-Apim-Subscription-Key"] = self._key
            headers["Ocp-Apim-Subscription-Region"] = self._region

        params = {"api-version": API_VERSION, "from": src, "to": tgt}

        try:
            async with self._make_client() as client:
                resp = await client.post(self._endpoint, params=params,
                                         headers=headers,
                                         json=build_payload(texts))
        except httpx.HTTPError as exc:
            # 连接重置 / SSL EOF 等瞬时抖动 → 重试而非降级（C19）
            raise TransientTranslationError(describe_transport_error(exc)) from exc

        if 500 <= resp.status_code or resp.status_code == 429:
            raise TransientTranslationError(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise TranslationError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        if len(data) != len(texts):
            raise TranslationError(
                f"返回条数不匹配：请求 {len(texts)}，返回 {len(data)}")
        return [item["translations"][0]["text"] for item in data]

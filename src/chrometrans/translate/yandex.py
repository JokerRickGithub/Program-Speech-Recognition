"""Yandex Translate v2（spec §5.4 的 Tier 3）。

2026-09-25 实测可达性：**直连 6/6 通**（走系统代理 4/6）。这一点与免 key 谷歌相反
（直连 0/6），所以它是「人在俄罗斯、网课必须开 VPN」这个场景下值得有的一层。

一个与别家不同的地方：`folderId` 只在**用户账号**（IAM token）认证下才是必需的；
用服务账号的 API key 时，目录由账号本身决定，文档明确说不必在请求里给。所以这里
`folder_id` 可为 None，且**没给就不发这个字段** —— 发个空的过去有可能被当成非法值。
"""
from __future__ import annotations

import httpx

from chrometrans.translate.base import (
    TransientTranslationError,
    TranslationError,
    describe_transport_error,
)

ENDPOINT = "https://translate.api.cloud.yandex.net/translate/v2/translate"

# Yandex 用 "zh"（简体中文）；与 Azure/Google 的写法都不同，理由同 google.py
# 里那张表：语言代码是各家自己的方言，别指望统一。
_LANG_MAP = {"zh-Hans": "zh"}


def _yandex_lang(code: str) -> str:
    return _LANG_MAP.get(code, code)


class YandexTranslator:
    def __init__(self, api_key: str, folder_id: str | None = None,
                 timeout_s: float = 10.0):
        self._key = api_key
        self._folder_id = folder_id
        self._timeout_s = timeout_s
        self.name = "yandex"

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout_s)

    async def translate(self, texts: list[str], src: str, tgt: str) -> list[str]:
        if not texts:
            return []

        body: dict = {
            "texts": list(texts),
            # 显式给源语言：文档建议如此，免得像 "angel" 这类词被自动识别带偏。
            # 本项目本来就知道源语言（语言画像里的 translate_src）。
            "sourceLanguageCode": _yandex_lang(src),
            "targetLanguageCode": _yandex_lang(tgt),
        }
        if self._folder_id:
            body["folderId"] = self._folder_id

        try:
            async with self._make_client() as client:
                resp = await client.post(
                    ENDPOINT, json=body,
                    headers={"Authorization": f"Api-Key {self._key}",
                             "Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            # 连接重置 / SSL EOF 等瞬时抖动 → 重试而非降级（C19）
            raise TransientTranslationError(describe_transport_error(exc)) from exc

        if 500 <= resp.status_code or resp.status_code == 429:
            raise TransientTranslationError(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            # 401/403（key 不对）落在这一支：重试多少次都一样，该降级
            raise TranslationError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            items = resp.json()["translations"]
            out = [item["text"] for item in items]
        except Exception as exc:
            raise TranslationError(f"响应结构不认识：{resp.text[:200]}") from exc
        if len(out) != len(texts):
            raise TranslationError(
                f"返回条数不匹配：请求 {len(texts)}，返回 {len(out)}")
        return out

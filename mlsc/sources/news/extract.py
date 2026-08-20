"""Extracts article text from a resolved publisher URL, via trafilatura."""

from __future__ import annotations

from importlib.metadata import version
from typing import Protocol

import httpx
import trafilatura

LIBRARY_VERSION = version("trafilatura")


class UnextractableArticle(RuntimeError):
    """The publisher page yielded no usable text."""


class ArticleExtractor(Protocol):
    async def extract(self, url: str) -> str: ...


class TrafilaturaExtractor:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def extract(self, url: str) -> str:
        try:
            response = await self._client.get(url, timeout=10.0)
        except httpx.HTTPError as error:
            raise UnextractableArticle(url) from error
        text = trafilatura.extract(response.text)
        if not text:
            raise UnextractableArticle(url)
        return text

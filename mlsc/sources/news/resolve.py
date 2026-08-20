"""Resolves a Google News redirect URL to its publisher URL.

Injected as its own collaborator (design.md, "Dependencies, injected") because
it fails for a different reason than article extraction: a bad redirect is a
Google-side problem, a bad extraction is a publisher-side one.
"""

from __future__ import annotations

from typing import Protocol

import httpx


class UnresolvableRedirect(RuntimeError):
    """A Google News link did not resolve to a publisher URL."""


class RedirectResolver(Protocol):
    async def resolve(self, google_news_url: str) -> str: ...


class HttpxRedirectResolver:
    """Follows the plain HTTP redirect; sufficient for pre-2024-style links.

    Post-2024 Google News links require a signed batchexecute RPC call to
    resolve and are not handled here — they surface as UnresolvableRedirect
    and are counted as filtered per design.md's failure strategy.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def resolve(self, google_news_url: str) -> str:
        try:
            response = await self._client.get(google_news_url, follow_redirects=True, timeout=10.0)
        except httpx.HTTPError as error:
            raise UnresolvableRedirect(google_news_url) from error
        final_url = str(response.url)
        if "news.google.com" in final_url:
            raise UnresolvableRedirect(google_news_url)
        return final_url

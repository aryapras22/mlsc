"""The abstract base every source adapter extends.

The only way an adapter can reach a transport is through the ``FetchClient``
injected into its constructor — there is no other path to a transport, so an
adapter cannot create its own client or bypass the guardrails (requirement 1).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from mlsc.core.fetch.client import FetchClient
from mlsc.core.fetch.contracts import FetchExpectations


class DiscoveryNotSupported(RuntimeError):
    """Raised by ``discover`` for an adapter over a surface with no search
    capability. Not every source can be searched for candidates by a query
    (theme-monitors design.md, "the surfaces that support it") — the default
    implementation raises this rather than forcing every adapter to override
    a method most of them cannot meaningfully implement."""


class SourceAdapter(ABC):
    """One adapter per source, holding only a ``FetchClient`` and its expectations."""

    def __init__(self, fetch_client: FetchClient) -> None:
        self._fetch_client = fetch_client

    @property
    @abstractmethod
    def expectations(self) -> FetchExpectations:
        """What a healthy response from this source looks like, declared as data."""

    @abstractmethod
    async def fetch(self, entity: str, cursor: Any, quota: int) -> Any:
        """Fetch and parse one page of validated rows for ``entity`` starting at ``cursor``."""

    async def discover(self, query: str) -> list[Any]:
        """Search this surface for candidate entities matching ``query``.

        Only adapters over a genuinely searchable, anonymous surface override
        this (theme-monitors requirement 3); every other adapter raises
        ``DiscoveryNotSupported``.
        """
        raise DiscoveryNotSupported(type(self).__name__)

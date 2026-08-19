"""Plain and TLS-impersonating transports, selected by client profile.

Ordinary hosts use ``httpx``. Hosts that fingerprint clients at the TLS handshake
use ``curl_cffi`` with a browser impersonation profile — headers alone cannot
defeat that fingerprinting, only a matching handshake can.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import curl_cffi
import httpx

from mlsc.core.fetch.contracts import ClientProfile, FetchRequest, TransportFailure


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    content_type: str
    body: bytes
    library_version: str


class Transport(Protocol):
    async def send(self, request: FetchRequest) -> TransportResponse: ...


class PlainTransport:
    """httpx for hosts that do not fingerprint the TLS handshake."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(self, request: FetchRequest) -> TransportResponse:
        try:
            response = await self._client.request(
                request.method,
                request.url,
                params=list(request.query),
                headers=dict(request.headers),
                content=request.body,
            )
        except httpx.HTTPError as error:
            raise TransportFailure(host_key=request.host_key, cause=error) from error
        return TransportResponse(
            status_code=response.status_code,
            content_type=response.headers.get("content-type", ""),
            body=response.content,
            library_version=f"httpx/{httpx.__version__}",
        )


class ImpersonatingTransport:
    """curl_cffi with browser TLS impersonation for hosts that fingerprint clients."""

    def __init__(self, session: curl_cffi.requests.AsyncSession, *, impersonate: str) -> None:
        self._session = session
        self._impersonate = impersonate

    async def send(self, request: FetchRequest) -> TransportResponse:
        try:
            response = await self._session.request(
                request.method,
                request.url,
                params=list(request.query),
                headers=dict(request.headers),
                data=request.body,
                impersonate=self._impersonate,
            )
        except curl_cffi.CurlError as error:
            raise TransportFailure(host_key=request.host_key, cause=error) from error
        return TransportResponse(
            status_code=response.status_code,
            content_type=response.headers.get("content-type", ""),
            body=response.content,
            library_version=f"curl_cffi/{curl_cffi.__version__}",
        )


def select_transport(
    profile: ClientProfile, *, plain: Transport, impersonating: Transport
) -> Transport:
    if profile is ClientProfile.PLAIN:
        return plain
    return impersonating

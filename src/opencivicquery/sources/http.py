"""One HTTP helper for connectors that call a fixed public host."""

from __future__ import annotations

import json
from typing import Any

import httpx

from opencivicquery import __version__
from opencivicquery.sources.base import SourceError, SourceQueryError

DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class FixedHostClient:
    """Send requests to URLs the connector builds itself, never to a caller's URL.

    Redirects are not followed, so a source cannot steer a request to another host.
    Responses are read in chunks and refused past a size cap.
    """

    def __init__(
        self,
        *,
        source_name: str,
        client: httpx.AsyncClient | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        user_agent: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.source_name = source_name
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds), follow_redirects=False
        )
        self._owns_client = client is None
        self._max_response_bytes = max_response_bytes
        self._headers = {
            "Accept": "application/json",
            "User-Agent": user_agent or f"OpenCivicQuery/{__version__}",
        }

    async def send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        """Return the status code and body. Network failures raise SourceError."""
        status, _, content = await self.send_with_headers(
            method, url, params=params, json_body=json_body, headers=headers
        )
        return status, content

    async def send_with_headers(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, httpx.Headers, bytes]:
        """Return the status code, response headers, and body.

        headers override the defaults for this request only. A request carries only the
        cookies its caller names: the client keeps whatever cookies sources set, and one
        caller's answer must never shape another caller's request.
        """
        request = self._client.build_request(
            method,
            url,
            params=params,
            json=json_body,
            headers={**self._headers, **(headers or {})},
        )
        if not any(name.lower() == "cookie" for name in headers or {}):
            request.headers.pop("cookie", None)
        try:
            response = await self._client.send(request, stream=True)
            try:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self._max_response_bytes:
                        raise SourceError(
                            f"The {self.source_name} response exceeded the size limit"
                        )
                return response.status_code, response.headers, bytes(content)
            finally:
                await response.aclose()
        except httpx.TimeoutException as exc:
            raise SourceQueryError(
                f"The {self.source_name} did not answer in time.",
                code="SOURCE_TIMEOUT",
                public_message=f"The {self.source_name} did not answer in time. Try again.",
            ) from exc
        except httpx.RequestError as exc:
            raise SourceError(f"The {self.source_name} request failed") from exc

    async def json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Return the parsed JSON body of a 200 answer, or raise a typed error.

        HTTP 400 and 422 mean the source rejected the request, so they carry the
        source's own reason. Any other status means the source could not answer.
        """
        status, content = await self.send(method, url, params=params, json_body=json_body)
        try:
            payload = json.loads(content)
        except ValueError:
            payload = None
        if status == 200:
            if payload is None:
                raise SourceError(f"The {self.source_name} returned a body that is not JSON")
            return payload
        detail = source_reason(payload)
        raise SourceQueryError(
            f"{self.source_name} HTTP {status}: {detail}",
            code="QUERY_REJECTED" if status in {400, 422} else "SOURCE_UNAVAILABLE",
            http_status=status,
            public_message=(
                detail
                if detail and status in {400, 422}
                else f"The {self.source_name} could not complete the request (HTTP {status})."
            ),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def source_reason(payload: Any) -> str:
    """The short reason a source gave for refusing a request, capped at 300 characters."""
    if not isinstance(payload, dict):
        return ""
    for key in ("errors", "error", "detail", "message", "messages", "Message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value[:300]
        if isinstance(value, list):
            return " ".join(item for item in value if isinstance(item, str))[:300]
    return ""

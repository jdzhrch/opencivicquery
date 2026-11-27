from __future__ import annotations

import tomllib
from pathlib import Path

import httpx
import pytest

from opencivicquery import PROJECT_URL, __version__
from opencivicquery.sources.base import SourceError, SourceQueryError
from opencivicquery.sources.http import FixedHostClient


@pytest.mark.asyncio
async def test_a_redirect_is_never_followed_even_when_the_given_client_allows_it() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "fixed.example":
            return httpx.Response(301, headers={"Location": "https://elsewhere.example/x"})
        return httpx.Response(200, json={"from": "elsewhere"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    fixed = FixedHostClient(source_name="Example API", client=client)

    status, content = await fixed.send("GET", "https://fixed.example/data")
    assert status == 301 and content == b""
    with pytest.raises(SourceQueryError) as caught:
        await fixed.json("GET", "https://fixed.example/data")

    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert caught.value.http_status == 301
    assert hosts == ["fixed.example", "fixed.example"]
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 500])
async def test_a_body_nested_past_the_parser_limit_is_a_typed_error(status: int) -> None:
    body = "[" * 100_000 + "]" * 100_000
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, text=body))
    )
    fixed = FixedHostClient(source_name="Example API", client=client)

    with pytest.raises(SourceError) as caught:
        await fixed.json("GET", "https://fixed.example/data")

    # A 200 with an unreadable body is a source error; any other status keeps its own code.
    assert isinstance(caught.value, SourceQueryError) == (status != 200)
    await client.aclose()


@pytest.mark.asyncio
async def test_the_default_identity_names_the_project_and_its_public_repository() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await FixedHostClient(source_name="Example API", client=client).json(
        "GET", "https://fixed.example/data"
    )

    expected = f"OpenCivicQuery/{__version__} (+https://github.com/jdzhrch/opencivicquery)"
    assert [request.headers["User-Agent"] for request in sent] == [expected]
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert tomllib.loads(pyproject.read_text())["project"]["urls"]["Repository"] == PROJECT_URL
    await client.aclose()

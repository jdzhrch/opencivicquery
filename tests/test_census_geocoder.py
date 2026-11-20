from __future__ import annotations

import copy
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from opencivicquery.models import Completeness
from opencivicquery.sources.base import SourceError, SourceQueryError
from opencivicquery.sources.census_geocoder import (
    APPROXIMATE_WARNING,
    DISTRICT_WARNING,
    FRESHNESS_WARNING,
    GEOCODER_HOST,
    MATCHED_ADDRESS_WARNING,
    MULTIPLE_MATCH_WARNING,
    NO_MATCH_WARNING,
    VINTAGE_WARNING,
    CensusGeocoder,
)

ADDRESS = "1 Dr Carlton B Goodlett Pl, San Francisco, CA 94102"
INPUT = {
    "address": {"address": ADDRESS},
    "vintage": {
        "isDefault": True,
        "id": "4",
        "vintageName": "Current_Current",
        "vintageDescription": "Current Vintage - Current Benchmark",
    },
    "benchmark": {
        "isDefault": True,
        "benchmarkDescription": "Public Address Ranges - Current Benchmark",
        "id": "4",
        "benchmarkName": "Public_AR_Current",
    },
}
# Trimmed from a live response on 2026-11-20.
MATCH = {
    "tigerLine": {"side": "L", "tigerLineId": "635441232"},
    "geographies": {
        "States": [{"GEOID": "06", "NAME": "California", "STUSAB": "CA", "AREALAND": 1}],
        "Counties": [{"GEOID": "06075", "NAME": "San Francisco County", "COUNTY": "075"}],
        "Census Tracts": [{"GEOID": "06075012405", "NAME": "Census Tract 124.05"}],
        "2020 Census Blocks": [{"GEOID": "060750124053007", "NAME": "Block 3007"}],
    },
    "coordinates": {"x": -122.41831039539, "y": 37.778536095745},
    "addressComponents": {"zip": "94102", "streetName": "CARLTON P GOODLETT"},
    "matchedAddress": "1 DR CARLTON P GOODLETT PL, SAN FRANCISCO, CA, 94102",
}


def payload(*matches: dict[str, Any], source_input: dict[str, Any] | None = None) -> dict:
    return {"result": {"input": source_input or INPUT, "addressMatches": list(matches)}}


def geocoder_answering(
    response: httpx.Response | Exception,
) -> tuple[CensusGeocoder, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CensusGeocoder(client=client), requests


@pytest.mark.asyncio
async def test_one_match_keeps_identifying_fields_in_the_source_names() -> None:
    geocoder, requests = geocoder_answering(httpx.Response(200, json=payload(MATCH)))

    result = await geocoder.geocode(ADDRESS)

    assert result.rows == [
        {
            "matchedAddress": "1 DR CARLTON P GOODLETT PL, SAN FRANCISCO, CA, 94102",
            "coordinates": {"x": -122.41831039539, "y": 37.778536095745},
            "tigerLine": {"tigerLineId": "635441232", "side": "L"},
            "geographies": {
                "States": [{"GEOID": "06", "NAME": "California"}],
                "Counties": [{"GEOID": "06075", "NAME": "San Francisco County"}],
                "Census Tracts": [{"GEOID": "06075012405", "NAME": "Census Tract 124.05"}],
                "2020 Census Blocks": [{"GEOID": "060750124053007", "NAME": "Block 3007"}],
            },
        }
    ]
    assert result.returned_row_count == 1
    assert result.completeness == Completeness.COMPLETE
    assert result.domain == GEOCODER_HOST
    assert result.dataset_id == "Public_AR_Current/Current_Current"
    assert result.dataset_name == (
        "Census Geocoder: Public Address Ranges - Current Benchmark; "
        "Current Vintage - Current Benchmark"
    )
    assert result.source_url == "https://geocoding.geo.census.gov/geocoder/"
    assert result.source_updated_at is None
    assert len(requests) == 1
    sent = requests[0]
    assert sent.method == "GET"
    assert sent.url.host == GEOCODER_HOST
    assert sent.url.path == "/geocoder/geographies/onelineaddress"
    assert sent.headers["User-Agent"].startswith("OpenCivicQuery/")
    assert dict(sent.url.params) == {
        "address": ADDRESS,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    }
    # The public link reruns exactly the request that was sent.
    assert result.query_url is not None
    assert parse_qs(urlsplit(result.query_url).query) == {
        key: [value] for key, value in sent.url.params.items()
    }
    assert result.query_url.startswith(
        "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress?"
    )
    assert result.query == urlsplit(result.query_url).query


@pytest.mark.asyncio
async def test_layer_names_feed_the_schema_hash() -> None:
    geocoder, _ = geocoder_answering(httpx.Response(200, json=payload(MATCH)))
    first = await geocoder.geocode(ADDRESS)
    fewer = copy.deepcopy(MATCH)
    del fewer["geographies"]["2020 Census Blocks"]
    geocoder, _ = geocoder_answering(httpx.Response(200, json=payload(fewer)))
    second = await geocoder.geocode(ADDRESS)

    assert first.schema_sha256 != second.schema_sha256


@pytest.mark.asyncio
async def test_a_pasted_multi_line_address_is_sent_as_one_line() -> None:
    geocoder, requests = geocoder_answering(httpx.Response(200, json=payload(MATCH)))

    await geocoder.geocode("  1 Dr Carlton B Goodlett Pl,\n San Francisco,\tCA 94102 ")

    assert requests[0].url.params["address"] == ADDRESS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "",
        "   ",
        "x" * 101,
        "1 Main St\x00",
        "1 Main St\x1b[2J",
        "1 Main\u200b St",
        "1 Main St\u202e",
        "1 Main St\x9b",
    ],
)
async def test_an_invalid_address_never_reaches_the_source(address: str) -> None:
    geocoder, requests = geocoder_answering(httpx.Response(200, json=payload(MATCH)))

    with pytest.raises(SourceQueryError) as caught:
        await geocoder.geocode(address)

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert caught.value.argument == "address"
    assert requests == []


@pytest.mark.asyncio
async def test_the_source_rejection_reason_reaches_the_caller() -> None:
    geocoder, _ = geocoder_answering(
        httpx.Response(400, json={"errors": ["Invalid benchmark in request"], "status": "400"})
    )

    with pytest.raises(SourceQueryError) as caught:
        await geocoder.geocode(ADDRESS)

    assert caught.value.tool_error() == {
        "code": "QUERY_REJECTED",
        "message": "Invalid benchmark in request",
        "argument": None,
        "http_status": 400,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="<html>Service Unavailable</html>"),
        httpx.Response(302, headers={"Location": "https://example.com/elsewhere"}),
    ],
)
async def test_an_unavailable_source_is_reported_without_following_it(
    response: httpx.Response,
) -> None:
    geocoder, requests = geocoder_answering(response)

    with pytest.raises(SourceQueryError) as caught:
        await geocoder.geocode(ADDRESS)

    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert caught.value.http_status == response.status_code
    assert caught.value.public_message == (
        f"The Census Geocoder could not complete the request (HTTP {response.status_code})."
    )
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"result": {"input": {}}}',
        b'{"result": {"addressMatches": "none"}}',
        b'{"result": {"addressMatches": ["not an object"]}}',
        b"[]",
        # No input block, so the benchmark and vintage are unconfirmed.
        b'{"result": {"addressMatches": []}}',
    ],
)
async def test_a_malformed_answer_is_a_source_error(body: bytes) -> None:
    geocoder, _ = geocoder_answering(httpx.Response(200, content=body))

    with pytest.raises(SourceError) as caught:
        await geocoder.geocode(ADDRESS)

    assert not isinstance(caught.value, SourceQueryError)


@pytest.mark.asyncio
async def test_an_answer_for_a_different_vintage_is_never_accepted() -> None:
    other = copy.deepcopy(INPUT)
    other["vintage"]["vintageName"] = "Census2020_Current"
    geocoder, _ = geocoder_answering(httpx.Response(200, json=payload(MATCH, source_input=other)))

    with pytest.raises(SourceError, match="did not confirm the benchmark and vintage"):
        await geocoder.geocode(ADDRESS)


@pytest.mark.asyncio
async def test_an_oversized_answer_is_refused() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"x" * 2048)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    geocoder = CensusGeocoder(client=client, max_response_bytes=1024)

    with pytest.raises(SourceError, match="size limit"):
        await geocoder.geocode(ADDRESS)


@pytest.mark.asyncio
async def test_a_network_failure_is_a_source_error() -> None:
    geocoder, _ = geocoder_answering(httpx.ConnectError("refused"))

    with pytest.raises(SourceError) as caught:
        await geocoder.geocode(ADDRESS)

    assert not isinstance(caught.value, SourceQueryError)


@pytest.mark.asyncio
async def test_a_stalled_source_is_a_timeout_the_caller_can_retry() -> None:
    geocoder, _ = geocoder_answering(httpx.ReadTimeout("stalled"))

    with pytest.raises(SourceQueryError) as caught:
        await geocoder.geocode(ADDRESS)

    assert caught.value.code == "SOURCE_TIMEOUT"
    assert "Try again" in caught.value.public_message


@pytest.mark.asyncio
async def test_warnings_depend_on_how_many_addresses_matched() -> None:
    always = [APPROXIMATE_WARNING, DISTRICT_WARNING, VINTAGE_WARNING, FRESHNESS_WARNING]
    cases = {
        0: [NO_MATCH_WARNING, *always],
        1: [MATCHED_ADDRESS_WARNING, *always],
        3: [MULTIPLE_MATCH_WARNING, MATCHED_ADDRESS_WARNING, *always],
    }
    for count, expected in cases.items():
        geocoder, _ = geocoder_answering(httpx.Response(200, json=payload(*[MATCH] * count)))
        result = await geocoder.geocode(ADDRESS)
        assert result.returned_row_count == count
        assert geocoder.warnings_for(result) == expected


@pytest.mark.asyncio
async def test_closing_leaves_a_borrowed_client_open() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    await CensusGeocoder(client=client).close()
    assert client.is_closed is False
    await client.aclose()

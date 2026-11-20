from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from opencivicquery.logging_redaction import REDACTED, SecretRedactingFilter, scrub
from opencivicquery.models import Completeness
from opencivicquery.sources.base import SourceError, SourceQueryError
from opencivicquery.sources.census_data import (
    ANNOTATION_RULE,
    ANNOTATION_WARNING,
    CENSUS_DATA_HOST,
    KEY_WARNING,
    MAX_CODES,
    MAX_ROWS,
    MAX_VARIABLES,
    MOE_WARNING,
    SOURCE_PAGE,
    TERMS_NOTICE,
    CensusData,
)

KEY = "test-key-5f1d0c"

# Trimmed from live 2024 ACS 5-year responses on 2026-11-20.
HEADER = ["NAME", "B01003_001E", "B19013_001E", "B19013_001M", "state", "county", "tract"]
TRACT = [
    "Census Tract 101.01; San Francisco County; California",
    "1911",
    "89022",
    "40627",
    "06",
    "075",
    "010101",
]
PARK = [
    "Census Tract 9803; San Francisco County; California",
    "0",
    "-666666666",
    "-222222222",
    "06",
    "075",
    "980300",
]
METADATA = {
    "NAME": {
        "name": "NAME",
        "label": "Geographic Area Name",
        "concept": "Sex by Age;Total Population",
        "group": "B01001,B01003",
    },
    "B01003_001E": {
        "name": "B01003_001E",
        "label": "Estimate!!Total",
        "concept": "Total Population",
        "group": "B01003",
        "attributes": "B01003_001EA,B01003_001M,B01003_001MA",
    },
    "B01003_001M": {
        "name": "B01003_001M",
        "label": "Margin of Error!!Total",
        "concept": "Total Population",
        "group": "B01003",
        "attributes": "B01003_001EA,B01003_001MA",
    },
    "B19013_001E": {
        "name": "B19013_001E",
        "label": "Estimate!!Median household income in the past 12 months "
        "(in 2024 inflation-adjusted dollars)",
        "concept": "Median Household Income in the Past 12 Months "
        "(in 2024 Inflation-Adjusted Dollars)",
        "group": "B19013",
        "attributes": "B19013_001EA,B19013_001M,B19013_001MA",
    },
    "B19013_001M": {
        "name": "B19013_001M",
        "label": "Margin of Error!!Median household income in the past 12 months "
        "(in 2024 inflation-adjusted dollars)",
        "concept": "Median Household Income in the Past 12 Months "
        "(in 2024 Inflation-Adjusted Dollars)",
        "group": "B19013",
        "attributes": "B19013_001EA,B19013_001MA",
    },
}
VARIABLES = ["NAME", "B01003_001E", "B19013_001E", "B19013_001M"]


def census_answering(
    data: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str | None = KEY,
    extra_metadata: dict[str, dict[str, Any]] | None = None,
) -> tuple[CensusData, list[httpx.Request]]:
    """A CensusData whose data requests go to `data` and metadata requests to METADATA."""
    requests: list[httpx.Request] = []
    metadata = METADATA | (extra_metadata or {})

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/variables/" in request.url.path:
            name = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
            if name in metadata:
                return httpx.Response(200, json=metadata[name])
            return httpx.Response(404, text="<!doctype html><html>Not Found</html>")
        return data(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CensusData(api_key=api_key, client=client), requests


def always(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: response


def table(*rows: list[str | None], header: list[str] = HEADER) -> httpx.Response:
    return httpx.Response(200, json=[header, *rows])


def prepare(census: CensusData, **overrides: Any):
    arguments: dict[str, Any] = {
        "year": 2024,
        "variables": VARIABLES,
        "geography_for": "tract:*",
        "geography_in": "state:06 county:075",
    }
    arguments.update(overrides)
    return census.get_data(**arguments)


def assert_no_key(error: BaseException) -> None:
    """The key appears in no message, public error, or chained cause."""
    seen: BaseException | None = error
    while seen is not None:
        assert KEY not in str(seen)
        assert KEY not in repr(seen)
        seen = seen.__cause__ or seen.__context__
    if isinstance(error, SourceQueryError):
        assert KEY not in json.dumps(error.tool_error())


@pytest.mark.asyncio
async def test_full_lookup_sends_the_key_and_keeps_it_out_of_the_lookup() -> None:
    census, requests = census_answering(always(table(TRACT, PARK)))

    prepared = prepare(census, variables=[" name ", "b01003_001e", "B19013_001E", "B19013_001M"])
    found = await prepared.run()

    data_request, *metadata_requests = requests
    assert data_request.url.host == CENSUS_DATA_HOST
    assert data_request.url.path == "/data/2024/acs/acs5"
    assert data_request.headers["User-Agent"].startswith("OpenCivicQuery/")
    assert dict(data_request.url.params) == {
        "get": "NAME,B01003_001E,B19013_001E,B19013_001M",
        "for": "tract:*",
        "in": "state:06 county:075",
        "key": KEY,
    }
    assert sorted(request.url.path for request in metadata_requests) == sorted(
        f"/data/2024/acs/acs5/variables/{name}.json" for name in VARIABLES
    )
    assert all("key" not in request.url.params for request in metadata_requests)

    result = found.result
    assert result.query_url == f"https://{CENSUS_DATA_HOST}{result.query}"
    assert str(data_request.url) == f"{result.query_url}&key={KEY}"
    assert result.query == (
        "/data/2024/acs/acs5?get=NAME%2CB01003_001E%2CB19013_001E%2CB19013_001M"
        "&for=tract%3A%2A&in=state%3A06+county%3A075"
    )
    assert result.rows[0] == dict(zip(HEADER, TRACT, strict=True))
    assert result.returned_row_count == 2
    assert result.completeness == Completeness.COMPLETE
    assert result.domain == CENSUS_DATA_HOST
    assert result.source_url == SOURCE_PAGE
    assert result.dataset_id == "2024/acs/acs5"
    assert "2020-2024" in result.dataset_name
    assert "B01003_001E = Estimate!!Total (Total Population)" in result.dataset_name
    assert "NAME = Geographic Area Name;" in result.dataset_name
    assert "Sex by Age" not in result.dataset_name
    assert "tract level" in found.row_grain
    assert prepared.host == CENSUS_DATA_HOST
    assert prepared.arguments == {
        "year": 2024,
        "dataset": "acs/acs5",
        "variables": VARIABLES,
        "geography_for": "tract:*",
        "geography_in": "state:06 county:075",
    }
    assert KEY_WARNING in found.warnings
    assert TERMS_NOTICE in found.warnings

    frozen = json.dumps(
        {
            "lookup": dataclasses.asdict(found) | {"result": result.model_dump(mode="json")},
            "arguments": prepared.arguments,
        }
    )
    assert KEY not in frozen
    assert KEY not in repr(census.__dict__)


@pytest.mark.asyncio
async def test_moe_warning_names_the_margins_of_error_left_out() -> None:
    header = ["NAME", "B01003_001E", "B19013_001E", "state", "county"]
    row = ["San Francisco County, California", "830235", "141446", "06", "075"]
    census, _ = census_answering(always(table(row, header=header)))

    found = await prepare(
        census,
        variables=["NAME", "B01003_001E", "B19013_001E"],
        geography_for="county:075",
        geography_in="state:06",
    ).run()

    (moe,) = [warning for warning in found.warnings if warning.startswith(MOE_WARNING)]
    assert moe.endswith("did not include these margins of error: B01003_001M, B19013_001M.")
    assert found.warnings[0] == moe
    assert found.rules[0] != ANNOTATION_RULE
    assert found.escalations == []


@pytest.mark.asyncio
async def test_moe_warning_is_plain_when_every_margin_is_requested() -> None:
    header = ["B19013_001E", "B19013_001M", "state"]
    census, _ = census_answering(always(table(["98000", "500", "06"], header=header)))

    found = await prepare(
        census,
        variables=["B19013_001E", "B19013_001M"],
        geography_for="state:06",
        geography_in=None,
    ).run()

    assert MOE_WARNING in found.warnings


@pytest.mark.asyncio
async def test_annotation_codes_lead_the_warnings_and_escalate() -> None:
    census, _ = census_answering(always(table(TRACT, PARK)))

    found = await prepare(census).run()

    assert found.warnings[0] == ANNOTATION_WARNING
    assert found.rules[0] == ANNOTATION_RULE
    assert found.escalations == ["ANNOTATION_CODES_PRESENT"]
    assert found.result.rows[1]["B19013_001E"] == "-666666666"


@pytest.mark.asyncio
async def test_a_null_value_counts_as_an_annotation() -> None:
    row = [*TRACT[:2], None, *TRACT[3:]]
    census, _ = census_answering(always(table(row)))

    found = await prepare(census).run()

    assert found.escalations == ["ANNOTATION_CODES_PRESENT"]
    assert found.result.rows[0]["B19013_001E"] is None


@pytest.mark.asyncio
async def test_codes_only_in_margins_of_error_do_not_escalate() -> None:
    # Live 2024 answer for county:075 in state:06: the total population is controlled.
    header = ["NAME", "B01003_001E", "B01003_001M", "state", "county"]
    row = ["San Francisco County, California", "830235", "-555555555", "06", "075"]
    census, _ = census_answering(always(table(row, header=header)))

    found = await prepare(
        census,
        variables=["NAME", "B01003_001E", "B01003_001M"],
        geography_for="county:075",
        geography_in="state:06",
    ).run()

    assert found.escalations == []
    assert ANNOTATION_RULE not in found.rules
    assert found.warnings[0] == MOE_WARNING
    assert ANNOTATION_WARNING in found.warnings


@pytest.mark.asyncio
async def test_a_listed_code_with_no_row_is_named_first() -> None:
    header = ["NAME", "B01003_001E", "B01003_001M", "state", "county"]
    row = ["San Francisco County, California", "830235", "-555555555", "06", "075"]
    census, _ = census_answering(always(table(row, header=header)))

    found = await prepare(
        census,
        variables=["NAME", "B01003_001E", "B01003_001M"],
        geography_for="county:075,999",
        geography_in="state:06",
    ).run()

    assert found.warnings[0].startswith("No row came back for county 999:")
    assert found.result.completeness == Completeness.COMPLETE


@pytest.mark.asyncio
async def test_a_key_with_surrounding_whitespace_is_trimmed() -> None:
    census, requests = census_answering(always(table(TRACT)), api_key=f"  {KEY}\n")

    await prepare(census).run()

    assert requests[0].url.params["key"] == KEY


@pytest.mark.asyncio
async def test_without_annotation_codes_there_is_no_escalation() -> None:
    census, _ = census_answering(always(table(TRACT)))

    found = await prepare(census).run()

    assert ANNOTATION_WARNING in found.warnings[1:]
    assert ANNOTATION_RULE not in found.rules
    assert found.escalations == []


@pytest.mark.asyncio
async def test_subject_and_profile_tables_use_their_own_paths() -> None:
    profile = {
        "name": "DP05_0001PE",
        "label": "Percent!!SEX AND AGE!!Total population",
        "concept": "ACS Demographic and Housing Estimates",
        "group": "DP05",
        "attributes": "DP05_0001PEA,DP05_0001PM,DP05_0001PMA",
    }
    header = ["DP05_0001PE", "state", "county"]
    census, requests = census_answering(
        always(table(["830235", "06", "075"], header=header)),
        extra_metadata={"DP05_0001PE": profile},
    )

    found = await prepare(
        census,
        dataset="acs/acs5/profile",
        variables=["DP05_0001PE"],
        geography_for="county:075",
        geography_in="state:06",
    ).run()

    assert requests[0].url.path == "/data/2024/acs/acs5/profile"
    assert requests[1].url.path == "/data/2024/acs/acs5/profile/variables/DP05_0001PE.json"
    assert "data profiles" in found.result.dataset_name
    assert found.warnings[0].endswith("margins of error: DP05_0001PM.")


@pytest.mark.parametrize("api_key", [None, "", "  \n"])
def test_missing_key_is_refused_without_a_request(api_key: str | None) -> None:
    census, requests = census_answering(always(table(TRACT)), api_key=api_key)

    with pytest.raises(SourceQueryError) as caught:
        prepare(census)

    assert caught.value.code == "SOURCE_NOT_CONFIGURED"
    assert caught.value.public_message == "This server has no Census Data API key configured."
    assert requests == []


@pytest.mark.parametrize(
    ("overrides", "argument"),
    [
        ({"geography_for": "tract:*&key=x"}, "geography_for"),
        ({"geography_for": "state:06;"}, "geography_for"),
        ({"geography_for": "state:06&in=county:*"}, "geography_for"),
        ({"geography_for": "state:06 county:075"}, "geography_for"),
        ({"geography_for": "tract:*\n"}, "geography_for"),
        ({"geography_for": "tract:\t*"}, "geography_for"),
        ({"geography_for": "státe:06"}, "geography_for"),
        ({"geography_for": "state:０６"}, "geography_for"),
        ({"geography_for": "state:٠٦"}, "geography_for"),
        ({"geography_for": "state:06​"}, "geography_for"),
        ({"geography_for": "tract"}, "geography_for"),
        ({"geography_for": "tract:"}, "geography_for"),
        ({"geography_for": "neighborhood:1"}, "geography_for"),
        ({"geography_for": "state:-1"}, "geography_for"),
        ({"geography_for": "state:12345678"}, "geography_for"),
        ({"geography_for": "state:06,"}, "geography_for"),
        ({"geography_for": "state:*,06"}, "geography_for"),
        ({"geography_for": "state:" + ",".join(["1"] * 51)}, "geography_for"),
        ({"geography_for": ""}, "geography_for"),
        ({"geography_for": None}, "geography_for"),
        ({"geography_in": "state:06&for=county:*"}, "geography_in"),
        ({"geography_in": "state:06,"}, "geography_in"),
        ({"geography_in": "state:06 state:07"}, "geography_in"),
        ({"geography_in": "state:06 tract:*"}, "geography_in"),
        ({"geography_in": "state:06/county:075"}, "geography_in"),
        ({"geography_in": "state:06 county:075 "}, None),
        ({"geography_in": ""}, "geography_in"),
        ({"geography_in": "state:06 county:075,075"}, "geography_in"),
        ({"geography_for": "county:075,075"}, "geography_for"),
        ({"variables": []}, "variables"),
        ({"variables": [f"B01001_{n:03d}E" for n in range(1, MAX_VARIABLES + 2)]}, "variables"),
        ({"variables": "NAME"}, "variables"),
        ({"variables": ["NAME", "name"]}, "variables"),
        ({"variables": ["B01003_001E;"]}, "variables"),
        ({"variables": ["B01003_001E&key=x"]}, "variables"),
        ({"variables": ["B01003 001E"]}, "variables"),
        ({"variables": ["group(B01003)"]}, "variables"),
        ({"variables": ["ＮＡＭＥ"]}, "variables"),
        ({"variables": ["Bı"]}, "variables"),
        ({"variables": [7]}, "variables"),
        ({"year": 2008}, "year"),
        ({"year": 2100}, "year"),
        ({"year": "2024"}, "year"),
        ({"year": True}, "year"),
        ({"year": 2009, "dataset": "acs/acs5/subject"}, "year"),
        ({"dataset": "acs/acs1"}, "dataset"),
        ({"dataset": "acs/acs5/../acs1"}, "dataset"),
        ({"dataset": ["acs/acs5"]}, "dataset"),
    ],
)
def test_validation_never_reaches_the_network(
    overrides: dict[str, Any], argument: str | None
) -> None:
    census, requests = census_answering(always(table(TRACT)))

    if argument is None:
        # Surrounding and repeated spaces are normalized, never refused.
        assert prepare(census, **overrides).arguments["geography_in"] == "state:06 county:075"
        return
    with pytest.raises(SourceQueryError) as caught:
        prepare(census, **overrides)

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert caught.value.argument == argument
    assert requests == []


def test_geography_accepts_levels_with_spaces_and_code_lists() -> None:
    census, _ = census_answering(always(table(TRACT)))

    prepared = prepare(
        census,
        geography_for="Block  Group:*",
        geography_in="state:06 county:075,081 tract:*",
    )

    assert prepared.arguments["geography_for"] == "block group:*"
    assert prepared.arguments["geography_in"] == "state:06 county:075,081 tract:*"
    district = prepare(
        census,
        geography_for="State Legislative District (Upper Chamber):chi,ADD",
        geography_in="state:50",
    )
    assert district.arguments["geography_for"] == (
        "state legislative district (upper chamber):CHI,ADD"
    )
    zcta = prepare(census, geography_for="zip code tabulation area:94110", geography_in=None)
    assert zcta.arguments["geography_in"] is None
    places = ",".join(f"{n:07d}" for n in range(MAX_CODES))
    listed = prepare(census, geography_for=f"place:{places}", geography_in="state:06")
    assert listed.arguments["geography_for"] == f"place:{places}"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json={"error": "nope"}),
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[HEADER, TRACT[:-1]]),
        httpx.Response(200, json=[HEADER, [*TRACT[:-1], 10101]]),
        httpx.Response(200, json=[HEADER[1:], TRACT[1:]]),
        httpx.Response(200, json=[HEADER[:-1], TRACT[:-1]]),
        httpx.Response(200, json=[[*HEADER, "tract"], [*TRACT, "010101"]]),
    ],
)
@pytest.mark.asyncio
async def test_malformed_body_is_a_source_error(response: httpx.Response) -> None:
    census, _ = census_answering(always(response))

    with pytest.raises(SourceError) as caught:
        await prepare(census).run()

    assert not isinstance(caught.value, SourceQueryError)
    assert_no_key(caught.value)


@pytest.mark.asyncio
async def test_metadata_that_does_not_confirm_the_variable_is_a_source_error() -> None:
    wrong = {"name": "B01003_001E", "label": "Estimate!!Total"}
    header = ["B99001_001E", "state"]
    census, _ = census_answering(
        always(table(["1", "06"], header=header)), extra_metadata={"B99001_001E": wrong}
    )

    with pytest.raises(SourceError):
        await prepare(
            census, variables=["B99001_001E"], geography_for="state:06", geography_in=None
        ).run()


@pytest.mark.parametrize(
    "text",
    [
        "error: unknown variable 'B99999_999E'",
        f"error: invalid key {KEY} for unknown variable 'B99999_999E'",
    ],
)
@pytest.mark.asyncio
async def test_400_passes_the_api_reason_through_without_the_key(text: str) -> None:
    census, _ = census_answering(always(httpx.Response(400, text=text)))

    with pytest.raises(SourceQueryError) as caught:
        await prepare(census).run()

    assert caught.value.code == "QUERY_REJECTED"
    assert caught.value.http_status == 400
    assert "unknown variable 'B99999_999E'" in caught.value.public_message
    assert_no_key(caught.value)


@pytest.mark.asyncio
async def test_400_with_an_html_body_gets_a_generic_message() -> None:
    census, _ = census_answering(always(httpx.Response(400, text="<html>bad</html>")))

    with pytest.raises(SourceQueryError) as caught:
        await prepare(census).run()

    assert caught.value.public_message == "The Census Data API rejected the request (HTTP 400)."


@pytest.mark.parametrize(
    ("status", "code", "argument"),
    [
        (204, "NOT_FOUND", None),
        (302, "SOURCE_KEY_REJECTED", None),
        (404, "NOT_FOUND", "year"),
        (500, "SOURCE_UNAVAILABLE", None),
        (503, "SOURCE_UNAVAILABLE", None),
    ],
)
@pytest.mark.asyncio
async def test_status_codes_map_to_errors_without_the_key(
    status: int, code: str, argument: str | None
) -> None:
    response = httpx.Response(
        status,
        headers={"location": f"https://api.census.gov/data/invalid_key.html?key={KEY}"}
        if status == 302
        else None,
        text=f"<html>{KEY}</html>" if status >= 404 else "",
    )
    census, _ = census_answering(always(response))

    with pytest.raises(SourceQueryError) as caught:
        await prepare(census).run()

    assert caught.value.code == code
    assert caught.value.argument == argument
    assert caught.value.http_status == status
    assert_no_key(caught.value)


@pytest.mark.asyncio
async def test_timeout_raises_without_the_key() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    census, _ = census_answering(timeout)

    with pytest.raises(SourceQueryError) as caught:
        await prepare(census).run()

    assert caught.value.code == "SOURCE_TIMEOUT"
    assert_no_key(caught.value)


@pytest.mark.asyncio
async def test_network_failure_is_a_source_error_without_the_key() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    census, _ = census_answering(refuse)

    with pytest.raises(SourceError) as caught:
        await prepare(census).run()

    assert_no_key(caught.value)


@pytest.mark.asyncio
async def test_more_rows_than_the_cap_is_refused() -> None:
    rows = [[*TRACT[:-1], f"{n:06d}"] for n in range(MAX_ROWS + 1)]
    census, requests = census_answering(always(table(*rows)))

    with pytest.raises(SourceQueryError) as caught:
        await prepare(census).run()

    assert caught.value.code == "RESULT_TOO_LARGE"
    assert caught.value.argument == "geography_for"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "raw",
    [
        f"https://api.census.gov/data/2024/acs/acs5?get=NAME&for=state%3A06&key={KEY}",
        f"https://api.census.gov/data/2024/acs/acs5?key={KEY}&get=NAME",
        f"https://api.govinfo.gov/collections?api_key={KEY}",
    ],
)
def test_scrub_removes_query_parameter_keys(raw: str) -> None:
    cleaned = scrub(raw)
    assert KEY not in cleaned
    assert REDACTED in cleaned


def test_scrub_keeps_a_json_log_line_valid() -> None:
    line = json.dumps({"text": 'key="rent" board', "url": f"https://api.census.gov/data?key={KEY}"})

    cleaned = scrub(line)

    assert KEY not in cleaned
    assert json.loads(cleaned)["text"] == 'key="rent" board'


@pytest.mark.asyncio
async def test_204_without_geography_in_names_geography_for() -> None:
    census, _ = census_answering(always(httpx.Response(204)))

    with pytest.raises(SourceQueryError) as caught:
        await prepare(census, geography_for="state:99", geography_in=None).run()

    assert caught.value.code == "NOT_FOUND"
    assert caught.value.argument == "geography_for"


def test_scrub_leaves_similar_parameter_names_alone() -> None:
    text = "GET https://example.test/search?monkey=banana&keyword=rent 200 OK"
    assert scrub(text) == text


def test_httpx_request_line_for_a_census_query_is_redacted() -> None:
    url = httpx.URL(
        "https://api.census.gov/data/2024/acs/acs5",
        params={"get": "NAME", "for": "state:06", "key": KEY},
    )
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("GET", url, "HTTP/1.1", 200, "OK"),
        exc_info=None,
    )

    assert SecretRedactingFilter().filter(record) is True
    rendered = record.getMessage()
    assert KEY not in rendered
    assert "key=[REDACTED]" in rendered

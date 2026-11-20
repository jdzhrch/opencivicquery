from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import httpx
import pytest

from opencivicquery.models import Completeness
from opencivicquery.sources import usaspending
from opencivicquery.sources.base import SourceError, SourceQueryError
from opencivicquery.sources.usaspending import (
    AGGREGATE_RECORDS_WARNING,
    AMOUNT_WARNING,
    ASSISTANCE_DELAY_WARNING,
    CONTRACT_DELAY_WARNING,
    DATE_RANGE_WARNING,
    FRESHNESS_WARNING,
    KEYWORD_WARNING,
    LOAN_AMOUNT_WARNING,
    NO_MATCH_WARNING,
    PLACE_WARNING,
    RECIPIENT_WARNING,
    REDACTED_RECORDS_WARNING,
    ROW_GRAIN,
    SUBAWARD_WARNING,
    USASPENDING_HOST,
    USAspending,
)

SEARCH_PATH = "/api/v2/search/spending_by_award/"
LAST_UPDATED_PATH = "/api/v2/awards/last_updated/"

# Trimmed from a live response on 2026-11-20.
CONTRACT = {
    "internal_id": 278396572,
    "Award ID": "1605JW21C0005",
    "Recipient Name": "MINACT, INC",
    "Recipient UEI": "K975UYPPLHD7",
    "Award Amount": 98395863.72,
    "Total Outlays": 97340157.36,
    "Description": "JOB CORPS IS A VOCATIONAL TRAINING PROGRAM FOR YOUTH.",
    "Contract Award Type": "DEFINITIVE CONTRACT",
    "Awarding Agency": "Department of Labor",
    "Awarding Sub Agency": "Office of the Assistant Secretary for Administration and Management",
    "Base Obligation Date": "2020-12-14",
    "Start Date": "2020-12-14",
    "End Date": "2026-03-31",
    "Primary Place of Performance": {
        "location_country_code": "USA",
        "state_code": "CA",
        "county_code": "075",
        "county_name": "SAN FRANCISCO",
        "zip5": "94130",
    },
    "generated_internal_id": "CONT_AWD_1605JW21C0005_1605_-NONE-_-NONE-",
    "awarding_agency_id": 267,
    "agency_slug": "department-of-labor",
}
LOAN = {
    "internal_id": 260369981,
    "Award ID": "DELP0000203",
    "Recipient Name": "AMANECER PUERTO RICO LLC",
    "Loan Value": 473791000.0,
    "Subsidy Cost": 40168000.98,
    "Issued Date": "2025-01-17",
    "Award Type": "DIRECT LOAN (E)",
    "generated_internal_id": "ASST_NON_DELP0000203_089",
}
# Assistance answers carry Award Amount where loans carry Subsidy Cost.
ASSISTANCE = {**LOAN, "Award Amount": 1250000.0}

CONTRACT_SEARCH = {
    "award_type": "contracts",
    "keywords": ["job corps"],
    "recipient": "Minact",
    "state": "CA",
    "county": "075",
    "start_date": "2024-10-01",
    "end_date": "2025-09-30",
    "limit": 10,
}


def page(*results: dict[str, Any], has_next: bool = False) -> dict[str, Any]:
    return {
        "spending_level": "awards",
        "limit": 10,
        "results": list(results),
        "page_metadata": {"page": 1, "hasNext": has_next},
        "messages": ["For searches, time period start and end dates are currently limited."],
    }


def source_answering(
    search: httpx.Response | Exception,
    last_updated: httpx.Response | None = None,
) -> tuple[USAspending, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    updated = last_updated or httpx.Response(200, json={"last_updated": "11/20/2026"})

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == LAST_UPDATED_PATH:
            return updated
        if isinstance(search, Exception):
            raise search
        return search

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return USAspending(client=client), requests


@pytest.mark.asyncio
async def test_the_request_body_is_exact_and_recorded_as_the_query() -> None:
    source, requests = source_answering(httpx.Response(200, json=page(CONTRACT)))

    prepared = source.search_awards(**CONTRACT_SEARCH)
    assert requests == []
    found = await prepared.run()

    assert prepared.host == USASPENDING_HOST
    sent = requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == f"https://{USASPENDING_HOST}{SEARCH_PATH}"
    assert sent.headers["User-Agent"].startswith("OpenCivicQuery/")
    expected_body = {
        "fields": [
            "Award ID",
            "Recipient Name",
            "Recipient UEI",
            "Award Amount",
            "Total Outlays",
            "Description",
            "Contract Award Type",
            "Awarding Agency",
            "Awarding Sub Agency",
            "Base Obligation Date",
            "Start Date",
            "End Date",
            "Primary Place of Performance",
            "generated_internal_id",
        ],
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],
            "time_period": [{"start_date": "2024-10-01", "end_date": "2025-09-30"}],
            "keywords": ["job corps"],
            "recipient_search_text": ["Minact"],
            "place_of_performance_locations": [{"country": "USA", "state": "CA", "county": "075"}],
        },
        "limit": 10,
        "order": "desc",
        "page": 1,
        "sort": "Award Amount",
        "spending_level": "awards",
    }
    assert json.loads(sent.content) == expected_body
    result = found.result
    assert result.query == json.dumps(expected_body, sort_keys=True, separators=(",", ":"))
    assert result.query_url is None
    assert result.domain == USASPENDING_HOST
    assert result.dataset_id == "spending_by_award/contracts"
    assert result.dataset_name == (
        "USAspending prime award search: contracts (award type codes A, B, C, D)"
    )
    assert result.source_url == "https://www.usaspending.gov/search"
    assert result.completeness == Completeness.COMPLETE
    assert result.returned_row_count == 1
    # The update date comes from the official last_updated endpoint, in ISO form.
    assert [request.url.path for request in requests] == [SEARCH_PATH, LAST_UPDATED_PATH]
    assert requests[1].method == "GET"
    assert result.source_updated_at == "2026-11-20"
    assert found.row_grain == ROW_GRAIN
    assert found.warnings == [
        DATE_RANGE_WARNING,
        AMOUNT_WARNING,
        KEYWORD_WARNING,
        RECIPIENT_WARNING,
        PLACE_WARNING,
        CONTRACT_DELAY_WARNING,
        FRESHNESS_WARNING,
        SUBAWARD_WARNING,
    ]


@pytest.mark.asyncio
async def test_arguments_are_normalized_without_audit_field_names() -> None:
    source, _ = source_answering(httpx.Response(200, json=page()))

    prepared = source.search_awards(
        award_type=" Grants ",
        keywords=["  homeless\n services "],
        state=" ca ",
        start_date="2024-10-01",
        end_date="2025-09-30",
    )

    assert prepared.arguments == {
        "award_type": "grants",
        "keywords": ["homeless services"],
        "recipient": None,
        "state": "CA",
        "county": None,
        "start_date": "2024-10-01",
        "end_date": "2025-09-30",
        "limit": 25,
    }
    reserved = {"domain", "query", "query_url", "status", "completeness", "portal"}
    assert reserved.isdisjoint(prepared.arguments)


@pytest.mark.asyncio
async def test_a_next_page_marks_the_result_truncated_without_fetching_it() -> None:
    source, requests = source_answering(httpx.Response(200, json=page(CONTRACT, has_next=True)))

    found = await source.search_awards(**CONTRACT_SEARCH).run()

    assert found.result.completeness == Completeness.TRUNCATED
    assert [request.url.path for request in requests].count(SEARCH_PATH) == 1


@pytest.mark.asyncio
async def test_rows_keep_the_requested_source_fields_and_add_the_award_page() -> None:
    odd = copy.deepcopy(CONTRACT)
    odd["generated_internal_id"] = "ASST_AGG_1 2/3?x#y_012"
    missing = copy.deepcopy(CONTRACT)
    del missing["generated_internal_id"]
    del missing["Total Outlays"]
    source, _ = source_answering(httpx.Response(200, json=page(CONTRACT, odd, missing)))

    rows = (await source.search_awards(**CONTRACT_SEARCH).run()).result.rows

    assert rows[0]["award_page_url"] == (
        "https://www.usaspending.gov/award/CONT_AWD_1605JW21C0005_1605_-NONE-_-NONE-"
    )
    assert rows[0]["Award Amount"] == 98395863.72
    assert rows[0]["Primary Place of Performance"]["county_code"] == "075"
    # Fields the request did not ask for are dropped; the source's names are kept.
    assert "internal_id" not in rows[0]
    assert "agency_slug" not in rows[0]
    assert list(rows[0])[:3] == ["Award ID", "Recipient Name", "Recipient UEI"]
    # An identifier is encoded so it cannot break out of the award path.
    assert rows[1]["award_page_url"] == (
        "https://www.usaspending.gov/award/ASST_AGG_1%202%2F3%3Fx%23y_012"
    )
    assert rows[2]["award_page_url"] is None
    assert rows[2]["Total Outlays"] is None


@pytest.mark.asyncio
async def test_loans_sort_by_subsidy_cost_and_request_loan_fields() -> None:
    source, requests = source_answering(httpx.Response(200, json=page(LOAN)))

    found = await source.search_awards(
        award_type="loans", state="CA", start_date="2024-10-01", end_date="2025-09-30"
    ).run()

    body = json.loads(requests[0].content)
    assert body["sort"] == "Subsidy Cost"
    assert body["filters"]["award_type_codes"] == ["07", "08", "F003", "F004"]
    assert body["filters"]["place_of_performance_locations"] == [{"country": "USA", "state": "CA"}]
    assert "Award Amount" not in body["fields"]
    assert {"Loan Value", "Subsidy Cost", "Issued Date"} <= set(body["fields"])
    assert found.result.rows[0]["Loan Value"] == 473791000.0
    assert found.warnings == [
        DATE_RANGE_WARNING,
        LOAN_AMOUNT_WARNING,
        PLACE_WARNING,
        ASSISTANCE_DELAY_WARNING,
        FRESHNESS_WARNING,
        SUBAWARD_WARNING,
    ]


@pytest.mark.asyncio
async def test_an_aggregate_record_adds_its_counting_warning() -> None:
    aggregate = copy.deepcopy(ASSISTANCE)
    aggregate["Recipient Name"] = "MULTIPLE RECIPIENTS"
    source, _ = source_answering(httpx.Response(200, json=page(aggregate)))

    found = await source.search_awards(
        award_type="direct_payments",
        recipient="multiple recipients",
        start_date="2024-10-01",
        end_date="2025-09-30",
    ).run()

    assert AGGREGATE_RECORDS_WARNING in found.warnings
    assert found.warnings.index(AGGREGATE_RECORDS_WARNING) < found.warnings.index(
        ASSISTANCE_DELAY_WARNING
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("award_type", "codes"),
    [
        ("grants", ["02", "03", "04", "05", "F001", "F002"]),
        ("direct_payments", ["06", "10", "F006", "F007"]),
        ("other", ["09", "11", "-1", "F005", "F008", "F009", "F010"]),
    ],
)
async def test_assistance_types_send_their_own_code_group(
    award_type: str, codes: list[str]
) -> None:
    source, requests = source_answering(httpx.Response(200, json=page(ASSISTANCE)))

    await source.search_awards(
        award_type=award_type, state="CA", start_date="2024-10-01", end_date="2025-09-30"
    ).run()

    body = json.loads(requests[0].content)
    assert body["filters"]["award_type_codes"] == codes
    assert body["sort"] == "Award Amount"
    assert {"Award Amount", "Award Type", "Assistance Listings"} <= set(body["fields"])


@pytest.mark.asyncio
async def test_no_match_leads_the_warnings() -> None:
    source, _ = source_answering(httpx.Response(200, json=page()))

    found = await source.search_awards(**CONTRACT_SEARCH).run()

    assert found.result.returned_row_count == 0
    assert found.result.completeness == Completeness.COMPLETE
    assert found.warnings[0] == NO_MATCH_WARNING


@pytest.mark.asyncio
async def test_a_redacted_record_adds_its_counting_warning() -> None:
    redacted = copy.deepcopy(ASSISTANCE)
    redacted["Recipient Name"] = "REDACTED DUE TO PII"
    source, _ = source_answering(httpx.Response(200, json=page(redacted)))

    found = await source.search_awards(
        award_type="grants", state="CA", start_date="2024-10-01", end_date="2025-09-30"
    ).run()

    assert REDACTED_RECORDS_WARNING in found.warnings
    assert AGGREGATE_RECORDS_WARNING not in found.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "last_updated",
    [
        httpx.Response(500, text="error"),
        httpx.Response(200, json={"last_updated": ""}),
        httpx.Response(200, json={"last_updated": "2026-11-20"}),
        httpx.Response(200, json={"last_updated": "13/45/2026"}),
        httpx.Response(200, content=b"not json"),
    ],
)
async def test_a_missing_update_date_leaves_it_empty(last_updated: httpx.Response) -> None:
    source, _ = source_answering(httpx.Response(200, json=page(CONTRACT)), last_updated)

    found = await source.search_awards(**CONTRACT_SEARCH).run()

    assert found.result.source_updated_at is None
    assert FRESHNESS_WARNING not in found.warnings
    assert found.result.returned_row_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "argument"),
    [
        ({"award_type": "idvs"}, "award_type"),
        ({"award_type": None}, "award_type"),
        ({"start_date": "2024-13-01"}, "start_date"),
        ({"start_date": "20241001"}, "start_date"),
        ({"start_date": "2024-W01-1"}, "start_date"),
        ({"start_date": "2007-09-30"}, "start_date"),
        ({"end_date": "2024-09-30"}, "end_date"),
        ({"end_date": None}, "end_date"),
        ({"keywords": "job corps"}, "keywords"),
        ({"keywords": ["ab"]}, "keywords"),
        ({"keywords": ["x" * 101]}, "keywords"),
        ({"keywords": ["one", "two", "six", "ten", "red", "big"]}, "keywords"),
        ({"keywords": ["job\x00corps"]}, "keywords"),
        ({"keywords": ["job​corps"]}, "keywords"),
        ({"recipient": "M"}, "recipient"),
        ({"recipient": "Min‮act"}, "recipient"),
        ({"recipient": 42}, "recipient"),
        ({"state": "ZZ"}, "state"),
        ({"state": "PW"}, "state"),
        ({"state": "California"}, "state"),
        ({"county": "75"}, "county"),
        ({"county": "SF1"}, "county"),
        ({"county": "०७५"}, "county"),
        ({"start_date": "２０２４-10-01"}, "start_date"),
        ({"state": None}, "county"),
        ({"limit": 0}, "limit"),
        ({"limit": 101}, "limit"),
        ({"limit": True}, "limit"),
        ({"limit": "10"}, "limit"),
        ({"keywords": None, "recipient": None, "state": None, "county": None}, None),
        ({"keywords": [], "recipient": None, "state": None, "county": None}, None),
    ],
)
async def test_an_invalid_search_never_reaches_the_source(
    override: dict[str, Any], argument: str | None
) -> None:
    source, requests = source_answering(httpx.Response(200, json=page(CONTRACT)))

    with pytest.raises(SourceQueryError) as caught:
        source.search_awards(**{**CONTRACT_SEARCH, **override})

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert caught.value.argument == argument
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        # Live bodies from 2026-11-20.
        httpx.Response(
            422,
            json={
                "detail": "Invalid value in 'filters|time_period': start_date cannot be "
                "earlier than '2007-10-01'"
            },
        ),
        httpx.Response(
            422, json={"message": "'award_type_codes' must only contain types from one group."}
        ),
        httpx.Response(400, json={"detail": "Sort value 'x' not found in requested fields"}),
    ],
)
async def test_the_source_rejection_reason_reaches_the_caller(response: httpx.Response) -> None:
    source, requests = source_answering(response)
    reason = response.json().get("detail") or response.json().get("message")

    with pytest.raises(SourceQueryError) as caught:
        await source.search_awards(**CONTRACT_SEARCH).run()

    assert caught.value.tool_error() == {
        "code": "QUERY_REJECTED",
        "message": reason,
        "argument": None,
        "http_status": response.status_code,
    }
    assert [request.url.path for request in requests] == [SEARCH_PATH]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="<html>Service Unavailable</html>"),
        httpx.Response(500, json={"detail": "internal"}),
        httpx.Response(302, headers={"Location": "https://example.com/elsewhere"}),
    ],
)
async def test_an_unavailable_source_is_reported_without_following_it(
    response: httpx.Response,
) -> None:
    source, requests = source_answering(response)

    with pytest.raises(SourceQueryError) as caught:
        await source.search_awards(**CONTRACT_SEARCH).run()

    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert caught.value.http_status == response.status_code
    assert caught.value.public_message == (
        f"The USAspending API could not complete the request (HTTP {response.status_code})."
    )
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"spending_level": "awards", "page_metadata": {"hasNext": false}}',
        b'{"spending_level": "awards", "results": [], "page_metadata": {}}',
        b'{"spending_level": "awards", "results": [], "page_metadata": {"hasNext": "no"}}',
        b'{"spending_level": "awards", "results": ["x"], "page_metadata": {"hasNext": false}}',
        # Subaward or unconfirmed results must never be frozen as prime awards.
        b'{"spending_level": "subawards", "results": [], "page_metadata": {"hasNext": false}}',
        b'{"results": [], "page_metadata": {"hasNext": false}}',
    ],
)
async def test_a_malformed_answer_is_a_source_error(body: bytes) -> None:
    source, _ = source_answering(httpx.Response(200, content=body))

    with pytest.raises(SourceError) as caught:
        await source.search_awards(**CONTRACT_SEARCH).run()

    assert not isinstance(caught.value, SourceQueryError)


@pytest.mark.asyncio
async def test_a_stalled_source_is_a_timeout_the_caller_can_retry() -> None:
    source, _ = source_answering(httpx.ReadTimeout("stalled"))

    with pytest.raises(SourceQueryError) as caught:
        await source.search_awards(**CONTRACT_SEARCH).run()

    assert caught.value.code == "SOURCE_TIMEOUT"


@pytest.mark.asyncio
async def test_the_schema_hash_moves_with_missing_fields_not_with_the_data() -> None:
    source, _ = source_answering(httpx.Response(200, json=page(CONTRACT)))
    first = (await source.search_awards(**CONTRACT_SEARCH).run()).result
    other = copy.deepcopy(CONTRACT)
    other["Award Amount"] = 1.0
    other["agency_code"] = "1601"
    source, _ = source_answering(httpx.Response(200, json=page(other)))
    same = (await source.search_awards(**CONTRACT_SEARCH).run()).result
    fewer = copy.deepcopy(CONTRACT)
    del fewer["Recipient UEI"]
    source, _ = source_answering(httpx.Response(200, json=page(fewer)))
    drifted = (await source.search_awards(**CONTRACT_SEARCH).run()).result

    assert first.schema_sha256 == same.schema_sha256
    assert first.schema_sha256 != drifted.schema_sha256


@pytest.mark.asyncio
async def test_a_stalled_update_date_does_not_hold_up_the_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(usaspending, "LAST_UPDATED_TIMEOUT_SECONDS", 0.01)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LAST_UPDATED_PATH:
            await asyncio.sleep(5)
        return httpx.Response(200, json=page(CONTRACT))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    found = await USAspending(client=client).search_awards(**CONTRACT_SEARCH).run()

    assert found.result.source_updated_at is None
    assert found.result.returned_row_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_closing_leaves_a_borrowed_client_open() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    await USAspending(client=client).close()
    assert client.is_closed is False
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["Award ID", "Award Amount", "generated_internal_id"])
async def test_a_field_null_on_every_award_is_a_source_error(field: str) -> None:
    # The API answers a renamed field with null, so null everywhere means a changed schema.
    renamed = copy.deepcopy(CONTRACT) | {field: None}
    source, _ = source_answering(httpx.Response(200, json=page(renamed, renamed)))

    with pytest.raises(SourceError, match="returned no"):
        await source.search_awards(
            award_type="contracts", state="CA", start_date="2024-10-01", end_date="2025-09-30"
        ).run()

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest

from opencivicquery.models import Completeness
from opencivicquery.sources import legistar as legistar_module
from opencivicquery.sources.base import SourceError, SourceQueryError
from opencivicquery.sources.legistar import (
    CLERK_EDITS_WARNING,
    EMPTY_RESULT_WARNING,
    LEGISTAR_HOST,
    MATTER_FIELDS,
    MEETING_FIELDS,
    NO_DATE_WARNING,
    SUBSTRING_WARNING,
    TOKEN_LINK_WARNING,
    VOTE_RECORD_WARNING,
    Legistar,
    stale_warning,
)

# Trimmed from live Oakland responses on 2026-11-20.
EVENT = {
    "EventId": 9617,
    "EventBodyName": "*Rules & Legislation Committee",
    "EventDate": "2026-11-19T00:00:00",
    "EventTime": "10:30 AM",
    "EventLocation": "City Council Chamber, 3rd Floor",
    "EventAgendaStatusName": "SUPPLEMENTAL",
    "EventMinutesStatusName": "DRAFT",
    "EventAgendaFile": "https://oakland.legistar1.com/oakland/meetings/2026/11/9617_A.pdf",
    "EventMinutesFile": "https://oakland.legistar1.com/oakland/meetings/2026/11/9617_M.pdf",
    "EventInSiteURL": "https://oakland.legistar.com/MeetingDetail.aspx?LEGID=9617&GID=134",
    "EventMedia": "7903",
    "EventVideoPath": None,
    "EventComment": None,
    "EventLastModifiedUtc": "2026-11-19T20:38:41.21",
    "EventGuid": "E6286148-2873-4B6C-8BBA-E02568BF1D26",
}
ITEM = {
    "EventItemId": 240300,
    "EventItemEventId": 9617,
    "EventItemAgendaNumber": "3",
    "EventItemAgendaSequence": 8,
    "EventItemTitle": "New Scheduling Items",
    "EventItemMatterFile": None,
    "EventItemActionName": "Accepted as Amended",
    "EventItemPassedFlagName": "Pass",
    "EventItemRollCallFlag": 0,
    "EventItemTally": None,
    "EventItemMover": "Carroll Fife",
    "EventItemSeconder": "Rowena Brown",
    "EventItemMatterAttachments": [{"MatterAttachmentHyperlink": "https://example.org/x.pdf"}],
}
VOTE = {
    "VoteId": 209448,
    "VotePersonName": "Rowena Brown",
    "VoteValueName": "Aye",
    "VoteResult": 1,
    "VoteLastModifiedUtc": "2026-11-19T19:08:14.8",
    "VoteEventItemId": 240300,
}
MATTER = {
    "MatterId": 35541,
    "MatterGuid": "D377AD2D-89C9-46F5-B223-FB321952E4F9",
    "MatterFile": "24-0624",
    "MatterName": None,
    "MatterTitle": "Subject: Justice For Renters Act",
    "MatterTypeName": "City Resolution",
    "MatterStatusName": "Adopted",
    "MatterIntroDate": "2024-07-01T00:00:00",
}


def legistar_answering(
    route: Callable[[httpx.Request], httpx.Response],
) -> tuple[Legistar, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return route(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # The fixtures' matters date from 2024, so a fixed day keeps them from reading as stale.
    return Legistar(client=client, today=lambda: date(2024, 12, 1)), requests


def always(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: response


@pytest.mark.asyncio
async def test_find_meetings_sends_one_bounded_filter_and_detects_truncation() -> None:
    legistar, requests = legistar_answering(always(httpx.Response(200, json=[EVENT] * 3)))

    found = await legistar.find_meetings(
        client=" Oakland ",
        start_date="2026-11-01",
        end_date="2026-11-19",
        body="o'connor council",
        limit=2,
    ).run()

    (sent,) = requests
    assert sent.url.host == LEGISTAR_HOST
    assert sent.url.path == "/v1/oakland/events"
    assert sent.headers["User-Agent"].startswith("OpenCivicQuery/")
    assert dict(sent.url.params) == {
        "$filter": (
            "EventDate ge datetime'2026-11-01' and EventDate lt datetime'2026-11-20' "
            "and substringof('o''connor council',EventBodyName)"
        ),
        "$orderby": "EventDate desc,EventId desc",
        "$top": "3",
        "$select": ",".join(MEETING_FIELDS),
    }
    result = found.result
    assert result.completeness == Completeness.TRUNCATED
    assert result.returned_row_count == 2
    assert set(result.rows[0]) == set(MEETING_FIELDS)
    assert "EventGuid" not in result.rows[0]
    assert result.query_url == str(sent.url)
    assert result.query == str(sent.url).removeprefix(f"https://{LEGISTAR_HOST}")
    assert result.source_url == "https://oakland.legistar.com/Calendar.aspx"
    assert result.dataset_id == "oakland/events"
    assert CLERK_EDITS_WARNING in found.warnings


@pytest.mark.asyncio
async def test_find_meetings_at_the_limit_is_complete() -> None:
    legistar, _ = legistar_answering(always(httpx.Response(200, json=[EVENT] * 2)))

    found = await legistar.find_meetings(
        client="oakland", start_date="2026-11-01", end_date="2026-11-19", limit=2
    ).run()

    assert found.result.completeness == Completeness.COMPLETE
    assert found.result.returned_row_count == 2


@pytest.mark.parametrize(
    ("arguments", "argument"),
    [
        ({"client": "oakland.legistar.com"}, "client"),
        ({"client": "../nyc"}, "client"),
        ({"client": "oak land"}, "client"),
        ({"client": ""}, "client"),
        ({"start_date": "11/01/2026"}, "start_date"),
        ({"start_date": "2026-02-30"}, "start_date"),
        ({"end_date": "2026-10-01"}, "end_date"),
        ({"body": "x"}, "body"),
        ({"body": "council\u202e"}, "body"),
        ({"body": "c" * 101}, "body"),
        ({"end_date": "9999-12-31"}, "end_date"),
        ({"start_date": "1899-12-31"}, "start_date"),
        ({"limit": 0}, "limit"),
        ({"limit": 201}, "limit"),
    ],
)
def test_invalid_meeting_arguments_are_refused_before_any_request(
    arguments: dict[str, Any], argument: str
) -> None:
    legistar, requests = legistar_answering(always(httpx.Response(200, json=[])))
    call = {"client": "oakland", "start_date": "2026-11-01", "end_date": "2026-11-19"}

    with pytest.raises(SourceQueryError) as caught:
        legistar.find_meetings(**(call | arguments))

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert caught.value.argument == argument
    assert requests == []


@pytest.mark.asyncio
async def test_meeting_items_name_the_meeting_and_drop_attachments() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oakland/events/9617":
            return httpx.Response(200, json=EVENT)
        return httpx.Response(200, json=[ITEM])

    legistar, requests = legistar_answering(route)

    found = await legistar.get_meeting_items(client="oakland", event_id=9617).run()

    assert [request.url.path for request in requests] == [
        "/v1/oakland/events/9617",
        "/v1/oakland/events/9617/eventitems",
    ]
    assert dict(requests[1].url.params) == {"AgendaNote": "1", "MinutesNote": "1", "$top": "1000"}
    result = found.result
    assert result.completeness == Completeness.COMPLETE
    assert "EventItemMatterAttachments" not in result.rows[0]
    assert result.rows[0]["EventItemActionName"] == "Accepted as Amended"
    assert "minutes status DRAFT" in result.dataset_name
    assert "2026-11-19" in result.dataset_name
    assert result.source_url == EVENT["EventInSiteURL"]
    assert found.warnings[0] == EMPTY_RESULT_WARNING


@pytest.mark.asyncio
async def test_a_meeting_page_on_another_host_is_never_cited() -> None:
    elsewhere = EVENT | {"EventInSiteURL": "https://example.com/phish"}

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oakland/events/9617":
            return httpx.Response(200, json=elsewhere)
        return httpx.Response(200, json=[])

    legistar, _ = legistar_answering(route)

    found = await legistar.get_meeting_items(client="oakland", event_id=9617).run()

    assert found.result.source_url == "https://oakland.legistar.com/Calendar.aspx"


@pytest.mark.asyncio
async def test_a_full_reply_page_is_reported_as_truncated() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oakland/events/9617":
            return httpx.Response(200, json=EVENT)
        return httpx.Response(200, json=[ITEM] * 1000)

    legistar, _ = legistar_answering(route)

    found = await legistar.get_meeting_items(client="oakland", event_id=9617).run()

    assert found.result.completeness == Completeness.TRUNCATED


@pytest.mark.asyncio
async def test_votes_carry_the_item_and_meeting_in_the_dataset_name() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oakland/events/9617":
            return httpx.Response(200, json=EVENT)
        if request.url.path == "/v1/oakland/events/9617/eventitems/240300":
            return httpx.Response(200, json=ITEM)
        return httpx.Response(200, json=[VOTE])

    legistar, requests = legistar_answering(route)

    found = await legistar.get_item_votes(
        client="oakland", event_id=9617, event_item_id=240300
    ).run()

    assert requests[-1].url.path == "/v1/oakland/eventitems/240300/votes"
    assert found.result.rows == [
        {
            "VotePersonName": "Rowena Brown",
            "VoteValueName": "Aye",
            "VoteResult": 1,
            "VoteLastModifiedUtc": "2026-11-19T19:08:14.8",
        }
    ]
    name = found.result.dataset_name
    assert "agenda item 3" in name
    assert "result Pass" in name
    assert "minutes status DRAFT" in name
    assert found.warnings[0] == VOTE_RECORD_WARNING
    assert found.result.query_url == f"https://{LEGISTAR_HOST}/v1/oakland/eventitems/240300/votes"


@pytest.mark.asyncio
async def test_an_item_from_another_meeting_is_not_found() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oakland/events/9609":
            return httpx.Response(200, json=EVENT | {"EventId": 9609})
        if request.url.path.endswith("/eventitems/240300"):
            return httpx.Response(200, json=ITEM)
        return httpx.Response(200, json=[VOTE])

    legistar, requests = legistar_answering(route)

    with pytest.raises(SourceQueryError) as caught:
        await legistar.get_item_votes(client="oakland", event_id=9609, event_item_id=240300).run()

    assert caught.value.code == "NOT_FOUND"
    assert caught.value.argument == "event_item_id"
    assert not any(request.url.path.endswith("/votes") for request in requests)


@pytest.mark.asyncio
async def test_legislation_search_escapes_text_and_links_each_matter() -> None:
    legistar, requests = legistar_answering(always(httpx.Response(200, json=[MATTER])))

    found = await legistar.search_legislation(
        client="oakland",
        text="renters' act",
        introduced_from="2024-01-01",
        introduced_to="2024-12-31",
        limit=5,
    ).run()

    params = dict(requests[0].url.params)
    assert params["$filter"] == (
        "(substringof('renters'' act',MatterTitle) or substringof('renters'' act',MatterName) "
        "or MatterFile eq 'renters'' act') and MatterIntroDate ge datetime'2024-01-01' "
        "and MatterIntroDate lt datetime'2025-01-01'"
    )
    assert params["$top"] == "6"
    assert params["$select"] == ",".join((*MATTER_FIELDS, "MatterGuid"))
    (row,) = found.result.rows
    assert row["LegislationDetailURL"] == (
        "https://oakland.legistar.com/LegislationDetail.aspx?"
        "ID=35541&GUID=D377AD2D-89C9-46F5-B223-FB321952E4F9"
    )
    assert "MatterGuid" not in row
    assert found.warnings[0] == SUBSTRING_WARNING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code", "argument"),
    [
        (
            httpx.Response(
                500,
                json={
                    "Message": "An error has occurred.",
                    "ExceptionMessage": (
                        "LegistarConnectionString setting is not set up in InSite for client: x"
                    ),
                    "StackTrace": "at C:\\builds\\secret\\Common.cs",
                },
            ),
            "UNKNOWN_CLIENT",
            "client",
        ),
        (httpx.Response(403), "TOKEN_REQUIRED", "client"),
        # Only an ID lookup's 404 names a missing record; a list query's 404 does not.
        (httpx.Response(404, json={"Message": "No HTTP resource"}), "SOURCE_UNAVAILABLE", None),
        (
            httpx.Response(500, json={"Message": "An error has occurred."}),
            "SOURCE_UNAVAILABLE",
            None,
        ),
        (httpx.Response(400, json={"Message": "bad filter"}), "QUERY_REJECTED", None),
    ],
)
async def test_source_failures_become_clear_tool_errors(
    response: httpx.Response, code: str, argument: str | None
) -> None:
    legistar, _ = legistar_answering(always(response))

    with pytest.raises(SourceQueryError) as caught:
        await legistar.find_meetings(
            client="oakland", start_date="2026-11-01", end_date="2026-11-19"
        ).run()

    assert caught.value.code == code
    assert caught.value.argument == argument
    assert "builds" not in caught.value.public_message


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not json", b'{"EventId": 1}', b"[1, 2]"])
async def test_a_malformed_answer_is_a_source_error(body: bytes) -> None:
    legistar, _ = legistar_answering(always(httpx.Response(200, content=body)))

    with pytest.raises(SourceError) as caught:
        await legistar.find_meetings(
            client="oakland", start_date="2026-11-01", end_date="2026-11-19"
        ).run()

    assert not isinstance(caught.value, SourceQueryError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(404, json={"Message": "Event not found"}), "NOT_FOUND"),
        (
            httpx.Response(404, text="<html>404 - File or directory not found.</html>"),
            "SOURCE_UNAVAILABLE",
        ),
    ],
)
async def test_only_a_record_404_reads_as_not_found(response: httpx.Response, code: str) -> None:
    legistar, _ = legistar_answering(always(response))

    with pytest.raises(SourceQueryError) as caught:
        await legistar.get_meeting_items(client="oakland", event_id=9617).run()

    assert caught.value.code == code


@pytest.mark.parametrize("text", ["住房" * 50, "'" * 100])
def test_a_search_too_long_once_encoded_is_refused_before_any_request(text: str) -> None:
    legistar, requests = legistar_answering(always(httpx.Response(200, json=[])))

    with pytest.raises(SourceQueryError) as caught:
        legistar.search_legislation(client="oakland", text=text)

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert requests == []


@pytest.mark.asyncio
async def test_no_recorded_votes_escalates() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oakland/events/9617":
            return httpx.Response(200, json=EVENT)
        if request.url.path.endswith("/eventitems/240300"):
            return httpx.Response(200, json=ITEM)
        return httpx.Response(200, json=[])

    legistar, _ = legistar_answering(route)

    found = await legistar.get_item_votes(
        client="oakland", event_id=9617, event_item_id=240300
    ).run()

    assert found.result.returned_row_count == 0
    assert found.escalations == ["NO_VOTES_RECORDED"]
    assert found.rules[0].startswith("NO MEMBER VOTES RECORDED")


@pytest.mark.asyncio
async def test_a_meeting_link_on_another_host_is_dropped_from_rows() -> None:
    elsewhere = EVENT | {"EventInSiteURL": "https://oakland.legistar.com.evil.example/x"}
    legistar, _ = legistar_answering(always(httpx.Response(200, json=[elsewhere, EVENT])))

    found = await legistar.find_meetings(
        client="oakland", start_date="2026-11-01", end_date="2026-11-19"
    ).run()

    assert found.result.rows[0]["EventInSiteURL"] is None
    assert found.result.rows[1]["EventInSiteURL"] == EVENT["EventInSiteURL"]


@pytest.mark.asyncio
async def test_a_date_filter_on_legislation_warns_about_undated_matters() -> None:
    legistar, _ = legistar_answering(always(httpx.Response(200, json=[MATTER])))

    dated = await legistar.search_legislation(
        client="oakland", text="renters", introduced_from="2024-01-01"
    ).run()
    undated = await legistar.search_legislation(client="oakland", text="renters").run()

    assert NO_DATE_WARNING in dated.warnings
    assert NO_DATE_WARNING not in undated.warnings


TOKEN = "dGVzdC10b2tlbi12YWx1ZQ=="


def tokened(
    route: Callable[[httpx.Request], httpx.Response],
) -> tuple[Legistar, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return route(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Legistar(client=client, tokens={"nyc": TOKEN}, today=lambda: date(2024, 12, 1)), requests


@pytest.mark.asyncio
async def test_a_client_token_is_sent_but_never_cited() -> None:
    legistar, requests = tokened(always(httpx.Response(200, json=[EVENT])))

    found = await legistar.find_meetings(
        client="nyc", start_date="2026-11-01", end_date="2026-11-19"
    ).run()

    assert requests[0].url.params["token"] == TOKEN
    assert TOKEN not in repr(found)
    assert "token" not in (found.result.query_url or "")
    assert TOKEN_LINK_WARNING in found.warnings


@pytest.mark.asyncio
async def test_other_clients_never_receive_the_token() -> None:
    legistar, requests = tokened(always(httpx.Response(200, json=[EVENT])))

    found = await legistar.find_meetings(
        client="oakland", start_date="2026-11-01", end_date="2026-11-19"
    ).run()

    assert "token" not in requests[0].url.params
    assert TOKEN_LINK_WARNING not in found.warnings


@pytest.mark.asyncio
async def test_a_refused_token_is_named_without_revealing_it() -> None:
    legistar, _ = tokened(always(httpx.Response(403)))

    with pytest.raises(SourceQueryError) as caught:
        await legistar.find_meetings(
            client="nyc", start_date="2026-11-01", end_date="2026-11-19"
        ).run()

    assert caught.value.code == "TOKEN_REJECTED"
    assert TOKEN not in str(caught.value)
    assert TOKEN not in caught.value.public_message


@pytest.mark.parametrize(
    "tokens", [{"nyc": ""}, {"nyc": "a b"}, {"nyc": "x&y=z"}, {"NYC": TOKEN}, {"../nyc": TOKEN}]
)
def test_a_malformed_token_setting_stops_startup(tokens: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        Legistar(tokens=tokens)


def test_a_token_in_a_logged_url_is_redacted() -> None:
    from opencivicquery.logging_redaction import scrub

    line = f"GET https://webapi.legistar.com/v1/nyc/events?%24top=1&token={TOKEN} 200"

    assert TOKEN not in scrub(line)
    assert "access_token=abc" not in scrub("x?access_token=abc")


def newest_matter_request(request: httpx.Request) -> bool:
    return request.url.params.get("$select") == "MatterIntroDate"


def api_requests(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [request for request in requests if request.url.host == LEGISTAR_HOST]


def stale_route(newest: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    """An API with the given newest matter, beside a website that cannot be read."""

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host != LEGISTAR_HOST:
            return httpx.Response(503, text="busy")
        if newest_matter_request(request):
            return newest
        return httpx.Response(200, json=[MATTER])

    return route


@pytest.mark.asyncio
async def test_an_api_copy_that_stopped_updating_escalates() -> None:
    # checked 2026-11-21: sfgov's newest matter was introduced on 2020-09-17.
    legistar, requests = legistar_answering(
        stale_route(httpx.Response(200, json=[{"MatterIntroDate": "2020-09-17T00:00:00"}]))
    )

    found = await legistar.search_legislation(client="sfgov", text="Mission Street").run()

    (newest,) = [request for request in requests if newest_matter_request(request)]
    assert len(api_requests(requests)) == 2
    # The website could not be read, so the API's older copy stands under its warning.
    (site,) = [request for request in requests if request.url.host != LEGISTAR_HOST]
    assert str(site.url) == "https://sfgov.legistar.com/Legislation.aspx"
    assert dict(newest.url.params) == {
        "$filter": "MatterIntroDate lt datetime'2024-12-02'",
        "$top": "1",
        "$orderby": "MatterIntroDate desc",
        "$select": "MatterIntroDate",
    }
    assert found.warnings[0] == stale_warning("sfgov", date(2020, 9, 17))
    assert found.rules[0].startswith("STALE API COPY")
    assert "https://sfgov.legistar.com" in found.rules[0]
    assert found.escalations == ["LEGISTAR_API_STALE"]
    assert found.result.returned_row_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "newest",
    [
        httpx.Response(200, json=[{"MatterIntroDate": "2024-11-19T00:00:00"}]),
        httpx.Response(200, json=[{"MatterIntroDate": None}]),
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[{"MatterIntroDate": "not a date"}]),
        httpx.Response(500, json={"Message": "An error has occurred."}),
    ],
)
async def test_a_current_or_unknown_newest_matter_adds_no_stale_caveat(
    newest: httpx.Response,
) -> None:
    legistar, _ = legistar_answering(stale_route(newest))

    found = await legistar.search_legislation(client="oakland", text="renters").run()

    assert found.warnings[0] == SUBSTRING_WARNING
    assert found.escalations == []
    assert found.result.returned_row_count == 1


@pytest.mark.asyncio
async def test_a_client_side_setting_error_is_named() -> None:
    # checked 2026-11-21: sfgov's events resource answers this way to every request.
    reason = (
        "'Agenda Draft Status' or 'Agenda Status Not Vievable By The Public' is not setup in "
        "settings. Value should be greater than 0."
    )
    legistar, _ = legistar_answering(
        always(httpx.Response(400, text=reason, headers={"Content-Type": "text/plain"}))
    )

    with pytest.raises(SourceQueryError) as caught:
        # The site answers the calendar page the same way, so the API's error stands.
        await legistar.find_meetings(
            client="sfgov", start_date="2024-11-01", end_date="2024-11-30"
        ).run()

    assert caught.value.code == "CLIENT_MISCONFIGURED"
    assert caught.value.argument == "client"
    assert reason in caught.value.public_message
    assert "https://sfgov.legistar.com" in caught.value.public_message


@pytest.mark.asyncio
@pytest.mark.parametrize(("newest", "stale"), [("2024-06-04", False), ("2024-06-03", True)])
async def test_the_stale_line_is_180_days(newest: str, stale: bool) -> None:
    # The helper fixes today at 2024-12-01; 180 days earlier is 2024-06-04.
    legistar, _ = legistar_answering(
        stale_route(httpx.Response(200, json=[{"MatterIntroDate": f"{newest}T00:00:00"}]))
    )

    found = await legistar.search_legislation(client="oakland", text="renters").run()

    assert (found.escalations == ["LEGISTAR_API_STALE"]) is stale


@pytest.mark.asyncio
async def test_a_slow_newest_matter_check_never_sinks_the_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legistar_module, "NEWEST_MATTER_TIMEOUT_SECONDS", 0.05)

    async def handler(request: httpx.Request) -> httpx.Response:
        if newest_matter_request(request):
            await asyncio.sleep(5)
            return httpx.Response(200, json=[{"MatterIntroDate": "2020-09-17T00:00:00"}])
        return httpx.Response(200, json=[MATTER])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    legistar = Legistar(client=client, today=lambda: date(2026, 11, 21))

    found = await asyncio.wait_for(
        legistar.search_legislation(client="sfgov", text="renters").run(), timeout=2
    )

    assert found.result.returned_row_count == 1
    assert found.escalations == []


@pytest.mark.asyncio
async def test_a_failed_search_cancels_the_newest_matter_check() -> None:
    started = asyncio.Event()
    finished: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if newest_matter_request(request):
            started.set()
            await asyncio.sleep(1)
            finished.append(True)
            return httpx.Response(200, json=[])
        await started.wait()
        return httpx.Response(500, json={"Message": "An error has occurred."})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    legistar = Legistar(client=client, today=lambda: date(2026, 11, 21))

    with pytest.raises(SourceQueryError):
        await legistar.search_legislation(client="oakland", text="renters").run()
    await asyncio.sleep(0)

    assert finished == []


@pytest.mark.asyncio
async def test_a_tokened_legislation_search_never_cites_the_token() -> None:
    legistar, requests = tokened(
        stale_route(httpx.Response(200, json=[{"MatterIntroDate": "2020-09-17T00:00:00"}]))
    )

    found = await legistar.search_legislation(client="nyc", text="rent").run()

    assert all(request.url.params["token"] == TOKEN for request in api_requests(requests))
    assert len(api_requests(requests)) == 2
    # The token belongs to the API alone; the jurisdiction's website never receives it.
    (site,) = [request for request in requests if request.url.host != LEGISTAR_HOST]
    assert site.url.host == "nyc.legistar.com"
    assert TOKEN not in str(site.url) and TOKEN not in repr(site.headers)
    assert TOKEN not in repr(found)
    assert found.escalations == ["LEGISTAR_API_STALE"]


# The calendar markup below follows sfgov.legistar.com/Calendar.aspx as served on
# 2026-11-22, trimmed to the grid, its pager, and the year menu's input.
CAL_GUID = "9E7FA469-A378-4F34-B5AD-03FD15F32EDD"
CAL_HEADERS = (
    "Name",
    "Meeting Date",
    "&nbsp;",
    "Meeting Time",
    "Meeting Location",
    "Meeting Details",
    "Agenda",
    "Minutes",
    "Video",
)
SETTING_REASON = (
    "'Agenda Draft Status' or 'Agenda Status Not Vievable By The Public' is not setup in "
    "settings. Value should be greater than 0."
)


def calendar_row(
    body: str, day: str, meeting_id: int, *, minutes: bool = True, agenda: str | None = None
) -> str:
    agenda_href = agenda or f"View.ashx?M=A&amp;ID={meeting_id}&amp;GUID={CAL_GUID}"
    minutes_cell = (
        f'<a href="View.ashx?M=M&amp;ID={meeting_id}&amp;GUID={CAL_GUID}">Minutes</a>'
        if minutes
        else '<a class="otherNotAvaiableMinutes"><font>Not&nbsp;available</font></a>'
    )
    return (
        f'<tr class="rgRow"><td><font><a><font>{body}</font></a></font></td>'
        f'<td class="rgSorted"><font>{day}</font></td>'
        f'<td><a href="View.ashx?M=IC&amp;ID={meeting_id}&amp;GUID={CAL_GUID}">'
        '<img alt="Export to iCalendar" /></a></td>'
        "<td><span><font>10:00 AM</font></span></td>"
        "<td><font>City Hall, Room 250<br /><em>Regular Meeting</em></font></td>"
        f'<td><a href="MeetingDetail.aspx?ID={meeting_id}&amp;GUID={CAL_GUID}'
        '&amp;Options=info|&amp;Search=">Meeting&nbsp;details</a></td>'
        f'<td><img alt="Agenda" /> <a href="{agenda_href}">Agenda</a></td>'
        f"<td>{minutes_cell}</td>"
        '<td><a class="videolink">Not&nbsp;available</a></td></tr>'
    )


def calendar_html(
    year: str, rows: list[str], *, pager: bool = False, body: str = "All Bodies"
) -> str:
    header = "".join(f'<th scope="col" class="rgHeader">{label}</th>' for label in CAL_HEADERS)
    pager_row = (
        '<tr class="rgPager"><td colspan="9"><table><tbody><tr><td class="rgPagerCell">'
        '<div class="rgWrap rgInfoPart">Page 1 of 2, items 1 to 100 of 181.</div>'
        "</td></tr></tbody></table></td></tr>"
        if pager
        else ""
    )
    return (
        f'<html><body><input id="ctl00_ContentPlaceHolder1_lstYears_Input" value="{year}" />'
        f'<input id="ctl00_ContentPlaceHolder1_lstBodies_Input" value="{body}" />'
        '<div class="rcbSlide"><div id="ctl00_ContentPlaceHolder1_lstBodies_DropDown">'
        '<div class="rcbScroll"><ul class="rcbList"><li class="rcbItem">All Bodies</li>'
        '<li class="rcbItem">Board</li></ul></div></div></div>'
        '<table class="rgMasterTable" id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00">'
        f"<thead>{pager_row}<tr>{header}</tr></thead><tbody>{''.join(rows)}</tbody>"
        f"<tfoot>{pager_row}</tfoot></table></body></html>"
    )


def calendar_site(
    pages: dict[tuple[str, str], str],
    *,
    first_page: httpx.Response | None = None,
    api: httpx.Response | None = None,
) -> tuple[Legistar, list[httpx.Request]]:
    """A Legistar whose events API names a client setting error and whose site serves pages."""

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == LEGISTAR_HOST:
            return api or httpx.Response(
                400, text=SETTING_REASON, headers={"Content-Type": "text/plain"}
            )
        assert request.url.host == "sfgov.legistar.com"
        assert request.url.path == "/Calendar.aspx"
        cookie = request.headers.get("cookie")
        if cookie is None:
            return first_page or httpx.Response(
                200,
                text="<html></html>",
                headers=[
                    ("Set-Cookie", "ASP.NET_SessionId=abc; path=/; secure; HttpOnly"),
                    ("Set-Cookie", "Setting-60-Calendar Options=info|; path=/; secure"),
                    ("Set-Cookie", "Setting-60-Calendar Year=This Month; path=/; secure"),
                ],
            )
        year = cookie.split("Calendar Year=")[1].split(";")[0]
        order = "DESC" if "MeetingStartDate DESC" in cookie else "ASC"
        page = pages[(year, order)]
        return httpx.Response(503, text="busy") if page == "503" else httpx.Response(200, text=page)

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return route(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Legistar(client=client, today=lambda: date(2026, 11, 21)), requests


def page_requests(requests: list[httpx.Request]) -> list[str]:
    return [r.headers["cookie"] for r in requests if "cookie" in r.headers]


@pytest.mark.asyncio
async def test_a_misconfigured_events_api_falls_back_to_the_calendar_page() -> None:
    page = calendar_html(
        "2026",
        [
            calendar_row("Audit and Oversight Committee", "11/19/2026", 3, minutes=False),
            calendar_row("Board of Supervisors", "11/17/2026", 2),
            calendar_row("Rules Committee", "10/1/2026", 1),
        ],
    )
    legistar, requests = calendar_site({("2026", "DESC"): page})

    found = await legistar.find_meetings(
        client="sfgov", start_date="2026-11-01", end_date="2026-11-30"
    ).run()

    rows = found.result.rows
    assert [row["EventDate"] for row in rows] == ["2026-11-19", "2026-11-17"]
    assert all(tuple(row) == legistar_module.CALENDAR_FIELDS for row in rows)
    assert rows[0]["EventMinutesFile"] is None
    assert rows[1]["EventAgendaFile"] == (
        f"https://sfgov.legistar.com/View.ashx?M=A&ID=2&GUID={CAL_GUID}"
    )
    assert rows[1]["EventInSiteURL"].startswith(
        f"https://sfgov.legistar.com/MeetingDetail.aspx?ID=2&GUID={CAL_GUID}"
    )
    # The page's own link gives the ID and GUID that the meeting page needs.
    assert (rows[1]["EventId"], rows[1]["EventGuid"]) == (2, CAL_GUID)
    assert rows[1]["EventLocation"] == "City Hall, Room 250; Regular Meeting"
    assert rows[1]["EventTime"] == "10:00 AM"
    assert found.result.completeness is Completeness.COMPLETE
    assert found.result.domain == "sfgov.legistar.com"
    assert found.result.query_url == "https://sfgov.legistar.com/Calendar.aspx"
    assert found.escalations == ["LEGISTAR_WEB_CALENDAR"]
    assert found.warnings[0] == legistar_module.calendar_warning("sfgov")
    assert {r.url.host for r in requests} == {LEGISTAR_HOST, "sfgov.legistar.com"}
    # The cookies the site set on the first answer never replace the ones asked for.
    assert page_requests(requests) == [
        "Setting-60-Calendar Year=2026; Setting-60-Calendar Body=All; "
        "Setting-60-ASP.calendar_aspx.gridCalendar.SortExpression=MeetingStartDate DESC"
    ]


@pytest.mark.asyncio
async def test_an_older_range_also_reads_the_oldest_first_view() -> None:
    newest = calendar_html(
        "2026",
        [calendar_row("Board", "11/19/2026", 9), calendar_row("Board", "5/13/2026", 8)],
        pager=True,
    )
    oldest = calendar_html(
        "2026",
        [
            calendar_row("Board", "1/5/2026", 1),
            calendar_row("Board", "3/10/2026", 2),
            calendar_row("Board", "6/11/2026", 3),
        ],
        pager=True,
    )
    legistar, requests = calendar_site({("2026", "DESC"): newest, ("2026", "ASC"): oldest})

    found = await legistar.find_meetings(
        client="sfgov", start_date="2026-03-01", end_date="2026-03-31"
    ).run()

    assert [row["EventDate"] for row in found.result.rows] == ["2026-03-10"]
    assert found.result.completeness is Completeness.COMPLETE
    assert [cookie[-4:] for cookie in page_requests(requests)] == ["DESC", " ASC"]


@pytest.mark.asyncio
async def test_a_recent_range_reads_only_the_newest_first_view() -> None:
    newest = calendar_html(
        "2026",
        [calendar_row("Board", "11/19/2026", 9), calendar_row("Board", "5/13/2026", 8)],
        pager=True,
    )
    legistar, requests = calendar_site({("2026", "DESC"): newest})

    found = await legistar.find_meetings(
        client="sfgov", start_date="2026-06-01", end_date="2026-11-30"
    ).run()

    assert found.result.returned_row_count == 1
    assert found.result.completeness is Completeness.COMPLETE
    assert len(page_requests(requests)) == 1


@pytest.mark.asyncio
async def test_dates_neither_view_reaches_are_named_and_partial() -> None:
    newest = calendar_html(
        "2026",
        [calendar_row("Board", "11/19/2026", 9), calendar_row("Board", "9/1/2026", 8)],
        pager=True,
    )
    oldest = calendar_html(
        "2026",
        [calendar_row("Board", "1/5/2026", 1), calendar_row("Board", "4/30/2026", 2)],
        pager=True,
    )
    legistar, _ = calendar_site({("2026", "DESC"): newest, ("2026", "ASC"): oldest})

    found = await legistar.find_meetings(
        client="sfgov", start_date="2026-04-01", end_date="2026-10-31"
    ).run()

    assert [row["EventDate"] for row in found.result.rows] == ["2026-09-01", "2026-04-30"]
    assert found.result.completeness is Completeness.TRUNCATED
    assert found.escalations == ["LEGISTAR_WEB_CALENDAR", "LEGISTAR_CALENDAR_GAP"]
    assert "2026-04-30 to 2026-09-01" in found.warnings[0]
    assert "2026-04-30 to 2026-09-01" in found.rules[0]


@pytest.mark.asyncio
async def test_two_calendar_years_and_next_year_are_read() -> None:
    pages = {
        ("2026", "DESC"): calendar_html("2026", [calendar_row("Board", "1/6/2026", 2)]),
        ("2025", "DESC"): calendar_html("2025", [calendar_row("Board", "12/16/2025", 1)]),
        ("Next Year", "DESC"): calendar_html("Next Year", [calendar_row("Board", "1/12/2027", 3)]),
    }
    legistar, requests = calendar_site(pages)

    across = await legistar.find_meetings(
        client="sfgov", start_date="2025-12-01", end_date="2026-01-31"
    ).run()
    ahead = await legistar.find_meetings(
        client="sfgov", start_date="2026-12-01", end_date="2027-01-31"
    ).run()

    assert [row["EventDate"] for row in across.result.rows] == ["2026-01-06", "2025-12-16"]
    assert across.result.completeness is Completeness.COMPLETE
    assert [row["EventDate"] for row in ahead.result.rows] == ["2027-01-12"]
    assert ahead.result.completeness is Completeness.COMPLETE
    # Each call learns the settings number afresh, with no cookie from the call before.
    assert len(page_requests(requests)) == 4


@pytest.mark.asyncio
async def test_years_past_next_year_are_named_or_leave_the_api_error() -> None:
    pages = {("Next Year", "DESC"): calendar_html("Next Year", [])}
    legistar, requests = calendar_site(pages)

    partly = await legistar.find_meetings(
        client="sfgov", start_date="2027-12-01", end_date="2028-01-31"
    ).run()
    with pytest.raises(SourceQueryError) as caught:
        await legistar.find_meetings(
            client="sfgov", start_date="2028-01-01", end_date="2028-01-31"
        ).run()

    assert partly.result.completeness is Completeness.TRUNCATED
    assert "2028-01-01 to 2028-01-31" in partly.warnings[0]
    assert caught.value.code == "CLIENT_MISCONFIGURED"
    assert len(page_requests(requests)) == 1


@pytest.mark.asyncio
async def test_a_full_page_without_a_pager_still_reads_the_oldest_first_view() -> None:
    rows = [
        calendar_row("Board", f"{12 - n // 10}/{28 - n % 10}/2026", 1000 - n)
        for n in range(legistar_module.CALENDAR_PAGE_ROWS)
    ]
    oldest = calendar_html("2026", [calendar_row("Board", "1/5/2026", 1)])
    legistar, requests = calendar_site(
        {("2026", "DESC"): calendar_html("2026", rows), ("2026", "ASC"): oldest}
    )

    found = await legistar.find_meetings(
        client="sfgov", start_date="2026-01-01", end_date="2026-12-31", limit=200
    ).run()

    assert len(page_requests(requests)) == 2
    assert found.result.returned_row_count == legistar_module.CALENDAR_PAGE_ROWS + 1


@pytest.mark.asyncio
async def test_a_meeting_in_both_views_is_listed_once_and_a_one_day_gap_is_named() -> None:
    newest = calendar_html(
        "2026",
        [calendar_row("Board", "11/19/2026", 9), calendar_row("Board", "6/1/2026", 5)],
        pager=True,
    )
    oldest = calendar_html(
        "2026",
        [calendar_row("Board", "1/5/2026", 1), calendar_row("Board", "6/1/2026", 5)],
        pager=True,
    )
    legistar, requests = calendar_site({("2026", "DESC"): newest, ("2026", "ASC"): oldest})

    # The range starts on the oldest date the newest-first view shows.
    found = await legistar.find_meetings(
        client="sfgov", start_date="2026-06-01", end_date="2026-07-01"
    ).run()

    assert [row["EventDate"] for row in found.result.rows] == ["2026-06-01"]
    assert found.result.completeness is Completeness.TRUNCATED
    assert "2026-06-01 to 2026-06-01" in found.warnings[0]
    assert len(page_requests(requests)) == 2


@pytest.mark.asyncio
async def test_a_slow_calendar_gives_back_the_api_error_within_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legistar_module, "CALENDAR_TIMEOUT_SECONDS", 0.05)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == LEGISTAR_HOST:
            return httpx.Response(400, text=SETTING_REASON)
        await asyncio.sleep(1)
        return httpx.Response(200, text="<html></html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    legistar = Legistar(client=client, today=lambda: date(2026, 11, 21))

    with pytest.raises(SourceQueryError) as caught:
        await legistar.find_meetings(
            client="sfgov", start_date="2026-11-01", end_date="2026-11-30"
        ).run()

    assert caught.value.code == "CLIENT_MISCONFIGURED"


@pytest.mark.asyncio
async def test_more_than_two_calendar_years_is_refused_before_the_site_is_read() -> None:
    legistar, requests = calendar_site({})

    with pytest.raises(SourceQueryError) as caught:
        await legistar.find_meetings(
            client="sfgov", start_date="2024-01-01", end_date="2026-11-30"
        ).run()

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert caught.value.argument == "start_date"
    assert {r.url.host for r in requests} == {LEGISTAR_HOST}


@pytest.mark.asyncio
async def test_body_words_filter_the_calendar_and_the_limit_truncates() -> None:
    page = calendar_html(
        "2026",
        [
            calendar_row("Board of Supervisors", "11/17/2026", 3),
            calendar_row("Rules Committee", "11/16/2026", 2),
            calendar_row("Board of Supervisors", "11/10/2026", 1),
        ],
    )
    legistar, _ = calendar_site({("2026", "DESC"): page})

    found = await legistar.find_meetings(
        client="sfgov", start_date="2026-11-01", end_date="2026-11-30", body="BOARD", limit=1
    ).run()

    assert [row["EventDate"] for row in found.result.rows] == ["2026-11-17"]
    assert found.result.completeness is Completeness.TRUNCATED


@pytest.mark.asyncio
async def test_a_calendar_link_to_another_host_is_left_out_and_named() -> None:
    page = calendar_html(
        "2026",
        [calendar_row("Board", "11/17/2026", 1, agenda="https://example.org/agenda.pdf")],
    )
    legistar, _ = calendar_site({("2026", "DESC"): page})

    found = await legistar.find_meetings(
        client="sfgov", start_date="2026-11-01", end_date="2026-11-30"
    ).run()

    assert found.result.rows[0]["EventAgendaFile"] is None
    assert legistar_module.UNREAD_LINK_WARNING in found.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pages", "first_page"),
    [
        # The page ignored the year cookie and showed its default span.
        ({("2026", "DESC"): calendar_html("This Month", [])}, None),
        # The page ignored the order cookie.
        (
            {
                ("2026", "DESC"): calendar_html(
                    "2026",
                    [calendar_row("Board", "11/1/2026", 1), calendar_row("Board", "11/9/2026", 2)],
                )
            },
            None,
        ),
        # The page showed one body instead of the whole calendar.
        (
            {
                ("2026", "DESC"): calendar_html(
                    "2026", [calendar_row("Board", "11/9/2026", 1)], body="Board"
                )
            },
            None,
        ),
        # A row was never closed, so the next one would swallow it.
        (
            {
                ("2026", "DESC"): calendar_html(
                    "2026",
                    [
                        calendar_row("Board", "11/9/2026", 2).removesuffix("</tr>"),
                        calendar_row("Board", "11/2/2026", 1),
                    ],
                )
            },
            None,
        ),
        # The oldest-first view failed after the newest-first view was read.
        (
            {
                ("2026", "DESC"): calendar_html(
                    "2026",
                    [calendar_row("Board", "11/19/2026", 2), calendar_row("Board", "11/2/2026", 1)],
                    pager=True,
                ),
                ("2026", "ASC"): "503",
            },
            None,
        ),
        # The site set no calendar cookie to learn its settings number from.
        ({}, httpx.Response(200, text="<html></html>")),
        ({}, httpx.Response(503, text="busy")),
    ],
)
async def test_an_unreadable_calendar_leaves_the_api_error(
    pages: dict[tuple[str, str], str], first_page: httpx.Response | None
) -> None:
    legistar, _ = calendar_site(pages, first_page=first_page)

    with pytest.raises(SourceQueryError) as caught:
        await legistar.find_meetings(
            client="sfgov", start_date="2026-11-01", end_date="2026-11-30"
        ).run()

    assert caught.value.code == "CLIENT_MISCONFIGURED"
    assert SETTING_REASON in caught.value.public_message


@pytest.mark.asyncio
async def test_other_api_errors_never_reach_the_website() -> None:
    unknown = httpx.Response(
        500, json={"ExceptionMessage": "LegistarConnectionString setting is not set up"}
    )
    legistar, requests = calendar_site({}, api=unknown)

    with pytest.raises(SourceQueryError) as caught:
        await legistar.find_meetings(
            client="sfgov", start_date="2026-11-01", end_date="2026-11-30"
        ).run()

    assert caught.value.code == "UNKNOWN_CLIENT"
    assert {r.url.host for r in requests} == {LEGISTAR_HOST}

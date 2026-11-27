from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from opencivicquery.models import Completeness
from opencivicquery.sources import sfgov
from opencivicquery.sources.base import SourceError, SourceQueryError
from opencivicquery.sources.sfgov import (
    DOCUMENT_SEARCH_FIELDS,
    DOCUMENT_SEARCH_WARNING,
    LINK_RULE,
    MEETING_DOCUMENTS_WARNING,
    MEETING_FIELDS,
    MEETING_SPAN_WARNING,
    NEWS_AGENCY_WARNING,
    NEWS_FIELDS,
    NEWS_NO_AGENCY_WARNING,
    NO_MEETINGS_WARNING,
    NO_NEWS_WARNING,
    NO_SEARCH_MATCH_WARNING,
    PAGE_SEARCH_FIELDS,
    PAGE_SEARCH_WARNING,
    SEARCH_NO_AGENCY_WARNING,
    SFGOV_HOST,
    UNDOCUMENTED_WARNING,
    SfGov,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sfgov"


def fixture(name: str) -> dict[str, Any]:
    """A response recorded from the live interface on 2026-11-26, long text trimmed."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


NEWS = fixture("news_mayor")
MAYOR_AGENCIES = fixture("agency_search_mayor_office")
POLICE_AGENCY = fixture("agency_search_police_commission")
MEETINGS = fixture("meetings_police_commission")
HEALTH_MEETING = fixture("meetings_health_commission")
PAGE_SEARCH = fixture("search_pages")
DOCUMENT_SEARCH = fixture("search_documents")
EMPTY = {"meta": {"total_count": 0}, "items": []}

Route = Callable[[httpx.Request], httpx.Response]


def source_answering(route: Route) -> tuple[SfGov, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return route(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SfGov(client=client, min_request_interval=0), requests


def by_type(**answers: Any) -> Route:
    """Answer each request from the body registered for its page type or path."""

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/documents/":
            key = "documents"
        else:
            key = request.url.params.get("type", "untyped").removeprefix("sf.").lower()
        answer = answers[key]
        if callable(answer):
            answer = answer(request)
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)

    return route


def listing(items: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {"meta": {"total_count": len(items) if total is None else total}, "items": items}


@pytest.mark.asyncio
async def test_news_resolves_the_agency_and_keeps_only_the_dates_asked_for() -> None:
    source, requests = source_answering(by_type(agency=MAYOR_AGENCIES, news=listing(NEWS["items"])))

    found = await source.find_news(
        agency="  office of the  mayor ", start_date="2026-11-24", end_date="2026-11-30"
    ).run()

    lookup, news = requests
    assert {request.url.host for request in requests} == {SFGOV_HOST}
    assert all(request.url.path == "/api/v2/pages/" for request in requests)
    assert dict(lookup.url.params) == {
        "type": "sf.Agency",
        "locale": "en",
        "search": "office of the mayor",
        "limit": "50",
    }
    assert dict(news.url.params) == {
        "type": "sf.News",
        "locale": "en",
        "primary_agency": "559",
        "order": "-date",
        "limit": "500",
    }
    assert news.headers["User-Agent"].startswith("OpenCivicQuery/")
    assert "Mozilla" not in news.headers["User-Agent"]
    result = found.result
    assert result.completeness == Completeness.COMPLETE
    (row,) = result.rows
    assert list(row) == list(NEWS_FIELDS)
    assert row["date"] == "2026-11-24"
    assert row["page_id"] == 110692
    assert row["news_type"] == "news"
    assert row["page_url"] == (
        "https://www.sf.gov/news-mayor-lurie-signs-legislation-increasing-relocation-"
        "payments-for-displaced-renters"
    )
    assert "body" not in row and "image" not in row
    assert result.dataset_name == "SF.gov news: Office of the Mayor, 2026-11-24 to 2026-11-30"
    assert result.source_url == "https://www.sf.gov/departments--office-mayor"
    assert result.query_url is not None
    assert result.query_url.startswith(f"https://{SFGOV_HOST}/api/v2/pages/?type=sf.News&")
    assert "2026-11-24 through 2026-11-30" in result.query
    assert found.warnings[0] == NEWS_AGENCY_WARNING
    assert found.warnings[-1] == UNDOCUMENTED_WARNING
    assert found.rules == [LINK_RULE]


@pytest.mark.asyncio
async def test_news_is_complete_once_the_read_passes_the_start_date() -> None:
    # The fixture holds 3 of the agency's 277 items, dated 2026-11-24 and 2026-11-23.
    source, _ = source_answering(by_type(agency=MAYOR_AGENCIES, news=NEWS))

    found = await source.find_news(
        agency="Office of the Mayor", start_date="2026-11-24", end_date="2026-11-24"
    ).run()

    assert found.result.completeness == Completeness.COMPLETE
    assert found.result.returned_row_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("start_date", ["2026-11-23", "2026-10-01"])
async def test_news_that_stops_inside_the_range_is_partial(start_date: str) -> None:
    source, _ = source_answering(by_type(agency=MAYOR_AGENCIES, news=NEWS))

    found = await source.find_news(
        agency="Office of the Mayor", start_date=start_date, end_date="2026-11-30"
    ).run()

    assert found.result.completeness == Completeness.TRUNCATED
    assert found.warnings[0].startswith("PARTIAL: this lookup read the 3 newest of 277")
    assert "2026-11-23 or earlier may be missing" in found.warnings[0]
    assert NO_NEWS_WARNING not in found.warnings


@pytest.mark.asyncio
async def test_news_without_an_agency_sends_one_request_and_says_rows_name_no_agency() -> None:
    source, requests = source_answering(by_type(news=listing(NEWS["items"])))

    found = await source.find_news(start_date="2020-01-01", end_date="2020-12-31").run()

    (sent,) = requests
    assert "primary_agency" not in sent.url.params
    assert found.result.rows == []
    assert found.result.completeness == Completeness.COMPLETE
    assert found.warnings[:2] == [NO_NEWS_WARNING, NEWS_NO_AGENCY_WARNING]
    assert found.result.source_url == "https://www.sf.gov/news"
    assert found.result.dataset_name.startswith("SF.gov news: all agencies")


def _news_with(change: Callable[[list[dict[str, Any]]], None]) -> dict[str, Any]:
    items = copy.deepcopy(NEWS["items"])
    change(items)
    return listing(items)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        _news_with(lambda items: items.reverse()),
        _news_with(lambda items: items[0].update(date=None)),
        _news_with(lambda items: items[0].update(date="11/24/2026")),
        _news_with(lambda items: items[0].update(title="")),
        _news_with(lambda items: items[0]["meta"].update(id="110692")),
        _news_with(lambda items: items[0]["meta"].update(id=True)),
        _news_with(lambda items: items[1]["meta"].update(id=items[0]["meta"]["id"])),
        _news_with(lambda items: items[0]["meta"].update(html_url="https://evil.example/x/")),
        _news_with(lambda items: items[0]["meta"].update(html_url="http://api.sf.gov/a/?b=1")),
        _news_with(lambda items: items[0].update(abstract=["not", "text"])),
        {"meta": {"total_count": 5}, "items": []},
        {"meta": {"total_count": 1}, "items": NEWS["items"]},
        {"meta": {"total_count": "3"}, "items": NEWS["items"]},
        {"meta": {}, "items": []},
        {"items": [None], "meta": {"total_count": 1}},
        [],
    ],
)
async def test_a_news_list_in_an_unexpected_shape_is_a_source_error(body: Any) -> None:
    source, _ = source_answering(by_type(news=body))

    with pytest.raises(SourceError) as caught:
        await source.find_news(start_date="2026-11-01", end_date="2026-11-30").run()

    assert not isinstance(caught.value, SourceQueryError)


@pytest.mark.asyncio
async def test_an_inexact_agency_name_returns_candidates_and_never_picks_one() -> None:
    source, requests = source_answering(by_type(agency=MAYOR_AGENCIES, news=NEWS))

    with pytest.raises(SourceQueryError) as caught:
        await source.find_news(
            agency="Mayor’s office", start_date="2026-10-01", end_date="2026-10-31"
        ).run()

    (lookup,) = requests
    assert lookup.url.params["search"] == "Mayor office"
    assert caught.value.code == "NOT_FOUND"
    assert caught.value.argument == "agency"
    message = caught.value.public_message
    assert "no agency page titled exactly Mayor’s office" in message
    assert "Office of the Mayor; Mayor's Office of Innovation" in message
    assert "(5)" in message


@pytest.mark.asyncio
async def test_an_apostrophe_style_does_not_stop_an_exact_agency_match() -> None:
    source, requests = source_answering(by_type(agency=MAYOR_AGENCIES, news=EMPTY))

    found = await source.find_news(
        agency="mayor’s office of innovation", start_date="2026-10-01", end_date="2026-10-31"
    ).run()

    assert requests[1].url.params["primary_agency"] == "185"
    assert "Mayor's Office of Innovation" in found.result.dataset_name


@pytest.mark.asyncio
async def test_an_unknown_agency_and_a_repeated_title_are_refused() -> None:
    source, _ = source_answering(by_type(agency=EMPTY))
    with pytest.raises(SourceQueryError) as missing:
        await source.find_meetings(
            agency="Zzz Board", start_date="2025-01-01", end_date="2025-12-31"
        ).run()
    assert missing.value.code == "NOT_FOUND"
    assert "Use fewer words" in missing.value.public_message

    twice = listing(POLICE_AGENCY["items"] * 2)
    source, requests = source_answering(by_type(agency=twice))
    with pytest.raises(SourceQueryError) as repeated:
        await source.find_meetings(
            agency="Police Commission", start_date="2025-01-01", end_date="2025-12-31"
        ).run()
    assert repeated.value.code == "AMBIGUOUS_AGENCY"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_a_long_candidate_list_says_how_many_it_shows() -> None:
    many = listing(
        [
            {
                "id": 1000 + number,
                "meta": {"type": "sf.Agency", "html_url": f"http://api.sf.gov/body-{number}/"},
                "title": f"Arts Committee {number}",
            }
            for number in range(12)
        ],
        total=64,
    )
    source, _ = source_answering(by_type(agency=many))

    with pytest.raises(SourceQueryError) as caught:
        await source.find_meetings(
            agency="Arts Committee", start_date="2025-01-01", end_date="2025-12-31"
        ).run()

    assert "(10 of 64)" in caught.value.public_message
    assert "Arts Committee 9" in caught.value.public_message
    assert "Arts Committee 10" not in caught.value.public_message


@pytest.mark.parametrize(
    ("call", "argument"),
    [
        (lambda s: s.find_news(start_date="2026-10-01", end_date="2026-09-30"), "end_date"),
        (lambda s: s.find_news(start_date="10/01/2026", end_date="2026-10-31"), "start_date"),
        (lambda s: s.find_news(start_date="2026-02-30", end_date="2026-10-31"), "start_date"),
        (lambda s: s.find_news(start_date="1800-01-01", end_date="2026-10-31"), "start_date"),
        (lambda s: s.find_news(start_date="2026-10-01", end_date=None), "end_date"),
        (lambda s: s.find_news(start_date="٢٠٢٦-١٠-٠١", end_date="2026-10-31"), "start_date"),
        (
            lambda s: s.find_news(agency="x", start_date="2026-10-01", end_date="2026-10-31"),
            "agency",
        ),
        (
            lambda s: s.find_news(agency="' -", start_date="2026-10-01", end_date="2026-10-31"),
            "agency",
        ),
        (
            lambda s: s.find_meetings(
                agency="Police\u200bCommission", start_date="2025-01-01", end_date="2025-12-31"
            ),
            "agency",
        ),
        (
            lambda s: s.find_meetings(
                agency="a" * 121, start_date="2025-01-01", end_date="2025-12-31"
            ),
            "agency",
        ),
        (
            lambda s: s.find_meetings(agency=None, start_date="2025-01-01", end_date="2025-12-31"),
            "agency",
        ),
        (lambda s: s.search(text="x"), "text"),
        (lambda s: s.search(text="?!"), "text"),
        (lambda s: s.search(text="__"), "text"),
        (lambda s: s.search(text="a b " * 300), "text"),
        (
            lambda s: s.find_meetings(agency="__", start_date="2025-01-01", end_date="2025-12-31"),
            "agency",
        ),
        (
            lambda s: s.find_meetings(agency="", start_date="2025-01-01", end_date="2025-12-31"),
            "agency",
        ),
        (lambda s: s.search(text=["budget"]), "text"),
        (lambda s: s.search(text="budget", kind=7), "kind"),
        (lambda s: s.find_news(start_date=20261001, end_date="2026-10-31"), "start_date"),
        (lambda s: s.search(text="a" * 101), "text"),
        (lambda s: s.search(text="budget\x00report"), "text"),
        (lambda s: s.search(text="budget", kind="images"), "kind"),
        (lambda s: s.search(text="budget", kind=None), "kind"),
    ],
)
def test_invalid_requests_are_refused_before_any_request(
    call: Callable[[SfGov], Any], argument: str
) -> None:
    source, requests = source_answering(by_type())

    with pytest.raises(SourceQueryError) as caught:
        call(source)

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert caught.value.argument == argument
    assert requests == []


@pytest.mark.asyncio
async def test_meetings_are_filtered_by_date_and_sorted_newest_first() -> None:
    source, requests = source_answering(by_type(agency=POLICE_AGENCY, meeting=MEETINGS))

    found = await source.find_meetings(
        agency="Police Commission", start_date="2025-01-01", end_date="2025-12-31"
    ).run()

    lookup, meetings = requests
    assert {request.url.host for request in requests} == {SFGOV_HOST}
    assert dict(meetings.url.params) == {
        "type": "sf.Meeting",
        "locale": "en",
        "primary_agency": "3005",
        "order": "-id",
        "fields": "date_time,cancelled,videos,related_documents",
        "limit": "100",
    }
    result = found.result
    assert result.completeness == Completeness.COMPLETE
    assert all(list(row) == list(MEETING_FIELDS) for row in result.rows)
    assert [row["meeting_date"] for row in result.rows] == [
        "2025-10-22",
        "2025-09-03",
        "2025-08-27",
        "2025-08-13",
        None,
    ]
    september = result.rows[1]
    assert september["start_time"] == "17:30:00"
    assert september["other_dates"] == []
    assert september["cancelled"] is False
    assert september["page_url"].startswith("https://www.sf.gov/")
    (minutes,) = september["documents"]
    assert minutes["group"] == "September 3, 2025 Minutes"
    assert minutes["url"].startswith("https://media.api.sf.gov/documents/")
    assert result.rows[2]["documents"] == []
    undated = result.rows[-1]
    assert undated["page_id"] == 5312 and undated["start_time"] is None
    assert result.dataset_name == (
        "SF.gov meeting pages: Police Commission, 2025-01-01 to 2025-12-31"
    )
    assert result.source_url == "https://www.sf.gov/departments--police-commission"
    assert found.warnings[0].startswith(
        "1 meeting page(s) of this body carry no date this lookup could read"
    )
    assert MEETING_DOCUMENTS_WARNING in found.warnings
    assert NO_MEETINGS_WARNING not in found.warnings


@pytest.mark.asyncio
async def test_a_minutes_heading_keeps_the_staff_wording_and_all_day_pages_have_no_time() -> None:
    source, _ = source_answering(by_type(agency=POLICE_AGENCY, meeting=MEETINGS))

    found = await source.find_meetings(
        agency="Police Commission", start_date="2022-01-01", end_date="2024-12-31"
    ).run()

    by_id = {row["page_id"]: row for row in found.result.rows}
    march = by_id[5209]
    assert march["meeting_date"] == "2022-03-09"
    assert [document["group"] for document in march["documents"]] == [
        "Supporting documents",
        "Supporting documents",
        "Minutes",
    ]
    # checked 2026-11-26: the file under this page's Minutes heading is for another meeting.
    assert march["documents"][-1]["title"] == "2/9/22 Minutes"
    all_day = [row for row in found.result.rows if row["meeting_date"] and row["page_id"] != 5209]
    assert all_day and all(row["start_time"] is None for row in all_day)


@pytest.mark.asyncio
async def test_no_meeting_in_the_dates_warns_first() -> None:
    dated = listing([item for item in MEETINGS["items"] if item["date_time"]])
    source, _ = source_answering(by_type(agency=POLICE_AGENCY, meeting=dated))

    found = await source.find_meetings(
        agency="Police Commission", start_date="2019-01-01", end_date="2019-12-31"
    ).run()

    assert found.result.rows == []
    assert found.result.completeness == Completeness.COMPLETE
    assert found.warnings[0] == NO_MEETINGS_WARNING


@pytest.mark.asyncio
async def test_video_links_come_from_the_page_and_unread_entries_are_counted() -> None:
    body = copy.deepcopy(HEALTH_MEETING)
    item = body["items"][0]
    item["videos"].append(
        {
            "type": "video",
            "value": {
                "title": "Clip",
                "video_type": [
                    {"type": "upload", "value": {"file": 12}},
                    {"type": "external_link", "value": {"url": "javascript:alert(1)"}},
                    {
                        "type": "external_link",
                        "value": {"url": "https://sfgovtv.org@evil.example/v"},
                    },
                ],
            },
        }
    )
    item["related_documents"] = [
        {"type": "rich_text", "value": "<p>See the clerk</p>"},
        {
            "type": "downloadable_files",
            "value": {
                "title": "Minutes",
                "documents": [
                    {"type": "document", "value": None},
                    {
                        "type": "document",
                        "value": {
                            "id": 7,
                            "title": "Elsewhere",
                            "file": "https://files.example.org/a.pdf",
                            "meta": {"download_url": "https://files.example.org/b.pdf"},
                        },
                    },
                    {
                        "type": "document",
                        "value": {
                            "id": 8,
                            "title": "Kept",
                            "file": "ftp://media.api.sf.gov/a.pdf",
                            "meta": {"download_url": "https://api.sf.gov/documents/8/a.pdf"},
                        },
                    },
                ],
            },
        },
    ]
    agency = listing(
        [
            {
                "id": 2911,
                "meta": {
                    "type": "sf.Agency",
                    "html_url": "http://api.sf.gov/departments--health-commission/",
                },
                "title": "San Francisco Health Commission",
            }
        ]
    )
    source, _ = source_answering(by_type(agency=agency, meeting=body))

    found = await source.find_meetings(
        agency="San Francisco Health Commission", start_date="2026-11-01", end_date="2026-11-30"
    ).run()

    (row,) = found.result.rows
    assert row["meeting_date"] == "2026-11-02" and row["start_time"] == "16:00:00"
    assert row["video_links"] == [
        {
            "title": "Meeting Video",
            "url": "https://sanfrancisco.granicus.com/player/clip/53478?view_id=171&redirect=true",
        }
    ]
    assert row["documents"] == [
        {"group": "Minutes", "title": "Kept", "url": "https://api.sf.gov/documents/8/a.pdf"}
    ]
    assert found.warnings[0].startswith("6 attachment or video entries on these pages")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        lambda block: block.pop("value"),
        lambda block: block.update(type="recurring"),
        lambda block: block["value"].update(start_date=None),
        lambda block: block["value"].update(start_date="2025-13-40"),
        lambda block: block["value"].update(start_time="5:30 pm"),
        lambda block: block["value"].update(is_all_day="no"),
    ],
)
async def test_a_page_with_an_unreadable_date_is_listed_last_and_never_dated(
    change: Callable[[dict[str, Any]], None],
) -> None:
    body = copy.deepcopy(MEETINGS)
    newest = body["items"][0]
    newest["date_time"].append(copy.deepcopy(newest["date_time"][0]))
    change(newest["date_time"][1])
    source, _ = source_answering(by_type(agency=POLICE_AGENCY, meeting=body))

    found = await source.find_meetings(
        agency="Police Commission", start_date="2025-01-01", end_date="2025-12-31"
    ).run()

    unreadable = [row for row in found.result.rows if row["meeting_date"] is None]
    assert [row["page_id"] for row in unreadable] == [newest["id"], 5312]
    assert found.result.rows[-2:] == unreadable
    assert found.warnings[0].startswith("2 meeting page(s) of this body carry no date")


@pytest.mark.asyncio
async def test_a_page_with_two_dates_is_kept_when_either_is_in_the_range() -> None:
    body = copy.deepcopy(MEETINGS)
    page = next(item for item in body["items"] if item["id"] == 80617)
    second = copy.deepcopy(page["date_time"][0])
    second["value"].update(start_date="2026-01-07", start_time="09:00:00")
    page["date_time"].append(second)
    source, _ = source_answering(by_type(agency=POLICE_AGENCY, meeting=body))

    found = await source.find_meetings(
        agency="Police Commission", start_date="2026-01-01", end_date="2026-01-31"
    ).run()

    (row,) = [row for row in found.result.rows if row["meeting_date"]]
    assert row["page_id"] == 80617
    assert row["meeting_date"] == "2026-01-07" and row["start_time"] == "09:00:00"
    assert row["other_dates"] == ["2025-09-03"]


def _paged(items: list[dict[str, Any]], total: int | None = None) -> Route:
    def meeting(request: httpx.Request) -> dict[str, Any]:
        offset = int(request.url.params.get("offset", "0"))
        size = int(request.url.params["limit"])
        return listing(items[offset : offset + size], len(items) if total is None else total)

    return by_type(agency=POLICE_AGENCY, meeting=meeting)


@pytest.mark.asyncio
async def test_meetings_are_read_page_by_page_until_all_are_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sfgov, "MEETING_PAGE_SIZE", 3)
    source, requests = source_answering(_paged(MEETINGS["items"]))

    found = await source.find_meetings(
        agency="Police Commission", start_date="2022-01-01", end_date="2026-12-31"
    ).run()

    # Each request after the first starts on the last page already read.
    assert [request.url.params.get("offset") for request in requests[1:]] == [None, "2", "4"]
    assert found.result.completeness == Completeness.COMPLETE
    assert found.result.returned_row_count == len(MEETINGS["items"])


@pytest.mark.asyncio
async def test_a_body_with_more_pages_than_the_cap_is_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sfgov, "MEETING_PAGE_SIZE", 2)
    monkeypatch.setattr(sfgov, "MAX_MEETING_PAGES", 2)
    source, requests = source_answering(_paged(MEETINGS["items"]))

    found = await source.find_meetings(
        agency="Police Commission", start_date="2025-01-01", end_date="2025-12-31"
    ).run()

    assert len(requests) == 3
    assert found.result.completeness == Completeness.TRUNCATED
    assert found.warnings[0].startswith(
        "PARTIAL: Police Commission has 7 meeting pages and this lookup read the 3 with the "
        "highest page ids"
    )


def _meetings_with(change: Callable[[list[dict[str, Any]]], None]) -> dict[str, Any]:
    items = copy.deepcopy(MEETINGS["items"])
    change(items)
    return listing(items)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        _meetings_with(lambda items: items.reverse()),
        _meetings_with(lambda items: items.insert(1, items[0])),
        _meetings_with(lambda items: items[0].update(date_time=None)),
        _meetings_with(lambda items: items[0].update(cancelled="false")),
        _meetings_with(lambda items: items[0].update(related_documents=None)),
        _meetings_with(lambda items: items[0].pop("videos")),
        _meetings_with(lambda items: items[0]["meta"].update(type="sf.Event")),
        _meetings_with(lambda items: items[0].update(id=None)),
        _meetings_with(
            lambda items: items[2].update(
                related_documents=[{"type": "downloadable_files", "value": {"title": "x"}}]
            )
        ),
    ],
)
async def test_a_meeting_list_in_an_unexpected_shape_is_a_source_error(body: Any) -> None:
    source, _ = source_answering(by_type(agency=POLICE_AGENCY, meeting=body))

    with pytest.raises(SourceError) as caught:
        await source.find_meetings(
            agency="Police Commission", start_date="2025-01-01", end_date="2025-12-31"
        ).run()

    assert not isinstance(caught.value, SourceQueryError)


@pytest.mark.asyncio
async def test_a_meeting_list_that_changes_between_pages_is_a_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sfgov, "MEETING_PAGE_SIZE", 3)
    calls = 0

    def meeting(request: httpx.Request) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        offset = int(request.url.params.get("offset", "0"))
        return listing(MEETINGS["items"][offset : offset + 3], total=7 + (calls > 1))

    source, _ = source_answering(by_type(agency=POLICE_AGENCY, meeting=meeting))

    with pytest.raises(SourceQueryError, match="changed while it was read") as caught:
        await source.find_meetings(
            agency="Police Commission", start_date="2025-01-01", end_date="2025-12-31"
        ).run()

    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert caught.value.public_message.endswith("Try again.")


@pytest.mark.asyncio
async def test_a_page_search_keeps_titles_types_and_public_links() -> None:
    source, requests = source_answering(by_type(untyped=PAGE_SEARCH))

    found = await source.search(text="  Point-in-Time   count's ").run()

    (sent,) = requests
    assert sent.url.host == SFGOV_HOST and sent.url.path == "/api/v2/pages/"
    assert dict(sent.url.params) == {
        "search": "Point in Time count",
        "locale": "en",
        "limit": "50",
    }
    result = found.result
    assert result.completeness == Completeness.TRUNCATED
    assert result.returned_row_count == 8
    assert all(list(row) == list(PAGE_SEARCH_FIELDS) for row in result.rows)
    first = result.rows[0]
    assert first["title"] == "2024 Point-in-Time Count"
    assert first["page_type"] == "sf.Report"
    assert first["page_url"] == "https://www.sf.gov/reports--september-2024--2024-point-time-count"
    assert found.warnings[0].startswith("PARTIAL: 9 pages match")
    assert found.warnings[1:3] == [PAGE_SEARCH_WARNING, SEARCH_NO_AGENCY_WARNING]
    assert result.dataset_name == "SF.gov page title search for Point in Time count: 9 match"


@pytest.mark.asyncio
async def test_a_document_search_returns_file_links_and_names_no_agency() -> None:
    complete = listing(DOCUMENT_SEARCH["items"])
    source, requests = source_answering(by_type(documents=complete))

    found = await source.search(text="point in time count", kind=" Documents ").run()

    (sent,) = requests
    assert sent.url.path == "/api/v2/documents/"
    assert dict(sent.url.params) == {"search": "point in time count", "limit": "50"}
    result = found.result
    assert result.completeness == Completeness.COMPLETE
    assert all(list(row) == list(DOCUMENT_SEARCH_FIELDS) for row in result.rows)
    first = result.rows[0]
    assert first["document_id"] == 51716
    assert first["published_date"] == "2026-03-02"
    assert first["file_url"] == "https://media.api.sf.gov/documents/2026.03.02_LHCB_Meeting_.pdf"
    assert result.rows[1]["published_date"] is None
    assert "description" not in first
    assert found.warnings[0] == DOCUMENT_SEARCH_WARNING


@pytest.mark.asyncio
async def test_an_empty_search_is_complete_and_warns_first() -> None:
    source, _ = source_answering(by_type(untyped=EMPTY))

    found = await source.search(text="zzzzqqqxx").run()

    assert found.result.completeness == Completeness.COMPLETE
    assert found.warnings[0] == NO_SEARCH_MATCH_WARNING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("pages", listing([{"id": 1, "meta": {"type": "sf.Report"}, "title": "No address"}])),
        ("pages", listing([{"id": 1, "meta": {"html_url": "http://api.sf.gov/a/"}, "title": "x"}])),
        ("pages", {"meta": {"total_count": 4}, "items": []}),
        ("documents", listing([{"id": 1, "title": "No link", "meta": {}}])),
        (
            "documents",
            listing([{"id": 1, "title": "Elsewhere", "file": "https://files.example.org/a.pdf"}]),
        ),
        (
            "documents",
            listing(
                [{"id": "1", "title": "x", "file": "https://media.api.sf.gov/documents/a.pdf"}]
            ),
        ),
    ],
)
async def test_a_search_answer_in_an_unexpected_shape_is_a_source_error(
    kind: str, body: Any
) -> None:
    source, _ = source_answering(by_type(untyped=body, documents=body))

    with pytest.raises(SourceError) as caught:
        await source.search(text="budget", kind=kind).run()

    assert not isinstance(caught.value, SourceQueryError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code", "message"),
    [
        (
            httpx.Response(400, json={"message": "cannot order by 'date' while searching"}),
            "QUERY_REJECTED",
            "cannot order by 'date' while searching",
        ),
        (httpx.Response(500, text="oops"), "SOURCE_UNAVAILABLE", "HTTP 500"),
        (httpx.Response(429, json={"message": "slow down"}), "SOURCE_RATE_LIMITED", "HTTP 429"),
        (
            httpx.Response(301, headers={"Location": "https://elsewhere.example/"}),
            "SOURCE_UNAVAILABLE",
            "HTTP 301",
        ),
    ],
)
async def test_source_refusals_and_outages_become_tool_errors(
    response: httpx.Response, code: str, message: str
) -> None:
    source, requests = source_answering(lambda _: response)

    with pytest.raises(SourceQueryError) as caught:
        await source.find_news(start_date="2026-10-01", end_date="2026-10-31").run()

    assert caught.value.code == code
    assert message in caught.value.public_message
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_a_source_error() -> None:
    source, _ = source_answering(lambda _: httpx.Response(200, text="<html>Welcome</html>"))

    with pytest.raises(SourceError):
        await source.search(text="budget").run()


@pytest.mark.asyncio
async def test_requests_are_spaced_and_never_overlap() -> None:
    now = 100.0
    waits: list[float] = []
    in_flight = 0
    most_in_flight = 0

    async def sleep(seconds: float) -> None:
        nonlocal now
        waits.append(seconds)
        now += seconds

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, most_in_flight
        in_flight += 1
        most_in_flight = max(most_in_flight, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        key = request.url.params["type"]
        body = MAYOR_AGENCIES if key == "sf.Agency" else listing(NEWS["items"])
        return httpx.Response(200, json=body)

    source = SfGov(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        min_request_interval=1.0,
        clock=lambda: now,
        sleep=sleep,
    )

    await asyncio.gather(
        source.find_news(
            agency="Office of the Mayor", start_date="2026-11-01", end_date="2026-11-30"
        ).run(),
        source.find_news(start_date="2026-11-01", end_date="2026-11-30").run(),
    )

    # Three requests: the first goes at once, each later one waits the full interval.
    assert waits == [1.0, 1.0]
    assert most_in_flight == 1


def test_page_addresses_map_to_the_public_site_or_fail() -> None:
    assert (
        sfgov._page_url("http://api.sf.gov/departments--police-commission/")
        == "https://www.sf.gov/departments--police-commission"
    )
    assert (
        sfgov._page_url("http://api.sf.gov/a--b/news--c_d/") == "https://www.sf.gov/a--b/news--c_d"
    )
    for bad in (
        None,
        "",
        "/news--x/",
        "http://www.sf.gov/x/",
        "http://api.sf.gov.evil.example/x/",
        "http://api.sf.gov@evil.example/x/",
        "http://api.sf.gov/x/?next=1",
        "http://api.sf.gov/x y/",
        "javascript://api.sf.gov/x",
    ):
        with pytest.raises(SourceError):
            sfgov._page_url(bad)


@pytest.mark.asyncio
async def test_a_page_that_spans_days_is_kept_for_any_day_it_covers() -> None:
    # checked 2026-11-26: page 83242 runs from 2025-10-22 to 2025-10-29; other pages carry
    # an end_date before their start_date, which is ignored.
    source, _ = source_answering(by_type(agency=POLICE_AGENCY, meeting=MEETINGS))

    found = await source.find_meetings(
        agency="Police Commission", start_date="2025-10-29", end_date="2025-10-29"
    ).run()

    (row,) = [row for row in found.result.rows if row["meeting_date"]]
    assert row["page_id"] == 83242
    assert row["meeting_date"] == "2025-10-22" and row["end_date"] == "2025-10-29"
    assert MEETING_SPAN_WARNING in found.warnings
    assert NO_MEETINGS_WARNING not in found.warnings


@pytest.mark.asyncio
async def test_an_end_date_before_the_start_is_ignored_and_a_broken_one_undates_the_page() -> None:
    body = copy.deepcopy(MEETINGS)
    stale = next(item for item in body["items"] if item["id"] == 80617)
    stale["date_time"][0]["value"]["end_date"] = "2025-01-08"
    broken = next(item for item in body["items"] if item["id"] == 79905)
    broken["date_time"][0]["value"]["end_date"] = "soon"
    source, _ = source_answering(by_type(agency=POLICE_AGENCY, meeting=body))

    found = await source.find_meetings(
        agency="Police Commission", start_date="2025-01-01", end_date="2025-12-31"
    ).run()

    by_id = {row["page_id"]: row for row in found.result.rows}
    assert by_id[80617]["meeting_date"] == "2025-09-03" and by_id[80617]["end_date"] is None
    assert by_id[79905]["meeting_date"] is None

    january = await source.find_meetings(
        agency="Police Commission", start_date="2025-01-01", end_date="2025-01-31"
    ).run()
    assert 80617 not in {row["page_id"] for row in january.result.rows}


@pytest.mark.asyncio
async def test_a_list_that_shifts_between_pages_is_a_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One page leaves the first batch and an older one joins, so the total and the id
    # order both still look right. Only the repeated page gives the change away.
    monkeypatch.setattr(sfgov, "MEETING_PAGE_SIZE", 3)
    before = MEETINGS["items"]
    after = [before[0], *before[2:]] + [dict(before[-1], id=1)]

    def meeting(request: httpx.Request) -> dict[str, Any]:
        offset = int(request.url.params.get("offset", "0"))
        current = after if offset else before
        return listing(current[offset : offset + 3], total=7)

    source, _ = source_answering(by_type(agency=POLICE_AGENCY, meeting=meeting))

    with pytest.raises(SourceQueryError, match="changed while it was read") as caught:
        await source.find_meetings(
            agency="Police Commission", start_date="2025-01-01", end_date="2025-12-31"
        ).run()

    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert caught.value.public_message.endswith("Try again.")


@pytest.mark.asyncio
async def test_a_server_that_returns_smaller_pages_is_still_read_without_gaps() -> None:
    def meeting(request: httpx.Request) -> dict[str, Any]:
        offset = int(request.url.params.get("offset", "0"))
        return listing(MEETINGS["items"][offset : offset + 2], total=7)

    source, requests = source_answering(by_type(agency=POLICE_AGENCY, meeting=meeting))

    found = await source.find_meetings(
        agency="Police Commission", start_date="2022-01-01", end_date="2026-12-31"
    ).run()

    # Five requests of two pages, each overlapping by one, reach six of the seven pages.
    assert [request.url.params.get("offset") for request in requests[1:]] == [
        None,
        "1",
        "2",
        "3",
        "4",
    ]
    assert found.result.completeness == Completeness.TRUNCATED
    assert "read the 6 with the highest page ids" in found.warnings[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agency", "ending"),
    [
        ("Office of the Mayor", "This lookup cannot reach further back for this agency."),
        (None, "Ask again with an agency to reach further back."),
        ("  ", "Ask again with an agency to reach further back."),
    ],
)
async def test_a_partial_news_answer_says_what_the_reader_can_do(
    agency: str | None, ending: str
) -> None:
    source, requests = source_answering(by_type(agency=MAYOR_AGENCIES, news=NEWS))

    found = await source.find_news(
        agency=agency, start_date="2026-10-01", end_date="2026-11-30"
    ).run()

    assert found.warnings[0].endswith(ending)
    assert len(requests) == (2 if agency and agency.strip() else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link",
    [
        "https://[sanfrancisco.granicus.com]/player/clip/53478",
        "http://[::1",
        "https://a\uff0fb.example/",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/admin",
        "https://localhost/video",
        "https://sfgovtv.org:8443/video",
        "https://sfgovtv.org/)[click](https://evil.example",
        "https://sfgovtv.org/" + "a" * 2000,
        "https://sfgovtv.org/a b",
        "ftp://sfgovtv.org/video",
        12,
    ],
)
async def test_a_video_link_that_is_not_a_plain_public_address_is_left_out(link: Any) -> None:
    body = copy.deepcopy(HEALTH_MEETING)
    body["items"][0]["videos"][0]["value"]["video_type"][0]["value"]["url"] = link
    agency = listing(
        [
            {
                "id": 2911,
                "meta": {"type": "sf.Agency", "html_url": "http://api.sf.gov/health/"},
                "title": "San Francisco Health Commission",
            }
        ]
    )
    source, _ = source_answering(by_type(agency=agency, meeting=body))

    found = await source.find_meetings(
        agency="San Francisco Health Commission", start_date="2026-11-01", end_date="2026-11-30"
    ).run()

    (row,) = found.result.rows
    assert row["video_links"] == []
    assert found.warnings[0].startswith("1 attachment or video entry on these pages")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "html_url",
    ["http://[::1", "http://[api.sf.gov]/x/", "http://api.sf.gov/a/../b/", "http://api.sf.gov//x/"],
)
async def test_a_page_address_that_cannot_be_parsed_is_a_source_error(html_url: str) -> None:
    body = copy.deepcopy(PAGE_SEARCH)
    body["items"][0]["meta"]["html_url"] = html_url
    source, _ = source_answering(by_type(untyped=body))

    with pytest.raises(SourceError) as caught:
        await source.search(text="point in time").run()

    assert not isinstance(caught.value, SourceQueryError)


@pytest.mark.asyncio
async def test_text_that_cannot_be_stored_is_a_source_error() -> None:
    raw = json.dumps(listing(NEWS["items"])).replace(
        json.dumps(NEWS["items"][0]["title"]), '"Mayor \\ud800 signs"'
    )
    source, _ = source_answering(
        lambda _: httpx.Response(200, content=raw, headers={"Content-Type": "application/json"})
    )

    with pytest.raises(SourceError, match="cannot be stored"):
        await source.find_news(start_date="2026-11-01", end_date="2026-11-30").run()


@pytest.mark.asyncio
async def test_meeting_rows_with_nested_links_can_be_written_as_a_snapshot() -> None:
    source, _ = source_answering(by_type(agency=POLICE_AGENCY, meeting=MEETINGS))

    found = await source.find_meetings(
        agency="Police Commission", start_date="2022-01-01", end_date="2026-12-31"
    ).run()

    stored = json.loads(found.result.model_dump_json())
    assert stored["rows"] == found.result.rows
    assert stored["completeness"] == "complete"


@pytest.mark.asyncio
async def test_lookups_run_one_after_another_and_a_cancelled_one_frees_the_queue() -> None:
    started: list[str] = []
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.append(request.url.params.get("search", ""))
        if request.url.params.get("search") == "first":
            await release.wait()
        return httpx.Response(200, json=EMPTY)

    source = SfGov(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_request_interval=0
    )
    first = asyncio.create_task(source.search(text="first").run())
    await asyncio.sleep(0.01)
    second = asyncio.create_task(source.search(text="second").run())
    await asyncio.sleep(0.01)

    # The second lookup waits in the queue and has sent nothing.
    assert started == ["first"]
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    found = await second
    assert started == ["first", "second"]
    assert found.result.returned_row_count == 0


def test_the_source_builds_a_client_that_names_itself_and_follows_no_redirect() -> None:
    source = SfGov()

    client = source._http._client
    assert client.follow_redirects is False
    assert source._http._headers["User-Agent"].startswith("OpenCivicQuery/")
    assert source.host == "api.sf.gov"
    # The module's own pace: requests at least one second apart.
    assert source._min_interval == 1.0


@pytest.mark.asyncio
async def test_closing_the_source_closes_its_own_client() -> None:
    source = SfGov()

    await source.close()

    assert source._http._client.is_closed


def test_search_words_drop_punctuation_and_possessives() -> None:
    assert sfgov.validate_text("  Mayor\u2019s  $1.5 million\n") == "Mayor 1 5 million"
    assert sfgov.validate_text("Pen\u0303a report") == "Pe\u00f1a report"
    assert sfgov.validate_agency(" Film SF |  San Francisco Film Commission ") == (
        "Film SF | San Francisco Film Commission"
    )


@pytest.mark.asyncio
async def test_a_429_is_not_retried_and_pauses_every_lookup_for_a_minute() -> None:
    now = 1000.0
    answers = [httpx.Response(429, headers={"Retry-After": "5"}, text="slow down")]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return answers.pop(0) if answers else httpx.Response(200, json=EMPTY)

    source = SfGov(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        min_request_interval=0,
        clock=lambda: now,
    )

    with pytest.raises(SourceQueryError) as refused:
        await source.find_meetings(
            agency="Police Commission", start_date="2025-01-01", end_date="2025-12-31"
        ).run()
    assert refused.value.code == "SOURCE_RATE_LIMITED"
    assert refused.value.http_status == 429
    assert "Try again in a minute" in refused.value.public_message
    assert len(requests) == 1

    now += 59.0
    # Refused when the lookup is prepared, before it could cost a usage allowance.
    with pytest.raises(SourceQueryError) as paused:
        source.search(text="budget")
    assert paused.value.code == "SOURCE_RATE_LIMITED"
    assert paused.value.http_status is None
    assert "was not sent. Try again in about 1 seconds" in paused.value.public_message
    with pytest.raises(SourceQueryError):
        source.find_news(start_date="2026-10-01", end_date="2026-10-31")
    with pytest.raises(SourceQueryError) as invalid:
        source.search(text="?")
    assert invalid.value.code == "INVALID_QUERY_ARGUMENT"
    assert len(requests) == 1

    now += 2.0
    found = await source.search(text="budget").run()
    assert found.result.returned_row_count == 0
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_a_429_on_a_later_request_of_a_lookup_stops_it_and_starts_the_pause() -> None:
    now = 50.0

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.params["type"] == "sf.Agency":
            return httpx.Response(200, json=POLICE_AGENCY)
        return httpx.Response(429, text="slow down")

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return route(request)

    source = SfGov(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        min_request_interval=0,
        clock=lambda: now,
    )
    prepared = source.find_meetings(
        agency="Police Commission", start_date="2025-01-01", end_date="2025-12-31"
    )
    queued_before_the_pause = source.search(text="budget")

    with pytest.raises(SourceQueryError) as refused:
        await prepared.run()
    assert refused.value.code == "SOURCE_RATE_LIMITED" and refused.value.http_status == 429
    assert [request.url.params["type"] for request in requests] == ["sf.Agency", "sf.Meeting"]

    # A lookup prepared before the pause began is refused when it runs, still unsent.
    with pytest.raises(SourceQueryError) as late:
        await queued_before_the_pause.run()
    assert late.value.code == "SOURCE_RATE_LIMITED" and late.value.http_status is None
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_a_lookup_that_waits_too_long_in_the_queue_is_refused_unsent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sfgov, "QUEUE_WAIT_SECONDS", 0.05)
    started: list[str] = []
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.append(request.url.params["search"])
        if request.url.params["search"] == "first":
            await release.wait()
        return httpx.Response(200, json=EMPTY)

    source = SfGov(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_request_interval=0
    )
    first = asyncio.create_task(source.search(text="first").run())
    await asyncio.sleep(0.01)

    with pytest.raises(SourceQueryError) as refused:
        await source.search(text="second").run()

    assert refused.value.code == "SOURCE_BUSY"
    assert "Nothing was sent to SF.gov" in refused.value.public_message
    assert started == ["first"]
    release.set()
    await first
    # The refused lookup left the queue clean: the next one runs at once.
    await source.search(text="third").run()
    assert started == ["first", "third"]

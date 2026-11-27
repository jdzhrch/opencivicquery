from __future__ import annotations

import copy as copying
import gzip
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from opencivicquery import cli
from opencivicquery.models import Completeness
from opencivicquery.sources import sfgov, sfgov_copy
from opencivicquery.sources.base import SourceError, SourceQueryError
from opencivicquery.sources.sfgov import SFGOV_HOST, SfGov
from opencivicquery.sources.sfgov_copy import (
    COPY_OBJECT,
    EXTENSION_URL,
    CopyReader,
    LocalCopyStore,
    SfGovCopy,
    decode_copy,
    encode_copy,
    html_to_text,
    meetings_from_copy,
    news_from_copy,
    search_from_copy,
    sync_copy,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sfgov"


def fixture(name: str) -> dict[str, Any]:
    """A response recorded from the live interface in November 2026, long text trimmed."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


NEWS = fixture("news_mayor")
NEWS_PAGE = fixture("news_page_110692")
MEETINGS = fixture("meetings_police_commission_agenda")
MAYOR = fixture("agency_search_mayor_office")
POLICE = fixture("agency_search_police_commission")
AGENCIES = ["Police Commission", "Office of the Mayor"]
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)

Route = Callable[[httpx.Request], httpx.Response]


def listing(items: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {"meta": {"total_count": len(items) if total is None else total}, "items": items}


def site(
    *,
    news: list[dict[str, Any]] | None = None,
    news_total: int | None = None,
    meetings: list[dict[str, Any]] | None = None,
    override: Route | None = None,
) -> tuple[Route, list[httpx.Request]]:
    """A recorded SF.gov that answers agency, news, news page, and meeting requests."""
    requests: list[httpx.Request] = []
    news_items = sorted(
        news if news is not None else NEWS["items"],
        key=lambda item: item["meta"]["id"],
        reverse=True,
    )
    meeting_items = meetings if meetings is not None else MEETINGS["items"]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if override is not None and (answer := override(request)) is not None:
            return answer
        path = request.url.path
        if path != "/api/v2/pages/":
            page_id = int(path.rstrip("/").rsplit("/", 1)[1])
            return httpx.Response(200, json=NEWS_PAGE | {"id": page_id})
        kind = request.url.params.get("type")
        if kind == "sf.Agency":
            words = request.url.params["search"].casefold()
            return httpx.Response(200, json=POLICE if "police" in words else MAYOR)
        # News belongs to the Office of the Mayor (559), meeting pages to the Police
        # Commission (3005), as on the live site.
        agency = request.url.params.get("primary_agency")
        if kind == "sf.News":
            items = news_items if agency == "559" else []
            return httpx.Response(200, json=listing(items, news_total if items else None))
        if kind == "sf.Meeting":
            return httpx.Response(200, json=listing(meeting_items if agency == "3005" else []))
        return httpx.Response(404, json={"message": "No Page matches the given query."})

    return handler, requests


def source_for(handler: Route, **kwargs: Any) -> SfGov:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SfGov(client=client, min_request_interval=0, **kwargs)


async def first_copy() -> SfGovCopy:
    handler, _ = site()
    made, _ = await sync_copy(source_for(handler), AGENCIES, None, now=lambda: NOW)
    assert made is not None
    return made


def test_html_becomes_one_line_per_block() -> None:
    html = (
        '<p data-block-key="a"><b>SAN FRANCISCO</b> &ndash; Mayor&nbsp;signs</p>'
        "<ul><li>One</li><li>Two<br>lines</li></ul><p></p>"
    )

    assert html_to_text(html) == "SAN FRANCISCO – Mayor signs\nOne\nTwo\nlines"
    assert html_to_text("<p> </p>") is None
    assert html_to_text(None) is None


def test_a_copy_survives_storage_and_a_damaged_one_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = SfGovCopy(
        copied_at=NOW,
        agencies=[
            sfgov_copy.CopyAgency(
                page_id=559,
                title="Office of the Mayor",
                page_url="https://www.sf.gov/departments--office-mayor",
                copied_at=NOW,
                news_total=0,
                meetings_total=0,
                news_complete=True,
                meetings_complete=True,
            )
        ],
        news=[],
        meetings=[],
        documents=[],
    )
    empty = made.model_dump(mode="json") | {"agencies": []}

    assert decode_copy(encode_copy(made)) == made
    for damaged in (
        b"not gzip",
        gzip.compress(b"{}"),
        gzip.compress(b'{"format": 2, "copied_at": "2026-11-27T00:00:00Z"}'),
        gzip.compress(json.dumps(empty).encode()),
    ):
        with pytest.raises(SourceError):
            decode_copy(damaged)
    monkeypatch.setattr(sfgov_copy, "MAX_COPY_BYTES", 10)
    with pytest.raises(SourceError, match="larger"):
        decode_copy(encode_copy(made))


@pytest.mark.asyncio
async def test_the_local_store_replaces_the_whole_file(tmp_path: Path) -> None:
    store = LocalCopyStore(tmp_path)

    assert await store.read() is None
    location = await store.write(b"first")
    await store.write(b"second")

    assert location == (tmp_path.resolve() / COPY_OBJECT).as_uri()
    assert await store.read() == b"second"
    assert [path.name for path in (tmp_path / "sfgov-copy").iterdir()] == ["current.json.gz"]


@pytest.mark.asyncio
async def test_the_first_read_takes_text_agendas_and_attached_files() -> None:
    handler, requests = site()
    source = source_for(handler)

    made, outcomes = await sync_copy(source, AGENCIES, None, now=lambda: NOW)

    assert made is not None
    assert {request.url.host for request in requests} == {SFGOV_HOST}
    assert all(request.headers["User-Agent"].startswith("OpenCivicQuery/") for request in requests)
    news_pages = [request.url.path for request in requests if request.url.path != "/api/v2/pages/"]
    assert news_pages == [f"/api/v2/pages/{page}/" for page in (110692, 110647, 110575)]
    assert source.requests_sent == len(requests) == 2 + 2 + 2 + 3  # agencies, news, meetings
    meeting_request = next(r for r in requests if r.url.params.get("type") == "sf.Meeting")
    assert meeting_request.url.params["fields"].endswith(",agenda,overview")
    assert [outcome.status for outcome in outcomes] == ["read", "read"]
    assert [agency.title for agency in made.agencies] == AGENCIES
    assert all(agency.copied_at == NOW for agency in made.agencies)
    assert all(agency.news_complete and agency.meetings_complete for agency in made.agencies)
    news = {item.page_id: item for item in made.news}
    assert news[110692].text is not None and news[110692].text.startswith("SAN FRANCISCO")
    assert news[110692].page_url.startswith("https://www.sf.gov/")
    meetings = {item.page_id: item for item in made.meetings}
    assert meetings[110632].text.startswith("General Public Comment")
    assert "Adoption of Minutes" in meetings[110632].text
    assert meetings[103689].cancelled is True
    assert meetings[110632].spans[0].start == date(2026, 12, 2)
    documents = {item.document_id: item for item in made.documents}
    assert set(documents) == {71015, 71016, 71018, 71019, 64995}
    assert documents[71018].page_ids == [110632]
    assert documents[71018].file_url.startswith("https://media.api.sf.gov/documents/")


@pytest.mark.asyncio
async def test_a_later_read_reuses_unchanged_text_and_rereads_a_changed_page() -> None:
    previous = await first_copy()
    changed = copying.deepcopy(NEWS["items"])
    changed[1]["abstract"] = "A corrected summary"
    handler, requests = site(news=changed)

    made, outcomes = await sync_copy(source_for(handler), AGENCIES, previous, now=lambda: LATER)

    assert made is not None
    news_pages = [r.url.path for r in requests if r.url.path != "/api/v2/pages/"]
    assert news_pages == [f"/api/v2/pages/{changed[1]['meta']['id']}/"]
    assert [outcome.text_requests for outcome in outcomes] == [0, 1]
    assert all(agency.copied_at == LATER for agency in made.agencies)


@pytest.mark.asyncio
async def test_a_failed_agency_keeps_its_previous_pages_and_date() -> None:
    previous = await first_copy()

    def police_down(request: httpx.Request) -> httpx.Response | None:
        if request.url.params.get("primary_agency") == "3005":
            return httpx.Response(503, text="unavailable")
        return None

    handler, _ = site(override=police_down)
    made, outcomes = await sync_copy(source_for(handler), AGENCIES, previous, now=lambda: LATER)

    assert made is not None
    police = next(agency for agency in made.agencies if agency.title == "Police Commission")
    mayor = next(agency for agency in made.agencies if agency.title == "Office of the Mayor")
    assert police.copied_at == NOW and mayor.copied_at == LATER
    assert {item.page_id for item in made.meetings} == {item.page_id for item in previous.meetings}
    assert {item.document_id for item in made.documents} == {
        item.document_id for item in previous.documents
    }
    assert outcomes[0].status == "kept the copy of 2026-11-27"
    assert "HTTP 503" in outcomes[0].detail


@pytest.mark.asyncio
async def test_a_slow_down_answer_stops_the_run_and_sends_nothing_more() -> None:
    previous = await first_copy()

    def slow_down(request: httpx.Request) -> httpx.Response | None:
        if request.url.params.get("type") == "sf.News":
            return httpx.Response(429, text="slow down")
        return None

    handler, requests = site(override=slow_down)
    made, outcomes = await sync_copy(source_for(handler), AGENCIES, previous, now=lambda: LATER)

    assert made is not None
    assert [agency.copied_at for agency in made.agencies] == [NOW, NOW]
    assert len(requests) == 2  # the agency lookup and the slowed news list
    assert "not read: the run stopped earlier" in outcomes[1].detail


@pytest.mark.asyncio
async def test_a_run_with_nothing_to_keep_writes_nothing() -> None:
    handler, _ = site(override=lambda _: httpx.Response(503, text="down"))

    made, outcomes = await sync_copy(source_for(handler), AGENCIES, None, now=lambda: NOW)

    assert made is None
    assert [outcome.status for outcome in outcomes] == ["failed, not in the copy"] * 2


@pytest.mark.asyncio
async def test_a_sample_read_is_marked_partial() -> None:
    handler, requests = site(news_total=277)

    made, _ = await sync_copy(source_for(handler), AGENCIES, None, sample=2, now=lambda: NOW)

    assert made is not None
    mayor = next(agency for agency in made.agencies if agency.title == "Office of the Mayor")
    assert mayor.news_total == 277 and not mayor.news_complete
    news_list = next(r for r in requests if r.url.params.get("type") == "sf.News")
    assert news_list.url.params["limit"] == "2"


@pytest.mark.asyncio
async def test_news_from_the_copy_names_its_date_scope_and_leaves_nothing_complete() -> None:
    made = await first_copy()

    found = news_from_copy(
        made, name="office of the mayor", first=date(2026, 11, 24), last=date(2026, 11, 26)
    )

    result = found.result
    assert [row["page_id"] for row in result.rows] == [110692]
    assert list(result.rows[0]) == list(sfgov.NEWS_FIELDS)
    assert result.completeness == Completeness.LIMITED
    assert result.query_url is None
    assert result.source_updated_at == NOW.isoformat()
    assert result.dataset_id == "sfgov-copy/news"
    assert found.warnings[0].startswith("FROM THE DAILY COPY.")
    assert "taken on 2026-11-27" in found.warnings[0]
    assert "Police Commission; Office of the Mayor" in found.warnings[0]
    assert found.rules[0].startswith("FROM A DAILY COPY, NOT LIVE.")
    assert found.rules[-1] == (
        'End your reply with this sentence, word for word: "These SF.gov results come from a '
        "copy taken on 2026-11-27; for live results that cover every SF.gov agency, use the "
        f'OpenCivicQuery SF.gov extension for Claude Desktop: {EXTENSION_URL}"'
    )
    assert "when the copy was taken, not an update time SF.gov reported" in found.rules[0]
    assert result.query.startswith(f"daily copy of SF.gov taken {NOW.isoformat()};")
    assert found.escalations == ["SFGOV_DAILY_COPY"]


@pytest.mark.asyncio
async def test_an_agency_outside_the_copy_is_refused_with_the_covered_names() -> None:
    made = await first_copy()

    with pytest.raises(SourceQueryError) as caught:
        news_from_copy(
            made, name="Fire Commission", first=date(2026, 1, 1), last=date(2026, 12, 31)
        )

    assert caught.value.code == "NOT_FOUND" and caught.value.argument == "agency"
    assert "covers only these agencies: Police Commission; Office of the Mayor" in (
        caught.value.public_message
    )
    assert EXTENSION_URL in caught.value.public_message


@pytest.mark.asyncio
async def test_partial_agencies_and_dates_reach_the_answers() -> None:
    handler, _ = site(news_total=277)
    made, _ = await sync_copy(source_for(handler), AGENCIES, None, sample=3, now=lambda: NOW)
    assert made is not None
    made.agencies[0].copied_at = LATER

    news = news_from_copy(made, name=None, first=date(2026, 1, 1), last=date(2026, 12, 31))
    meetings = meetings_from_copy(
        made, name="Police Commission", first=date(2026, 11, 1), last=date(2026, 11, 20)
    )

    assert news.result.completeness == Completeness.TRUNCATED
    assert "between 2026-11-27 and 2026-11-28" in news.warnings[0]
    assert news.warnings[1].startswith("PARTIAL: the copy holds 3 of the 277 news pages")
    assert news.result.source_updated_at == NOW.isoformat()
    assert [row["meeting_date"] for row in meetings.result.rows] == ["2026-11-04"]
    assert meetings.result.completeness == Completeness.LIMITED
    assert meetings.result.source_updated_at == LATER.isoformat()
    # Each answer dates itself by the agencies it draws on.
    assert meetings.result.query.startswith(f"daily copy of SF.gov taken {LATER.isoformat()};")
    assert news.result.query.startswith(f"daily copy of SF.gov taken {NOW.isoformat()};")


@pytest.mark.asyncio
async def test_searching_the_copy_matches_whole_words() -> None:
    made = await first_copy()

    pages = search_from_copy(made, words="Police Commission meeting", kind="pages")
    # checked 2026-11-27: staff titled the minutes files "Oct 7 Mins" and "Oct 14 Mins".
    files = search_from_copy(made, words="document protocol", kind="documents")
    short = search_from_copy(made, words="minutes", kind="documents")
    nothing = search_from_copy(made, words="zoning variance", kind="pages")

    assert [row["page_id"] for row in pages.result.rows] == [110632, 103689]
    assert all(row["first_published_at"] is None for row in pages.result.rows)
    assert pages.warnings[1] == sfgov_copy.COPY_SEARCH_PAGES_WARNING
    assert {row["document_id"] for row in files.result.rows} == {71015, 71016}
    assert short.result.rows == []
    assert nothing.result.rows == [] and nothing.warnings[1] == sfgov_copy.NO_COPY_MATCH_WARNING


@pytest.mark.asyncio
async def test_a_copy_search_beyond_the_row_limit_is_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = await first_copy()
    monkeypatch.setattr(sfgov_copy, "SEARCH_LIMIT", 1)

    found = search_from_copy(made, words="Police Commission meeting", kind="pages")

    assert found.result.completeness == Completeness.TRUNCATED
    assert found.warnings[1].startswith("PARTIAL: 2 pages in the copy match")


async def copy_backed_source(
    tmp_path: Path, answer: Callable[[], httpx.Response], **kwargs: Any
) -> tuple[SfGov, list[httpx.Request]]:
    store = LocalCopyStore(tmp_path)
    await store.write(encode_copy(await first_copy()))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return answer()

    return source_for(handler, copy=CopyReader(store), **kwargs), requests


@pytest.mark.asyncio
async def test_a_site_without_data_turns_to_the_copy_and_is_left_alone_for_an_hour(
    tmp_path: Path,
) -> None:
    now = 100.0
    source, requests = await copy_backed_source(
        tmp_path, lambda: httpx.Response(202, text=""), clock=lambda: now
    )

    first = await source.find_news(
        agency="Office of the Mayor", start_date="2026-11-01", end_date="2026-11-30"
    ).run()
    second = await source.find_meetings(
        agency="Police Commission", start_date="2026-11-01", end_date="2026-12-31"
    ).run()
    third = await source.search(text="minutes", kind="documents").run()

    assert len(requests) == 1
    for found in (first, second, third):
        assert found.warnings[0].startswith("FROM THE DAILY COPY.")
        assert found.escalations == ["SFGOV_DAILY_COPY"]
    now += sfgov.LIVE_RETRY_SECONDS + 1
    await source.search(text="minutes").run()
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        lambda: httpx.Response(503, text="down"),
        lambda: httpx.Response(403, text="<html>Forbidden</html>"),
        lambda: httpx.Response(429, text="slow down"),
    ],
)
async def test_an_unreachable_site_turns_to_the_copy(
    tmp_path: Path, answer: Callable[[], httpx.Response]
) -> None:
    source, requests = await copy_backed_source(tmp_path, answer)

    found = await source.search(text="Police Commission meeting").run()

    assert found.result.dataset_id == "sfgov-copy/pages"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_a_network_failure_turns_to_the_copy(tmp_path: Path) -> None:
    store = LocalCopyStore(tmp_path)
    await store.write(encode_copy(await first_copy()))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    found = await source_for(refuse, copy=CopyReader(store)).search(text="Police meeting").run()

    assert found.result.dataset_id == "sfgov-copy/pages"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        lambda: httpx.Response(400, json={"message": "type doesn't exist"}),
        lambda: httpx.Response(200, json={"unexpected": True}),
    ],
)
async def test_a_bad_request_or_answer_never_hides_behind_the_copy(
    tmp_path: Path, answer: Callable[[], httpx.Response]
) -> None:
    source, _ = await copy_backed_source(tmp_path, answer)

    with pytest.raises(SourceError) as caught:
        await source.search(text="Police Commission meeting").run()

    assert "copy" not in str(caught.value).casefold()


@pytest.mark.asyncio
async def test_without_a_copy_the_live_error_stands(tmp_path: Path) -> None:
    source = source_for(
        lambda _: httpx.Response(202, text=""), copy=CopyReader(LocalCopyStore(tmp_path))
    )

    with pytest.raises(SourceQueryError) as caught:
        await source.search(text="budget").run()

    assert caught.value.http_status == 202
    # With nothing to answer from, the next lookup tries the site again.
    with pytest.raises(SourceQueryError) as later:
        await source.search(text="budget").run()
    assert later.value.http_status == 202


@pytest.mark.asyncio
async def test_the_reader_keeps_a_copy_for_ten_minutes_and_survives_a_bad_read(
    tmp_path: Path,
) -> None:
    now = 0.0
    store = LocalCopyStore(tmp_path)
    await store.write(encode_copy(await first_copy()))
    reader = CopyReader(store, clock=lambda: now)

    assert not reader.has_copy
    kept = await reader.load()
    assert kept is not None and reader.has_copy
    await store.write(b"damaged")
    assert await reader.load() is kept
    now += sfgov_copy.COPY_CACHE_SECONDS + 1
    assert await reader.load() is kept  # the damaged file is logged and the last copy stays


@pytest.mark.asyncio
async def test_the_pause_refuses_early_only_without_a_copy(tmp_path: Path) -> None:
    now = 10.0
    source, _ = await copy_backed_source(
        tmp_path, lambda: httpx.Response(429, text="slow down"), clock=lambda: now
    )
    await source.search(text="Police Commission meeting").run()

    # Holding a copy, a lookup in the pause is prepared and answered from the copy.
    found = await source.search(text="Police Commission meeting").run()
    assert found.result.dataset_id == "sfgov-copy/pages"
    live_only = source_for(lambda _: httpx.Response(429, text="slow down"), clock=lambda: now)
    with pytest.raises(SourceQueryError):
        await live_only.search(text="budget").run()
    with pytest.raises(SourceQueryError) as refused:
        live_only.search(text="budget")
    assert refused.value.code == "SOURCE_RATE_LIMITED"


def test_the_command_refuses_unsafe_combinations() -> None:
    parser = cli.build_parser()
    for argv in (
        ["sfgov-copy"],
        ["sfgov-copy", "--path", "a", "--bucket", "b"],
        ["sfgov-copy", "--bucket", "b", "--sample", "3"],
        ["sfgov-copy", "--path", "a", "--sample", "0"],
    ):
        with pytest.raises(SystemExit):
            cli._sfgov_copy(parser.parse_args(argv))


def test_the_command_writes_a_copy_and_plans_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    handler, _ = site()
    monkeypatch.setattr(sfgov, "SfGov", lambda: source_for(handler))
    args = ["sfgov-copy", "--path", str(tmp_path), "--agency", "Police Commission"]

    assert cli._sfgov_copy(cli.build_parser().parse_args(args)) == 0
    written = decode_copy((tmp_path / COPY_OBJECT).read_bytes())
    assert [agency.title for agency in written.agencies] == ["Police Commission"]
    assert "Wrote the copy to" in capsys.readouterr().out

    (tmp_path / COPY_OBJECT).unlink()
    assert cli._sfgov_copy(cli.build_parser().parse_args([*args, "--plan"])) == 0
    out = capsys.readouterr().out
    assert '"meeting_pages": 3' in out and "requests in all" in out
    assert not (tmp_path / COPY_OBJECT).exists()


@pytest.mark.asyncio
async def test_a_range_reaching_the_copy_date_is_partial() -> None:
    made = await first_copy()

    news = news_from_copy(made, name=None, first=date(2026, 11, 1), last=date(2026, 11, 27))
    meetings = meetings_from_copy(
        made, name="Police Commission", first=date(2027, 1, 1), last=date(2027, 1, 31)
    )

    assert news.result.completeness == Completeness.TRUNCATED
    assert any(
        warning.startswith("PARTIAL: the copy was taken on 2026-11-27") for warning in news.warnings
    )
    assert meetings.result.completeness == Completeness.TRUNCATED
    assert meetings.result.rows == []
    assert sfgov_copy.NO_COPY_MEETINGS_WARNING in meetings.warnings
    assert sfgov.NO_MEETINGS_WARNING not in meetings.warnings


@pytest.mark.asyncio
async def test_a_copy_search_names_agencies_it_holds_only_in_part() -> None:
    handler, _ = site(news_total=277)
    made, _ = await sync_copy(source_for(handler), AGENCIES, None, sample=3, now=lambda: NOW)
    assert made is not None

    pages = search_from_copy(made, words="Police Commission meeting", kind="pages")
    files = search_from_copy(made, words="document protocol", kind="documents")

    assert pages.result.completeness == Completeness.TRUNCATED
    assert any(
        "does not hold every page SF.gov listed for Office of the Mayor" in w
        for w in pages.warnings
    )
    assert files.result.completeness == Completeness.LIMITED


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("news", 0, "page_url"), "https://evil.example/page"),
        (
            ("meetings", 0, "documents"),
            [{"group": None, "title": "x", "url": "https://evil.example/a.pdf"}],
        ),
        (("meetings", 0, "video_links"), [{"title": None, "url": "http://127.0.0.1/admin"}]),
        (("documents", 0, "file_url"), "https://media.api.sf.gov/a b.pdf"),
        (("agencies", 0, "page_url"), "javascript:alert(1)"),
    ],
)
@pytest.mark.asyncio
async def test_a_copy_changed_in_storage_cannot_carry_foreign_links(
    path: tuple[str, int, str], value: Any
) -> None:
    stored = (await first_copy()).model_dump(mode="json")
    section, index, key = path
    stored[section][index][key] = value

    with pytest.raises(SourceError, match="not in the expected form"):
        decode_copy(gzip.compress(json.dumps(stored).encode()))


def test_an_oversized_stored_copy_is_refused_before_unpacking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sfgov_copy, "MAX_STORED_BYTES", 10)

    with pytest.raises(SourceError, match="larger"):
        decode_copy(b"x" * 11)


@pytest.mark.asyncio
async def test_one_unreadable_news_page_keeps_the_agency_but_five_in_a_row_do_not() -> None:
    def one_page_down(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/api/v2/pages/110647/":
            return httpx.Response(500, text="error")
        return None

    handler, _ = site(override=one_page_down)
    made, outcomes = await sync_copy(source_for(handler), AGENCIES, None, now=lambda: NOW)

    assert made is not None
    mayor = next(agency for agency in made.agencies if agency.title == "Office of the Mayor")
    assert mayor.news_complete is True
    assert {item.page_id: item.text is None for item in made.news}[110647] is True
    assert outcomes[1].text_failures == 1 and outcomes[1].status == "read"

    many = [
        copying.deepcopy(NEWS["items"][0])
        | {"meta": {"id": 200000 + n, "html_url": "http://api.sf.gov/n/"}}
        for n in range(6)
    ]
    handler, requests = site(
        news=many,
        override=lambda r: httpx.Response(500) if r.url.path != "/api/v2/pages/" else None,
    )
    _, outcomes = await sync_copy(
        source_for(handler), ["Office of the Mayor"], None, now=lambda: NOW
    )
    assert outcomes[0].status == "failed, not in the copy"
    assert (
        sum(1 for r in requests if r.url.path != "/api/v2/pages/")
        == sfgov_copy.MAX_TEXT_FAILURES_IN_A_ROW
    )


@pytest.mark.asyncio
async def test_repeated_agency_names_are_read_once() -> None:
    handler, requests = site()

    made, outcomes = await sync_copy(
        source_for(handler), ["Office of the Mayor", "office of the  mayor"], None, now=lambda: NOW
    )

    assert made is not None
    assert [agency.title for agency in made.agencies] == ["Office of the Mayor"]
    assert len(outcomes) == 1
    assert len({item.page_id for item in made.news}) == len(made.news)


@pytest.mark.asyncio
async def test_a_page_that_is_not_json_turns_to_the_copy(tmp_path: Path) -> None:
    source, requests = await copy_backed_source(
        tmp_path, lambda: httpx.Response(200, text="<html><body>Please wait</body></html>")
    )

    first = await source.search(text="Police Commission meeting").run()
    second = await source.search(text="Police Commission meeting").run()

    assert first.result.dataset_id == second.result.dataset_id == "sfgov-copy/pages"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_without_a_copy_a_refused_request_does_not_lock_out_the_site(
    tmp_path: Path,
) -> None:
    answers = [
        httpx.Response(403, text="Forbidden"),
        httpx.Response(200, json={"meta": {"total_count": 0}, "items": []}),
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return answers.pop(0)

    source = source_for(handler, copy=CopyReader(LocalCopyStore(tmp_path)))
    with pytest.raises(SourceQueryError):
        await source.search(text="budget").run()
    found = await source.search(text="budget").run()

    assert found.result.dataset_id == "pages"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_the_reader_takes_a_new_copy_after_ten_minutes(tmp_path: Path) -> None:
    now = 0.0
    store = LocalCopyStore(tmp_path)
    first = await first_copy()
    await store.write(encode_copy(first))
    reader = CopyReader(store, clock=lambda: now)
    assert (await reader.load()) == first

    newer = first.model_copy(update={"copied_at": LATER})
    await store.write(encode_copy(newer))
    assert (await reader.load()) == first
    now += sfgov_copy.COPY_CACHE_SECONDS + 1
    assert (await reader.load()) == newer


class _Blob:
    def __init__(self, data: bytes | None) -> None:
        self.data = data
        self.size = None if data is None else len(data)

    def download_as_bytes(self) -> bytes:
        assert self.data is not None
        return self.data

    def upload_from_string(self, data: bytes, content_type: str) -> None:
        assert content_type == "application/gzip"
        self.data, self.size = data, len(data)


class _Bucket:
    def __init__(self, blob: _Blob) -> None:
        self._blob = blob
        self.names: list[str] = []

    def blob(self, name: str) -> _Blob:
        self.names.append(name)
        return self._blob

    def get_blob(self, name: str) -> _Blob | None:
        self.names.append(name)
        return None if self._blob.data is None else self._blob


class _Client:
    def __init__(self, bucket: _Bucket) -> None:
        self._bucket = bucket

    def bucket(self, name: str) -> _Bucket:
        assert name == "snapshots"
        return self._bucket


@pytest.mark.asyncio
async def test_the_bucket_store_reads_writes_and_refuses_an_oversized_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob = _Blob(None)
    bucket = _Bucket(blob)
    store = sfgov_copy.GcsCopyStore("snapshots", client=_Client(bucket))

    assert await store.read() is None
    assert await store.write(b"copy") == "gs://snapshots/sfgov-copy/current.json.gz"
    assert await store.read() == b"copy"
    assert set(bucket.names) == {COPY_OBJECT}
    monkeypatch.setattr(sfgov_copy, "MAX_STORED_BYTES", 2)
    with pytest.raises(SourceError, match="larger"):
        await store.read()
    with pytest.raises(SourceError):
        sfgov_copy.GcsCopyStore("a/b", client=_Client(bucket))


def test_the_plan_counts_list_requests_with_their_overlap() -> None:
    count = sfgov_copy._list_requests
    assert [count(n, 500) for n in (0, 1, 500, 501, 999, 1000)] == [1, 1, 1, 2, 2, 3]
    assert [count(n, 50) for n in (50, 51, 99, 100)] == [1, 2, 2, 3]


def test_the_command_reads_everything_again_after_a_damaged_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "sfgov-copy").mkdir()
    (tmp_path / COPY_OBJECT).write_bytes(b"damaged")
    handler, _ = site()
    monkeypatch.setattr(sfgov, "SfGov", lambda: source_for(handler))
    args = ["sfgov-copy", "--path", str(tmp_path), "--agency", "Police Commission"]

    assert cli._sfgov_copy(cli.build_parser().parse_args(args)) == 0

    assert "could not be read" in capsys.readouterr().out
    assert (
        decode_copy((tmp_path / COPY_OBJECT).read_bytes()).agencies[0].title == "Police Commission"
    )

"""Reading a Legistar client's public pages when its Web API copy is stale or cannot answer.

The pages under tests/fixtures/legistar were recorded from sfgov.legistar.com on 2026-11-26
and trimmed; fixtures/legistar/README.md says how.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable
from datetime import date
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from opencivicquery import __version__
from opencivicquery.models import Completeness
from opencivicquery.sources import legistar as legistar_module
from opencivicquery.sources import legistar_pages as pages
from opencivicquery.sources.base import SourceError, SourceQueryError
from opencivicquery.sources.legistar import (
    DETAIL_RULE,
    HISTORY_GAP_RULE,
    LEGISTAR_HOST,
    NO_DATE_WARNING,
    NO_VOTES_RULE,
    PARTIAL_PAGE_WARNING,
    SETTING_REASON,
    SITE_UNREAD_WARNING,
    WEB_ITEM_COUNT_RULE,
    WEB_ITEM_FIELDS,
    WEB_ITEM_GRAIN,
    WEB_ITEMS_WARNING,
    WEB_MATTER_FIELDS,
    WEB_SPONSOR_RULE,
    WEB_TITLE_CUT_WARNING,
    Legistar,
    detail_warning,
    history_gap_warning,
    items_view_warning,
    search_dates_warning,
    search_gap_warning,
    stale_reason,
    stale_warning,
    web_rule,
    web_warning,
)

FIXTURES = Path(__file__).parent / "fixtures" / "legistar"
SITE = "sfgov.legistar.com"
SETTING_ERROR = (
    "'Agenda Draft Status' or 'Agenda Status Not Vievable By The Public' is not setup in "
    "settings. Value should be greater than 0."
)
STALE = date(2020, 9, 17)
TODAY = date(2026, 11, 26)

# The Rules Committee meeting of 2026-09-14 and the Board meeting of 2026-09-29.
MEETING_ID = 1439610
MEETING_GUID = "9A52EBCA-0730-4056-9FDE-A7AF7B764B17"
BOARD_ID = 1449004
BOARD_GUID = "82BD7CE6-C773-4BF3-A2FA-3A59A473DBB1"
ITEM_ID = 37487161
ITEM_GUID = "EAAFCF66-D5C3-4200-AE9A-9039D498B203"
MATTER_ID = 8156781
MATTER_GUID = "B29F0A7C-3940-4F09-B553-CE4550ECDC0F"
HOUSING = pages.Criteria(title="housing", first=date(2026, 10, 1), last=date(2026, 11, 26))
HOUSING_SEARCH = {
    "client": "sfgov",
    "text": "housing",
    "introduced_from": "2026-10-01",
    "introduced_to": "2026-11-26",
}


def page(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def echoing(html: str, *, title: str = "", file: str = "", first: str = "", last: str = "") -> str:
    """A recorded results page as it would print other criteria back into its form."""
    text_input = r'(<input name="ctl00\$ContentPlaceHolder1\${}" type="text")(?: value="[^"]*")?'
    date_input = r'(<input id="ctl00_ContentPlaceHolder1_{}" [^>]*?) value="[^"]*"'
    patterns = {
        text_input.format("txtTit"): title,
        text_input.format("txtFil"): file,
        date_input.format("txtFileCreated1"): first,
        date_input.format("txtFileCreated2"): last,
    }
    for pattern, value in patterns.items():
        html, count = re.subn(
            pattern, lambda match, value=value: f'{match.group(1)} value="{escape(value)}"', html
        )
        assert count == 1
    chosen = "between" if first and last else "="
    html = html.replace(' checked="checked"', "")
    html, count = re.subn(
        rf'(name="ctl00\$ContentPlaceHolder1\$radFileCreated" value="{chosen}")',
        r'\1 checked="checked"',
        html,
    )
    assert count == 1
    return html


def matter_page(file: str) -> str:
    """The recorded matter page as the page of another file."""
    html = page("legislation_detail")
    assert html.count("<font>260875</font>") == 1
    return html.replace("<font>260875</font>", f"<font>{file}</font>")


class Site:
    """sfgov's website as recorded, beside a Web API that is stale, broken, or current."""

    def __init__(self, api: str = "stale") -> None:
        self.api = api
        self.requests: list[httpx.Request] = []
        self.searches: dict[tuple[str, str], str] = {
            ("title", "housing"): page("legislation_results"),
            ("file", "260875"): page("legislation_results_file"),
        }
        self.matters: dict[int, str] = {
            MATTER_ID: page("legislation_detail"),
            8329872: matter_page("261287"),
            8326663: matter_page("261267"),
            8326658: matter_page("261262"),
            8326657: matter_page("261261"),
        }
        self.answers: dict[str, httpx.Response | Callable[[httpx.Request], Any]] = {}
        self.active = 0
        self.busiest = 0
        self.delay = 0.0

    def legistar(self) -> Legistar:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return Legistar(client=client, today=lambda: TODAY)

    @property
    def site_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.host == SITE]

    def paths(self) -> list[str]:
        return [f"{request.method} {request.url.path}" for request in self.site_requests]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == LEGISTAR_HOST:
            return self.api_answer(request)
        assert request.url.host == SITE
        self.active += 1
        self.busiest = max(self.busiest, self.active)
        try:
            await asyncio.sleep(self.delay)
            answer = self.answers.get(request.url.path.removeprefix("/"), self.site_answer)
            if callable(answer):
                answer = answer(request)
            return await answer if inspect.isawaitable(answer) else answer
        finally:
            self.active -= 1

    def api_answer(self, request: httpx.Request) -> httpx.Response:
        setting = httpx.Response(400, text=SETTING_ERROR, headers={"Content-Type": "text/plain"})
        if self.api == "setting":
            return setting
        if request.url.path.endswith("/matters"):
            if request.url.params.get("$select") == "MatterIntroDate":
                newest = TODAY if self.api == "current" else STALE
                return httpx.Response(200, json=[{"MatterIntroDate": f"{newest}T00:00:00"}])
            return httpx.Response(200, json=[{"MatterId": 1, "MatterFile": "200001"}])
        # sfgov's events resource names a setting error; a stale copy has no such meeting.
        if self.api == "stale":
            return setting
        return httpx.Response(404, json={"Message": "Event not found"})

    def site_answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/")
        if path == pages.SEARCH_PATH and request.method == "GET":
            return httpx.Response(
                200,
                text=page("legislation_form"),
                headers=[
                    ("Set-Cookie", "ASP.NET_SessionId=abc123; path=/; secure; HttpOnly"),
                    ("Set-Cookie", "Setting-60-Legislation Year=This Year; path=/; secure"),
                ],
            )
        if path == pages.SEARCH_PATH:
            form = form_of(request)
            if "ctl00$ContentPlaceHolder1$btnSwitch" in form:
                return httpx.Response(
                    200,
                    text=page("legislation_advanced"),
                    headers=[("Set-Cookie", "Setting-60-Legislation Type=; path=/; secure")],
                )
            title = form["ctl00$ContentPlaceHolder1$txtTit"]
            file = form["ctl00$ContentPlaceHolder1$txtFil"]
            found = self.searches.get(("file", file) if file else ("title", title))
            none = echoing(
                page("legislation_results_none"),
                title=title,
                file=file,
                first=form.get("ctl00$ContentPlaceHolder1$txtFileCreated1", ""),
                last=form.get("ctl00$ContentPlaceHolder1$txtFileCreated2", ""),
            )
            return httpx.Response(
                200,
                text=found or none,
                # The site remembers a search in cookies; the next search must not send them.
                headers=[("Set-Cookie", f"Search-Title={title or file}; path=/; secure")],
            )
        record = int(request.url.params["ID"])
        if path == pages.MATTER_PATH:
            return httpx.Response(200, text=self.matters[record])
        if path == pages.MEETING_PATH:
            return httpx.Response(200, text=page("meeting_detail"))
        assert path == pages.ACTION_PATH
        return httpx.Response(200, text=page("history_detail"))


def form_of(request: httpx.Request) -> dict[str, str]:
    return {
        name: values[0]
        for name, values in parse_qs(request.content.decode(), keep_blank_values=True).items()
    }


# --- Parsing the recorded pages ------------------------------------------------------


def test_the_search_form_says_which_mode_it_is_in_and_how_to_switch() -> None:
    simple = pages.parse_search_form(page("legislation_form"))
    advanced = pages.parse_search_form(page("legislation_advanced"))

    assert simple.advanced is False
    assert advanced.advanced is True
    fields = pages.switch_fields(simple)
    assert fields["ctl00$ContentPlaceHolder1$btnSwitch"] == "Advanced search >>>"
    assert "__VIEWSTATE" in fields
    # Only the pressed button is posted, as a browser would.
    assert not any("helpButton" in name or "btnClear" in name for name in fields)
    with pytest.raises(SourceError):
        pages.search_fields(simple, HOUSING)
    with pytest.raises(SourceError):
        pages.parse_search_form("<html><body>Maintenance</body></html>")


def test_a_search_posts_the_form_state_and_only_the_asked_criteria() -> None:
    form = pages.parse_search_form(page("legislation_advanced"))

    fields = pages.search_fields(form, HOUSING)
    undated = pages.search_fields(form, pages.Criteria(file="260875"))

    assert fields["ctl00$ContentPlaceHolder1$txtTit"] == "housing"
    assert fields["ctl00$ContentPlaceHolder1$txtFil"] == ""
    assert fields["ctl00$ContentPlaceHolder1$txtFileCreated1"] == "2026-10-01"
    assert fields["ctl00$ContentPlaceHolder1$txtFileCreated2$dateInput"] == "11/26/2026"
    assert fields["ctl00$ContentPlaceHolder1$radFileCreated"] == "between"
    assert fields["ctl00_ContentPlaceHolder1_lstMax_ClientState"] == '{"value": "500"}'
    assert fields["ctl00_ContentPlaceHolder1_lstYearsAdvanced_ClientState"] == '{"value": "All"}'
    assert fields["__VIEWSTATE"] == form.hidden["__VIEWSTATE"]
    assert "ctl00$ContentPlaceHolder1$radFileCreated" not in undated
    assert undated["ctl00$ContentPlaceHolder1$txtFil"] == "260875"
    assert HOUSING.describe() == (
        'Title contains "housing"; Introduced between 2026-10-01 and 2026-11-26; '
        "Number of results 500; Date All Years"
    )


def test_search_results_carry_each_matter_and_the_pages_own_count() -> None:
    found = pages.parse_search_results("sfgov", page("legislation_results"), HOUSING)

    assert found.total == 4
    assert [row["MatterFile"] for row in found.rows] == ["261287", "261267", "261262", "261261"]
    first = found.rows[1]
    assert first["MatterId"] == 8326663
    assert first["MatterTypeName"] == "Resolution"
    assert first["MatterStatusName"] == "Scheduled for Committee Hearing"
    assert first["MatterIntroDate"] == "2026-11-17"
    assert first["MatterPassedDate"] is None
    assert first["LegislationDetailURL"] == (
        "https://sfgov.legistar.com/LegislationDetail.aspx"
        "?ID=8326663&GUID=10F13A9A-5CE8-419E-AAD7-3E271B9505E0"
    )


def test_a_paged_result_keeps_the_total_the_pager_gives() -> None:
    criteria = pages.Criteria(first=date(2026, 9, 1), last=date(2026, 11, 26))

    found = pages.parse_search_results("sfgov", page("legislation_results_paged"), criteria)

    # The fixture keeps 3 of the 100 rows the page showed of its 363 matches.
    assert (found.total, len(found.rows)) == (363, 3)


def test_no_match_is_told_apart_from_a_search_that_never_ran() -> None:
    none = pages.parse_search_results(
        "sfgov", page("legislation_results_none"), pages.Criteria(title="zzqqxxjj")
    )
    unsearched = page("legislation_results_none").replace(
        "No records were found.", "Please enter your search criteria."
    )

    unfinished = page("legislation_results_none").replace(
        "No records were found.", "The search could not be completed. Please try again."
    )

    assert (none.total, none.rows) == (0, [])
    assert none.newest_first is True
    # Only the page's own no-match line reads as no match.
    for html in (unsearched, unfinished):
        with pytest.raises(SourceError, match="showed no result"):
            pages.parse_search_results("sfgov", html, pages.Criteria(title="zzqqxxjj"))


@pytest.mark.parametrize(
    "criteria",
    [
        pages.Criteria(title="rent", first=HOUSING.first, last=HOUSING.last),
        pages.Criteria(title="housing"),
        pages.Criteria(title="housing", first=date(2026, 1, 1), last=HOUSING.last),
        pages.Criteria(file="housing", first=HOUSING.first, last=HOUSING.last),
    ],
)
def test_a_page_that_ran_another_search_is_refused(criteria: pages.Criteria) -> None:
    with pytest.raises(SourceError, match="did not run the requested search"):
        pages.parse_search_results("sfgov", page("legislation_results"), criteria)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (">4 records<", ">7 records<", "do not match its count"),
        (">4 records<", ">Group<", "gave no count"),
        (
            'href="LegislationDetail.aspx?ID=8329872',
            'href="https://example.org/x?ID=8329872',
            "no link",
        ),
        ('href="LegislationDetail.aspx?ID=8329872', 'href="Other.aspx?ID=8329872', "no link"),
        ("GUID=12FF587B-F263-408F-B47C-1632D615BDCB", "GUID=../../etc", "no link"),
        # Addresses the URL parser itself refuses must fail the same way.
        (
            'href="LegislationDetail.aspx?ID=8329872',
            'href="//[/LegislationDetail.aspx?ID=1',
            "no link",
        ),
        (
            'href="LegislationDetail.aspx?ID=8329872',
            'href="//a\u2100b/Legislation.aspx?ID=1',
            "no link",
        ),
        (">11/24/2026<", ">24 November<", "date in an unknown form"),
        (">4 records<", ">" + "9" * 5000 + " records<", "gave no count"),
        (">Introduced</a>", ">File Created</a>", "lacks a column"),
    ],
)
def test_a_malformed_results_page_is_refused(old: str, new: str, message: str) -> None:
    html = page("legislation_results")
    assert old in html

    with pytest.raises(SourceError, match=message):
        pages.parse_search_results("sfgov", html.replace(old, new), HOUSING)


def test_a_matter_page_gives_details_sponsors_and_action_history() -> None:
    matter = pages.parse_matter("sfgov", page("legislation_detail"))

    assert matter.details == {
        "MatterFile": "260875",
        "MatterVersion": "1",
        "MatterName": (
            "Administrative Code - Exempting Members of the Rent Board from Term Limits"
        ),
        "MatterTitle": (
            "Ordinance amending the Administrative Code to exempt members of the Residential "
            "Rent Stabilization and Arbitration Board from term limits."
        ),
        "MatterTypeName": "Ordinance",
        "MatterStatusName": "Passed",
        "MatterBodyName": "Clerk of the Board",
        "MatterIntroDate": "2026-07-28",
        "MatterAgendaDate": None,
        "MatterPassedDate": "2026-10-02",
        "MatterEnactmentDate": "2026-10-02",
        "MatterEnactmentNumber": "207-26",
        "Sponsors": ["Rafael Mandelman"],
    }
    assert matter.history is not None and len(matter.history) == 6
    assert matter.history_unread is False
    assert all(tuple(action) == pages.HISTORY_FIELDS for action in matter.history)
    mayor, final = matter.history[0], matter.history[1]
    # The mayor's approval has an action page and no meeting.
    assert (mayor["ActionBy"], mayor["Action"], mayor["Result"]) == ("Mayor", "APPROVED", None)
    assert mayor["EventItemId"] == 37824798 and mayor["EventId"] is None
    assert final == {
        "ActionDate": "2026-09-29",
        "Version": "1",
        "ActionBy": "Board of Supervisors",
        "Action": "FINALLY PASSED",
        "Result": "Pass",
        "EventItemId": 37737674,
        "EventId": BOARD_ID,
        "EventGuid": BOARD_GUID,
        "ActionDetailURL": (
            "https://sfgov.legistar.com/HistoryDetail.aspx"
            "?ID=37737674&GUID=729E0332-5455-4D13-9F66-F31F34731C3E"
        ),
        "MeetingDetailURL": (
            f"https://sfgov.legistar.com/MeetingDetail.aspx?ID={BOARD_ID}&GUID={BOARD_GUID}"
        ),
    }


def test_several_sponsors_are_listed_one_by_one() -> None:
    html = page("legislation_detail")
    one = '<a href="PersonDetail.aspx?ID=196476&amp;GUID=63B530EB-D641-42BB-BFEB-A1367DC844CE">'
    assert one in html
    two = html.replace(one, '<a href="PersonDetail.aspx?ID=1&amp;GUID=x">Connie Chan</a>, ' + one)

    assert pages.parse_matter("sfgov", two).details["Sponsors"] == [
        "Connie Chan",
        "Rafael Mandelman",
    ]


def test_history_links_that_leave_the_site_carry_no_ids() -> None:
    html = page("legislation_detail")
    popup = "radopen(&#39;HistoryDetail.aspx?ID=37737674"
    meeting = 'href="MeetingDetail.aspx?ID=1449004'
    assert popup in html and meeting in html
    html = html.replace(popup, "radopen(&#39;https://example.org/HistoryDetail.aspx?ID=37737674")
    html = html.replace(meeting, 'href="//example.org/MeetingDetail.aspx?ID=1449004')

    final = pages.parse_matter("sfgov", html).history[1]

    assert final["Action"] == "FINALLY PASSED"
    assert final["EventItemId"] is None and final["ActionDetailURL"] is None
    assert final["EventId"] is None and final["MeetingDetailURL"] is None


def test_a_meeting_page_gives_its_header_and_legislative_files() -> None:
    meeting = pages.parse_meeting("sfgov", page("meeting_detail"))

    assert meeting.header == {
        "EventBodyName": "Rules Committee",
        "EventDate": "2026-09-14",
        "EventTime": "10:00 AM",
        "EventLocation": "City Hall, Legislative Chamber, Room 250 Regular Meeting",
        "EventAgendaStatusName": "Final",
        "EventMinutesStatusName": "Final",
        "EventAgendaFile": (
            f"https://sfgov.legistar.com/View.ashx?M=A&ID={MEETING_ID}&GUID={MEETING_GUID}"
        ),
        "EventMinutesFile": (
            f"https://sfgov.legistar.com/View.ashx?M=M&ID={MEETING_ID}&GUID={MEETING_GUID}"
        ),
    }
    assert len(meeting.items) == 7 and meeting.unread is False
    assert meeting.item_guids[ITEM_ID] == ITEM_GUID
    last = meeting.items[-1]
    assert last["EventItemId"] == ITEM_ID
    assert last["EventItemMatterFile"] == "260875"
    assert last["EventItemMatterType"] == "Ordinance"
    assert (last["EventItemActionName"], last["EventItemPassedFlagName"]) == ("RECOMMENDED", "Pass")
    assert last["EventItemMatterId"] == MATTER_ID
    assert last["ActionDetailURL"] == (
        f"https://sfgov.legistar.com/HistoryDetail.aspx?ID={ITEM_ID}&GUID={ITEM_GUID}"
    )
    # The first file was amended before its vote: an action with no result.
    assert meeting.items[0]["EventItemPassedFlagName"] is None
    # One row per recorded action: two files appear twice, so 7 rows hold 5 files.
    assert len({item["EventItemMatterFile"] for item in meeting.items}) == 5
    assert meeting.view == "Legislation only"


def test_an_action_page_gives_each_members_vote() -> None:
    action = pages.parse_action(page("history_detail"))

    assert action.header["File"] == "260875"
    assert (action.header["Action"], action.header["Result"]) == ("FINALLY PASSED", "Pass")
    assert action.header["Mover"] is None
    assert len(action.votes) == 11 and action.unread is False
    assert action.votes[0] == {"VotePersonName": "Connie Chan", "VoteValueName": "Aye"}
    assert {vote["VoteValueName"] for vote in action.votes} == {"Aye"}


def test_an_action_without_a_roll_call_is_told_apart_from_a_page_without_a_vote_table() -> None:
    # The mayor's approval of file 260875: the page carries the vote table, with no rows.
    silent = pages.parse_action(page("history_detail_no_votes"))
    grid = 'id="ctl00_ContentPlaceHolder1_gridVote_ctl00"'
    assert grid in page("history_detail")

    assert silent.votes == [] and silent.unread is False
    assert (silent.header["File"], silent.header["Action"]) == ("260875", "APPROVED")
    with pytest.raises(SourceError, match="no vote table"):
        pages.parse_action(page("history_detail").replace(grid, 'id="other"'))
    with pytest.raises(SourceError):
        pages.parse_action("<html><body>Maintenance</body></html>")


def test_a_page_built_differently_fails_closed() -> None:
    matter = page("legislation_detail")
    assert ">Action</a>" in matter and "gridLegislation_ctl00" in matter

    # A history grid with other column names is refused, never read as empty actions.
    with pytest.raises(SourceError, match="lacks a column"):
        pages.parse_matter("sfgov", matter.replace(">Action</a>", ">Motion</a>"))
    with pytest.raises(SourceError, match="no table"):
        pages.parse_matter("sfgov", matter.replace("gridLegislation_ctl00", "other"))
    # A page with no sponsors field gives null, which differs from a field left empty.
    field = 'id="ctl00_ContentPlaceHolder1_lblSponsors2"'
    assert field in matter
    assert pages.parse_matter("sfgov", matter.replace(field, "")).details["Sponsors"] is None
    meeting = page("meeting_detail")
    assert ">Result</a>" in meeting
    with pytest.raises(SourceError, match="lacks a column"):
        pages.parse_meeting("sfgov", meeting.replace(">Result</a>", ">Outcome</a>"))


def test_cells_keep_nested_text_and_drop_script_text() -> None:
    html = page("meeting_detail")
    name = "<font>Presidential Appointment, Planning Commission - Malea Chavez</font>"
    assert html.count(name) == 2
    nested = html.replace(
        name, "<script>var x = 1;</script><table><tr><td>Nested name</td></tr></table>", 1
    )

    meeting = pages.parse_meeting("sfgov", nested)

    assert meeting.items[0]["EventItemMatterName"] == "Nested name"
    assert len(meeting.items) == 7


def test_a_missing_page_and_an_unchecked_address_are_refused() -> None:
    with pytest.raises(pages.PageNotFound):
        pages.parse_meeting("sfgov", "Invalid parameters!")
    with pytest.raises(SourceError):
        pages.page_url("sfgov", "Gateway.aspx", 1, MEETING_GUID)
    with pytest.raises(SourceError):
        pages.page_url("sfgov", pages.MEETING_PATH, 1, "1&x=https://example.org")
    assert (
        pages.reference("sfgov", "MeetingDetail.aspx?ID=1&GUID=" + MEETING_GUID, "X.aspx") is None
    )
    assert (
        pages.reference("sfgov", f"http://{SITE}/MeetingDetail.aspx?ID=1", pages.MEETING_PATH)
        is None
    )
    # A trailing newline, a refused address, a user name, or a port never passes.
    for href in (
        f"MeetingDetail.aspx?ID=5&GUID={MEETING_GUID}%0A",
        f"MeetingDetail.aspx?ID=5%0A&GUID={MEETING_GUID}",
        f"//[/MeetingDetail.aspx?ID=5&GUID={MEETING_GUID}",
        f"https://user@{SITE}/MeetingDetail.aspx?ID=5&GUID={MEETING_GUID}",
        f"https://{SITE}:8443/MeetingDetail.aspx?ID=5&GUID={MEETING_GUID}",
    ):
        assert pages.reference("sfgov", href, pages.MEETING_PATH) is None
    assert pages.reference(
        "sfgov", f"MeetingDetail.aspx?ID=5&GUID={MEETING_GUID.lower()}", pages.MEETING_PATH
    ) == (5, MEETING_GUID)


def test_only_plain_cookies_of_the_answer_are_kept_for_the_call() -> None:
    jar: dict[str, str] = {}
    headers = httpx.Headers(
        [
            ("Set-Cookie", "ASP.NET_SessionId=abc; path=/; secure; HttpOnly"),
            ("Set-Cookie", "Setting-60-Legislation Options=ID|Text|; expires=Tue, 26-Nov-2126"),
            ("Set-Cookie", "no-value; path=/"),
            ("Set-Cookie", "bell=a\x07b; path=/"),
            ("Set-Cookie", "huge=" + "x" * 2000),
        ]
    )

    legistar_module._keep_cookies(jar, headers)
    for number in range(40):
        legistar_module._keep_cookies(jar, httpx.Headers({"Set-Cookie": f"extra{number}=1"}))
    legistar_module._keep_cookies(jar, httpx.Headers({"Set-Cookie": "ASP.NET_SessionId=new"}))

    assert jar["ASP.NET_SessionId"] == "new"
    assert jar["Setting-60-Legislation Options"] == "ID|Text|"
    assert "no-value" not in jar and "bell" not in jar and "huge" not in jar
    assert len(jar) == 20


# --- search_legislation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_api_copy_is_answered_from_the_search_and_matter_pages() -> None:
    site = Site()

    found = (
        await site.legistar()
        .search_legislation(
            client="sfgov",
            text="  housing ",
            introduced_from="2026-10-01",
            introduced_to="2026-11-26",
        )
        .run()
    )

    assert site.paths() == [
        "GET /Legislation.aspx",
        "POST /Legislation.aspx",
        "POST /Legislation.aspx",
        *["GET /LegislationDetail.aspx"] * 4,
    ]
    get, switch, search, *matters = site.site_requests
    assert "cookie" not in get.headers
    cookies = "ASP.NET_SessionId=abc123; Setting-60-Legislation Year=This Year"
    assert switch.headers["cookie"] == cookies
    assert search.headers["cookie"] == f"{cookies}; Setting-60-Legislation Type="
    assert form_of(search)["ctl00$ContentPlaceHolder1$txtTit"] == "housing"
    # A matter page is a plain request: no cookie, and only the ID and GUID the page gave.
    assert all("cookie" not in request.headers for request in matters)
    assert dict(matters[0].url.params) == {
        "ID": "8329872",
        "GUID": "12FF587B-F263-408F-B47C-1632D615BDCB",
    }
    assert {request.headers["user-agent"] for request in site.requests} == {
        f"OpenCivicQuery/{__version__}"
    }

    result = found.result
    assert [row["MatterFile"] for row in result.rows] == ["261287", "261267", "261262", "261261"]
    assert all(tuple(row) == WEB_MATTER_FIELDS for row in result.rows)
    assert all(row["DetailPageRead"] is True for row in result.rows)
    assert all(row["Sponsors"] == ["Rafael Mandelman"] for row in result.rows)
    assert all(row["History"] is None for row in result.rows)
    # The search row gives the matter and its page; the matter's page fills in the rest.
    assert result.rows[1]["MatterId"] == 8326663
    assert result.rows[1]["LegislationDetailURL"].endswith(
        "LegislationDetail.aspx?ID=8326663&GUID=10F13A9A-5CE8-419E-AAD7-3E271B9505E0"
    )
    assert result.rows[1]["MatterEnactmentNumber"] == "207-26"
    assert result.completeness is Completeness.COMPLETE
    assert result.domain == SITE
    assert result.dataset_id == "sfgov/web-matters"
    assert result.source_url == result.query_url == "https://sfgov.legistar.com/Legislation.aspx"
    assert result.query == (
        'Legislation.aspx advanced search: Title contains "housing"; Introduced between '
        "2026-10-01 and 2026-11-26; Number of results 500; Date All Years"
    )
    assert found.warnings[0] == web_warning("sfgov", stale_reason(STALE))
    assert "holds no matter introduced after 2020-09-17" in found.warnings[0]
    assert NO_DATE_WARNING in found.warnings
    assert found.rules[:2] == [web_rule("sfgov", stale_reason(STALE)), WEB_SPONSOR_RULE]
    assert found.escalations == ["LEGISTAR_WEB_PAGES"]
    assert stale_warning("sfgov", STALE) not in found.warnings
    assert SITE_UNREAD_WARNING not in found.warnings


@pytest.mark.asyncio
async def test_a_file_number_finds_the_matter_with_its_action_history() -> None:
    site = Site()

    found = await site.legistar().search_legislation(client="sfgov", text="260875").run()

    assert site.paths() == [
        "GET /Legislation.aspx",
        "POST /Legislation.aspx",
        "POST /Legislation.aspx",
        "POST /Legislation.aspx",
        "GET /LegislationDetail.aspx",
    ]
    _, _, by_file, by_title, _ = site.site_requests
    assert form_of(by_file)["ctl00$ContentPlaceHolder1$txtFil"] == "260875"
    assert form_of(by_file)["ctl00$ContentPlaceHolder1$txtTit"] == ""
    assert form_of(by_title)["ctl00$ContentPlaceHolder1$txtTit"] == "260875"
    assert form_of(by_title)["ctl00$ContentPlaceHolder1$txtFil"] == ""
    # The first search's cookie never rides along with the second.
    assert by_title.headers["cookie"] == by_file.headers["cookie"]
    assert "Search-Title" not in by_title.headers["cookie"]
    (row,) = found.result.rows
    assert row["MatterFile"] == "260875"
    assert row["Sponsors"] == ["Rafael Mandelman"]
    assert row["MatterEnactmentNumber"] == "207-26"
    assert len(row["History"]) == 6
    assert row["History"][3]["EventItemId"] == ITEM_ID
    assert row["History"][3]["EventId"] == MEETING_ID
    assert row["History"][3]["EventGuid"] == MEETING_GUID
    assert found.result.completeness is Completeness.COMPLETE
    assert found.result.query == (
        'Legislation.aspx advanced search: File/enactment # contains "260875"; Number of '
        'results 500; Date All Years | Title contains "260875"; Number of results 500; '
        "Date All Years"
    )


@pytest.mark.asyncio
async def test_a_file_match_leads_and_a_matter_in_both_searches_is_listed_once() -> None:
    site = Site()
    # The title search returns four matters; the file search returns one of them again.
    site.searches[("title", "261262")] = echoing(page("legislation_results"), title="261262")
    site.searches[("file", "261262")] = (
        page("legislation_results_file")
        .replace("260875", "261262")
        .replace(f"ID={MATTER_ID}&amp;GUID={MATTER_GUID}", "ID=8326658&amp;GUID=" + MATTER_GUID)
    )

    found = await site.legistar().search_legislation(client="sfgov", text="261262").run()

    files = [row["MatterFile"] for row in found.result.rows]
    assert files == ["261262", "261287", "261267", "261261"]
    assert found.result.rows[0]["History"] is not None
    assert all(row["History"] is None for row in found.result.rows[1:])
    assert found.result.completeness is Completeness.COMPLETE


@pytest.mark.asyncio
async def test_a_setting_error_on_the_matters_api_is_answered_from_the_pages() -> None:
    site = Site(api="setting")

    found = (
        await site.legistar()
        .search_legislation(
            client="sfgov", text="housing", introduced_from="2026-10-01", introduced_to="2026-11-26"
        )
        .run()
    )

    assert found.result.returned_row_count == 4
    assert found.warnings[0] == web_warning("sfgov", SETTING_REASON)


@pytest.mark.asyncio
async def test_a_current_api_copy_never_touches_the_website() -> None:
    site = Site(api="current")

    found = await site.legistar().search_legislation(client="sfgov", text="housing").run()

    assert site.site_requests == []
    assert found.result.domain == LEGISTAR_HOST
    assert found.escalations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(503, text="busy"),
        httpx.Response(302, headers={"Location": "https://example.org/Legislation.aspx"}),
        httpx.Response(200, text="<html><body>Maintenance</body></html>"),
    ],
)
async def test_an_unreadable_site_leaves_the_api_answer_or_error(answer: httpx.Response) -> None:
    stale, broken = Site(), Site(api="setting")
    stale.answers[pages.SEARCH_PATH] = broken.answers[pages.SEARCH_PATH] = answer

    found = await stale.legistar().search_legislation(client="sfgov", text="housing").run()
    with pytest.raises(SourceQueryError) as caught:
        await broken.legistar().search_legislation(client="sfgov", text="housing").run()

    # A redirect is never followed, and the older API copy stands under its warning.
    assert {request.url.host for request in stale.requests} == {LEGISTAR_HOST, SITE}
    assert len(stale.site_requests) == 1
    assert found.result.domain == LEGISTAR_HOST
    assert found.warnings[:2] == [stale_warning("sfgov", STALE), SITE_UNREAD_WARNING]
    assert found.escalations == ["LEGISTAR_API_STALE"]
    # With no API answer to fall back on, the error says the site could not be read.
    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert "Try again in a minute" in caught.value.public_message
    assert "setting on the jurisdiction's side" in caught.value.public_message


@pytest.mark.asyncio
async def test_a_search_with_more_matches_than_one_view_is_partial_and_says_so() -> None:
    site = Site()
    site.searches[("title", "ordinance")] = echoing(
        page("legislation_results_paged"), title="ordinance", first="2026-09-01", last="2026-11-26"
    )
    site.matters.update({8329694: matter_page("261286"), 8328845: matter_page("261254")})

    found = (
        await site.legistar()
        .search_legislation(
            client="sfgov",
            text="ordinance",
            introduced_from="2026-09-01",
            introduced_to="2026-11-26",
        )
        .run()
    )

    assert found.result.returned_row_count == 3
    assert found.result.completeness is Completeness.TRUNCATED
    assert found.warnings[0] == search_gap_warning(363, 3, True)
    assert "found 363 matters" in found.warnings[0]
    assert "highest matter ID first" in found.warnings[0]
    assert found.rules[0].startswith("PARTIAL SEARCH")
    assert found.escalations == ["LEGISTAR_WEB_PAGES", "LEGISTAR_WEB_SEARCH_GAP"]
    assert "500 or more" in search_gap_warning(500, 100, True)
    # An order the page did not show is never claimed.
    assert "in the page's own order" in search_gap_warning(363, 3, False)
    assert "newest" not in search_gap_warning(363, 3, False)


@pytest.mark.asyncio
async def test_two_partial_searches_are_each_named() -> None:
    site = Site()
    paged = page("legislation_results_paged")
    site.searches[("title", "2612")] = echoing(paged, title="2612")
    site.searches[("file", "2612")] = echoing(paged, file="2612")
    site.matters.update({8329694: matter_page("261286"), 8328845: matter_page("261254")})

    found = await site.legistar().search_legislation(client="sfgov", text="2612").run()

    assert found.warnings[:2] == [search_gap_warning(363, 3, True)] * 2
    assert found.result.returned_row_count == 3
    assert found.result.completeness is Completeness.TRUNCATED


@pytest.mark.asyncio
async def test_a_history_the_page_cut_short_is_partial_and_says_so() -> None:
    site = Site()
    cut = page("legislation_detail")
    assert ">6 records<" in cut
    site.matters[MATTER_ID] = cut.replace(">6 records<", ">106 records<")

    found = await site.legistar().search_legislation(client="sfgov", text="260875").run()

    assert len(found.result.rows[0]["History"]) == 6
    assert found.result.completeness is Completeness.TRUNCATED
    assert found.warnings[0] == history_gap_warning(["260875"])
    assert found.rules[0] == HISTORY_GAP_RULE
    assert "LEGISTAR_WEB_HISTORY_GAP" in found.escalations


@pytest.mark.asyncio
async def test_a_matter_page_built_differently_is_left_unread() -> None:
    site = Site()
    renamed = matter_page("261267").replace(">Action</a>", ">Motion</a>")
    hidden = matter_page("261287").replace('id="ctl00_ContentPlaceHolder1_lblSponsors2"', "")
    site.matters.update({8329872: hidden, 8326663: renamed})

    found = await site.legistar().search_legislation(**HOUSING_SEARCH).run()

    first, second = found.result.rows[:2]
    assert (first["DetailPageRead"], first["Sponsors"]) == (True, None)
    assert (second["DetailPageRead"], second["Sponsors"]) == (False, None)
    assert WEB_SPONSOR_RULE in found.rules
    assert detail_warning(1, 4) in found.warnings


@pytest.mark.asyncio
async def test_the_limit_cuts_the_list_and_bounds_the_matter_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legistar_module, "MAX_DETAIL_PAGES", 2)
    site = Site()

    found = await site.legistar().search_legislation(**HOUSING_SEARCH).run()
    cut = await site.legistar().search_legislation(**HOUSING_SEARCH, limit=3).run()

    assert site.paths().count("GET /LegislationDetail.aspx") == 4
    rows = found.result.rows
    assert [row["DetailPageRead"] for row in rows] == [True, True, False, False]
    assert rows[2]["Sponsors"] is None and rows[2]["MatterFile"] == "261262"
    assert detail_warning(2, 4) in found.warnings
    assert WEB_TITLE_CUT_WARNING in found.warnings
    assert DETAIL_RULE in found.rules
    assert "LEGISTAR_WEB_DETAILS_UNREAD" in found.escalations
    assert found.result.completeness is Completeness.COMPLETE
    assert cut.result.returned_row_count == 3
    assert cut.result.completeness is Completeness.TRUNCATED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [
        httpx.Response(503, text="busy"),
        httpx.Response(200, text="Invalid parameters!"),
        httpx.Response(200, text=matter_page("999999")),
    ],
)
async def test_a_matter_page_that_fails_is_named_and_the_list_stays(broken: httpx.Response) -> None:
    site = Site()

    def third_fails(request: httpx.Request) -> httpx.Response:
        return broken if request.url.params["ID"] == "8326658" else site.site_answer(request)

    site.answers[pages.MATTER_PATH] = third_fails

    found = await site.legistar().search_legislation(**HOUSING_SEARCH).run()

    assert [row["DetailPageRead"] for row in found.result.rows] == [True, True, False, False]
    assert found.result.returned_row_count == 4
    assert detail_warning(2, 4) in found.warnings
    # The reading stops at the first failure, so the site is not asked again.
    assert site.paths().count("GET /LegislationDetail.aspx") == 3


@pytest.mark.asyncio
async def test_a_slow_search_page_leaves_the_api_answer_within_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legistar_module, "PAGE_TIMEOUT_SECONDS", 0.2)
    site = Site()
    site.delay = 5

    found = await asyncio.wait_for(
        site.legistar().search_legislation(**HOUSING_SEARCH).run(), timeout=3
    )

    assert found.result.domain == LEGISTAR_HOST
    assert found.warnings[0] == stale_warning("sfgov", STALE)


@pytest.mark.asyncio
async def test_matter_pages_past_the_budget_are_left_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legistar_module, "PAGE_TIMEOUT_SECONDS", 0.3)
    site = Site()

    async def second_stalls(request: httpx.Request) -> httpx.Response:
        if request.url.params["ID"] != "8329872":
            await asyncio.sleep(5)
        return site.site_answer(request)

    site.answers[pages.MATTER_PATH] = second_stalls

    found = await asyncio.wait_for(
        site.legistar().search_legislation(**HOUSING_SEARCH).run(), timeout=3
    )

    # The matters stay listed; the pages the budget did not reach are named.
    assert [row["DetailPageRead"] for row in found.result.rows] == [True, False, False, False]
    assert found.result.rows[0]["Sponsors"] == ["Rafael Mandelman"]
    assert detail_warning(1, 4) in found.warnings
    assert found.warnings[0] == web_warning("sfgov", stale_reason(STALE))


@pytest.mark.asyncio
async def test_an_open_ended_date_is_searched_within_the_pages_range() -> None:
    site = Site()

    found = (
        await site.legistar()
        .search_legislation(client="sfgov", text="zoning", introduced_to="2026-11-26")
        .run()
    )
    refused = (
        await site.legistar()
        .search_legislation(client="sfgov", text="zoning", introduced_to="1979-12-31")
        .run()
    )

    search = form_of(site.site_requests[2])
    assert search["ctl00$ContentPlaceHolder1$txtFileCreated1"] == "1980-01-01"
    assert search["ctl00$ContentPlaceHolder1$txtFileCreated2"] == "2026-11-26"
    assert search_dates_warning(date(1980, 1, 1), date(2026, 11, 26)) in found.warnings
    assert found.result.rows == []
    assert found.result.completeness is Completeness.COMPLETE
    # Dates the page cannot search leave the API's older copy, under its warning.
    assert refused.result.domain == LEGISTAR_HOST
    assert refused.warnings[0] == stale_warning("sfgov", STALE)


@pytest.mark.asyncio
async def test_a_page_already_on_advanced_search_is_not_switched() -> None:
    site = Site()

    def advanced_first(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=page("legislation_advanced"))
        return site.site_answer(request)

    site.answers[pages.SEARCH_PATH] = advanced_first

    found = await site.legistar().search_legislation(**HOUSING_SEARCH).run()

    assert site.paths()[:3] == [
        "GET /Legislation.aspx",
        "POST /Legislation.aspx",
        "GET /LegislationDetail.aspx",
    ]
    assert found.result.returned_row_count == 4


@pytest.mark.asyncio
async def test_one_page_reading_call_runs_at_a_time() -> None:
    site = Site()
    site.delay = 0.01
    legistar = site.legistar()

    answers = await asyncio.gather(
        legistar.search_legislation(**HOUSING_SEARCH).run(),
        legistar.get_meeting_items(
            client="sfgov", event_id=MEETING_ID, event_guid=MEETING_GUID
        ).run(),
        legistar.get_item_votes(
            client="sfgov", event_id=MEETING_ID, event_item_id=ITEM_ID, event_guid=MEETING_GUID
        ).run(),
    )

    assert site.busiest == 1
    assert [answer.result.returned_row_count for answer in answers] == [4, 7, 11]


# --- get_meeting_items ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_meeting_items_come_from_the_meeting_page_when_the_api_cannot_answer() -> None:
    site = Site()

    found = (
        await site.legistar()
        .get_meeting_items(client="sfgov", event_id=MEETING_ID, event_guid=MEETING_GUID.lower())
        .run()
    )

    (request,) = site.site_requests
    assert str(request.url) == (
        f"https://sfgov.legistar.com/MeetingDetail.aspx?ID={MEETING_ID}&GUID={MEETING_GUID}"
    )
    assert "cookie" not in request.headers
    result = found.result
    assert result.returned_row_count == 7
    assert all(tuple(row) == WEB_ITEM_FIELDS for row in result.rows)
    assert result.rows[-1]["EventItemMatterFile"] == "260875"
    assert result.rows[-1]["EventItemId"] == ITEM_ID
    assert result.completeness is Completeness.COMPLETE
    assert result.source_url == result.query_url == str(request.url)
    assert result.dataset_name == (
        "Legistar website agenda items, client sfgov: Rules Committee, meeting 1439610 on "
        "2026-09-14, minutes status Final"
    )
    assert result.query == (
        f"MeetingDetail.aspx?ID={MEETING_ID}&GUID={MEETING_GUID}, view: Legislation only"
    )
    assert found.warnings[:2] == [web_warning("sfgov", SETTING_REASON), WEB_ITEMS_WARNING]
    assert found.escalations == ["LEGISTAR_WEB_PAGES"]
    # A row is a recorded action, so the 7 rows are never called 7 files.
    assert found.row_grain == WEB_ITEM_GRAIN and "two rows" in found.row_grain
    assert WEB_ITEM_COUNT_RULE in found.rules
    assert len({row["EventItemMatterFile"] for row in result.rows}) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "view"), [("Show: All agenda items", "All agenda items"), ("Views", None)]
)
async def test_a_meeting_page_in_another_view_makes_no_claim_about_what_it_leaves_out(
    label: str, view: str | None
) -> None:
    site = Site()
    html = page("meeting_detail")
    assert html.count(">Show: Legislation only<") == 1
    site.answers[pages.MEETING_PATH] = httpx.Response(
        200, text=html.replace(">Show: Legislation only<", f">{label}<")
    )

    found = (
        await site.legistar()
        .get_meeting_items(client="sfgov", event_id=MEETING_ID, event_guid=MEETING_GUID)
        .run()
    )

    assert found.warnings[1] == items_view_warning(view)
    assert "legislation only" not in found.warnings[1]
    assert found.result.query.endswith(f"view: {view or 'not stated'}")


@pytest.mark.asyncio
async def test_a_meeting_page_needs_the_meetings_guid() -> None:
    site = Site()
    legistar = site.legistar()

    with pytest.raises(SourceQueryError) as missing:
        await legistar.get_meeting_items(client="sfgov", event_id=MEETING_ID).run()
    with pytest.raises(SourceQueryError) as malformed:
        legistar.get_meeting_items(
            client="sfgov", event_id=MEETING_ID, event_guid="x&y=https://example.org/a"
        )

    assert missing.value.code == "EVENT_GUID_REQUIRED"
    assert missing.value.argument == "event_guid"
    assert "EventGuid that find_meetings returned" in missing.value.public_message
    assert malformed.value.code == "INVALID_QUERY_ARGUMENT"
    assert site.site_requests == []


@pytest.mark.asyncio
async def test_a_wrong_guid_reads_as_not_found_and_a_failing_site_leaves_the_api_error() -> None:
    wrong, down = Site(), Site()
    wrong.answers[pages.MEETING_PATH] = httpx.Response(200, text="Invalid parameters!")
    down.answers[pages.MEETING_PATH] = httpx.Response(503, text="busy")

    with pytest.raises(SourceQueryError) as not_found:
        await (
            wrong.legistar()
            .get_meeting_items(client="sfgov", event_id=MEETING_ID, event_guid=BOARD_GUID)
            .run()
        )
    with pytest.raises(SourceQueryError) as api_error:
        await (
            down.legistar()
            .get_meeting_items(client="sfgov", event_id=MEETING_ID, event_guid=MEETING_GUID)
            .run()
        )

    assert not_found.value.code == "NOT_FOUND"
    assert not_found.value.argument == "event_guid"
    assert api_error.value.code == "SOURCE_UNAVAILABLE"
    assert "Try again in a minute" in api_error.value.public_message


@pytest.mark.asyncio
async def test_a_meeting_page_that_continues_is_partial() -> None:
    site = Site()
    html = page("meeting_detail")
    assert ">7 records<" in html
    site.answers[pages.MEETING_PATH] = httpx.Response(
        200, text=html.replace(">7 records<", ">107 records<")
    )

    found = (
        await site.legistar()
        .get_meeting_items(client="sfgov", event_id=MEETING_ID, event_guid=MEETING_GUID)
        .run()
    )

    assert found.result.completeness is Completeness.TRUNCATED
    assert found.warnings[0] == PARTIAL_PAGE_WARNING


@pytest.mark.asyncio
async def test_a_meeting_the_stale_api_lacks_is_read_from_its_page() -> None:
    lacking, current = Site(api="missing"), Site(api="current")

    found = (
        await lacking.legistar()
        .get_meeting_items(client="sfgov", event_id=MEETING_ID, event_guid=MEETING_GUID)
        .run()
    )
    with pytest.raises(SourceQueryError) as unknown:
        await (
            current.legistar()
            .get_meeting_items(client="sfgov", event_id=MEETING_ID, event_guid=MEETING_GUID)
            .run()
        )
    with pytest.raises(SourceQueryError) as unasked:
        await lacking.legistar().get_meeting_items(client="sfgov", event_id=MEETING_ID).run()

    assert found.result.returned_row_count == 7
    assert found.warnings[0] == web_warning("sfgov", stale_reason(STALE))
    # A current API copy that lacks the meeting is believed, and the site is left alone.
    assert unknown.value.code == "NOT_FOUND"
    assert current.site_requests == []
    assert unasked.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_a_site_that_fails_for_a_stale_client_is_never_reported_as_no_such_record() -> None:
    site = Site(api="missing")
    site.answers[pages.MEETING_PATH] = httpx.Response(503, text="busy")
    legistar = site.legistar()

    with pytest.raises(SourceQueryError) as items:
        await legistar.get_meeting_items(
            client="sfgov", event_id=MEETING_ID, event_guid=MEETING_GUID
        ).run()
    with pytest.raises(SourceQueryError) as votes:
        await legistar.get_item_votes(
            client="sfgov", event_id=MEETING_ID, event_item_id=ITEM_ID, event_guid=MEETING_GUID
        ).run()

    for caught in (items, votes):
        assert caught.value.code == "SOURCE_UNAVAILABLE"
        assert "holds no matter introduced after 2020-09-17" in caught.value.public_message
        assert "no public record" not in caught.value.public_message


@pytest.mark.asyncio
async def test_page_reading_ends_inside_the_calls_own_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legistar_module, "CALL_BUDGET_SECONDS", 0.05)

    class SlowApi(Site):
        async def handle(self, request: httpx.Request) -> httpx.Response:
            if request.url.host == LEGISTAR_HOST:
                await asyncio.sleep(0.1)
            return await super().handle(request)

    site = SlowApi()

    with pytest.raises(SourceQueryError) as caught:
        await (
            site.legistar()
            .get_meeting_items(client="sfgov", event_id=MEETING_ID, event_guid=MEETING_GUID)
            .run()
        )

    # The API used up the call's time, so the site is not asked and the error says to retry.
    assert caught.value.code == "SOURCE_TIMEOUT"
    assert site.site_requests == []


@pytest.mark.asyncio
async def test_the_turn_is_free_again_after_a_timeout_and_after_a_cancelled_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legistar_module, "PAGE_TIMEOUT_SECONDS", 0.2)
    site = Site()
    legistar = site.legistar()
    items = {"client": "sfgov", "event_id": MEETING_ID, "event_guid": MEETING_GUID}

    site.delay = 5
    with pytest.raises(SourceQueryError) as timed_out:
        await legistar.get_meeting_items(**items).run()
    holder = asyncio.create_task(legistar.get_meeting_items(**items).run())
    await asyncio.sleep(0.05)
    waiter = asyncio.create_task(legistar.get_meeting_items(**items).run())
    await asyncio.sleep(0.05)
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder
    with pytest.raises(SourceQueryError) as waited:
        await waiter
    site.delay = 0
    found = await legistar.get_meeting_items(**items).run()

    assert timed_out.value.code == "SOURCE_TIMEOUT"
    assert waited.value.code == "SOURCE_TIMEOUT"
    assert found.result.returned_row_count == 7
    assert legistar._page_lock.locked() is False


# --- get_item_votes -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_votes_come_from_the_action_page_found_through_the_meeting_page() -> None:
    site = Site()

    found = (
        await site.legistar()
        .get_item_votes(
            client="sfgov", event_id=MEETING_ID, event_item_id=ITEM_ID, event_guid=MEETING_GUID
        )
        .run()
    )

    meeting, action = site.site_requests
    assert meeting.url.path == "/MeetingDetail.aspx"
    # The action page's GUID comes from the meeting page's own row for the item.
    assert str(action.url) == (
        f"https://sfgov.legistar.com/HistoryDetail.aspx?ID={ITEM_ID}&GUID={ITEM_GUID}"
    )
    result = found.result
    assert result.returned_row_count == 11
    assert result.rows[0] == {"VotePersonName": "Connie Chan", "VoteValueName": "Aye"}
    assert result.completeness is Completeness.COMPLETE
    assert result.source_url == result.query_url == str(action.url)
    assert result.dataset_name == (
        "Legistar website votes, client sfgov: EventItemId 37487161, file 260875 version 1: "
        "Administrative Code - Exempting Members of the Rent Board from Term Limits; action "
        "FINALLY PASSED, result Pass, mover None, seconder None; Rules Committee, meeting "
        "1439610 on 2026-09-14, minutes status Final"
    )
    assert found.warnings[0] == web_warning("sfgov", SETTING_REASON)
    assert NO_VOTES_RULE not in found.rules
    assert found.escalations == ["LEGISTAR_WEB_PAGES"]


@pytest.mark.asyncio
async def test_an_item_the_meeting_page_does_not_list_is_not_found() -> None:
    site = Site()

    with pytest.raises(SourceQueryError) as caught:
        await (
            site.legistar()
            .get_item_votes(
                client="sfgov", event_id=MEETING_ID, event_item_id=37737674, event_guid=MEETING_GUID
            )
            .run()
        )

    assert caught.value.code == "NOT_FOUND"
    assert caught.value.argument == "event_item_id"
    assert site.paths() == ["GET /MeetingDetail.aspx"]


VOTES = {
    "client": "sfgov",
    "event_id": MEETING_ID,
    "event_item_id": ITEM_ID,
    "event_guid": MEETING_GUID,
}


@pytest.mark.asyncio
async def test_an_action_without_a_roll_call_escalates() -> None:
    site = Site()
    site.answers[pages.ACTION_PATH] = httpx.Response(200, text=page("history_detail_no_votes"))

    found = await site.legistar().get_item_votes(**VOTES).run()

    assert found.result.rows == []
    assert found.result.completeness is Completeness.COMPLETE
    assert found.rules[0] == NO_VOTES_RULE
    assert found.escalations == ["LEGISTAR_WEB_PAGES", "NO_VOTES_RECORDED"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old", "new"),
    [
        # A page with no vote table, and a page about another file, are pages not read.
        ('id="ctl00_ContentPlaceHolder1_gridVote_ctl00"', 'id="other"'),
        ("<font>260875</font>", "<font>260001</font>"),
    ],
)
async def test_an_action_page_that_cannot_be_trusted_is_an_error_and_never_no_votes(
    old: str, new: str
) -> None:
    site = Site()
    assert old in page("history_detail")
    site.answers[pages.ACTION_PATH] = httpx.Response(
        200, text=page("history_detail").replace(old, new)
    )

    with pytest.raises(SourceQueryError) as caught:
        await site.legistar().get_item_votes(**VOTES).run()

    assert caught.value.code == "SOURCE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_an_item_missing_from_a_partly_shown_meeting_page_is_not_called_absent() -> None:
    site = Site()
    html = page("meeting_detail")
    site.answers[pages.MEETING_PATH] = httpx.Response(
        200, text=html.replace(">7 records<", ">107 records<")
    )

    with pytest.raises(SourceQueryError) as caught:
        await site.legistar().get_item_votes(**(VOTES | {"event_item_id": 37999999})).run()

    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert caught.value.argument == "event_item_id"
    assert "showed only part of its list" in caught.value.public_message
    assert site.paths() == ["GET /MeetingDetail.aspx"]


@pytest.mark.asyncio
async def test_only_the_four_page_types_of_the_clients_own_site_are_requested() -> None:
    site = Site()
    legistar = site.legistar()

    await legistar.search_legislation(client="sfgov", text="260875").run()
    await legistar.get_meeting_items(
        client="sfgov", event_id=MEETING_ID, event_guid=MEETING_GUID
    ).run()
    await legistar.get_item_votes(
        client="sfgov", event_id=MEETING_ID, event_item_id=ITEM_ID, event_guid=MEETING_GUID
    ).run()

    assert {request.url.host for request in site.requests} == {LEGISTAR_HOST, SITE}
    assert {request.url.scheme for request in site.requests} == {"https"}
    assert {request.url.path.removeprefix("/") for request in site.site_requests} == set(
        pages.PAGE_PATHS
    )
    with pytest.raises(SourceError):
        await legistar._page("sfgov", "Gateway.aspx")

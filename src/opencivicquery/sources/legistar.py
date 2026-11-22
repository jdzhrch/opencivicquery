"""Granicus Legistar Web API: city and county meetings, agenda items, votes, legislation.

Most Legistar clients answer without a key. Every request goes to one fixed host; the
caller names only a client, which is checked against a strict pattern before it becomes
a path segment.

When a client's API refuses to list meetings because of a setting on the jurisdiction's
side, find_meetings reads the same client's public calendar page instead. That page lives
at <client>.legistar.com, a host the Web API has just confirmed by naming the client's
own setting error.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from opencivicquery.models import Completeness, SourceQueryResult
from opencivicquery.sources.base import Lookup, PreparedLookup, SourceError, SourceQueryError
from opencivicquery.sources.http import FixedHostClient

LEGISTAR_HOST = "webapi.legistar.com"
_BASE = f"https://{LEGISTAR_HOST}/v1"
SOURCE_NAME = "Legistar Web API"

MAX_REPLY_ROWS = 1000
# official: webapi.legistar.com/Home/Examples, "queries replies are limited to 1000
# responses".
MAX_LIMIT = 200
MAX_TEXT_LENGTH = 100
MAX_QUERY_LENGTH = 2000
# checked 2026-11-20: a matters query string of about 2,300 characters drew an HTML 404
# from the server instead of a result.
EARLIEST_DATE = date(1900, 1, 1)
LATEST_DATE = date(2100, 12, 31)

_CLIENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_TOKEN = re.compile(r"^[A-Za-z0-9+/=_.-]{1,1024}$")
# official: webapi.legistar.com/Home/Examples gives a token as a URL parameter,
# token=verylongbase64token.
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNKNOWN_CLIENT = "LegistarConnectionString setting is not set up"
# checked 2026-11-20: an unknown client name, and the retired chicago client, both
# returned HTTP 500 with this ExceptionMessage.
_CLIENT_SETTING = "is not setup in settings"
# checked 2026-11-21: sfgov answered every events request, even events/10000, with HTTP
# 400, a text/plain body, and the message "'Agenda Draft Status' or 'Agenda Status Not
# Vievable By The Public' is not setup in settings. Value should be greater than 0."
STALE_AFTER = timedelta(days=180)
NEWEST_MATTER_TIMEOUT_SECONDS = 10.0
# checked 2026-11-21: the newest MatterIntroDate was 2026-11-19 for oakland and
# 2026-11-16 for sanjose, but 2020-09-17 for sfgov, whose Legistar website lists 2026
# meetings and whose API holds no file 240001 or 250001. A legislature in recess can go a
# few months without a new matter, so the 180-day line is a judgment, not a documented
# rule, and the warning is worded as a condition. The check runs alongside the search and
# gives up after its own timeout, so it cannot hold a finished search past the service's
# deadline.

MAX_CALENDAR_YEARS = 2
CALENDAR_PAGE_ROWS = 100
CALENDAR_TIMEOUT_SECONDS = 25.0
# Each year costs one or two pages of about 1 MB. checked 2026-11-22: a sfgov year page
# was 1,067,607 bytes and answered in 0.7 seconds. The calendar's own time budget ends
# before the service's 60-second deadline, so a slow site still returns the API's error
# and its pointer to the website.
NEXT_YEAR = "Next Year"
# checked 2026-11-22: the sfgov year menu lists each year to the current one, then Next
# Year among the relative spans.
_GRID_ID = "ctl00_ContentPlaceHolder1_gridCalendar_ctl00"
_YEAR_INPUT_ID = "ctl00_ContentPlaceHolder1_lstYears_Input"
_BODY_INPUT_ID = "ctl00_ContentPlaceHolder1_lstBodies_Input"
_BODY_MENU_ID = "ctl00_ContentPlaceHolder1_lstBodies_DropDown"
# checked 2026-11-22: with "Calendar Body=All" the sfgov body menu showed its first
# entry, "BOS and Committees", above each named body.
_SETTING_PREFIX = re.compile(r"^Setting-(\d{1,6})-Calendar Year=")
_CALENDAR_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
# checked 2026-11-22: a plain GET of sfgov.legistar.com/Calendar.aspx sets the cookies
# "Setting-60-Calendar Year=This Month", "Setting-60-Calendar Body=All", and
# "Setting-60-ASP.calendar_aspx.gridCalendar.SortExpression=MeetingStartDate DESC". Sending
# "Calendar Year=2026" back on a plain GET returned the 2026 view, 100 rows a page with a
# pager reading "Page 1 of 2, items 1 to 100 of 181"; "MeetingStartDate ASC" returned the
# oldest 100, 2026-01-05 to 2026-06-11, and the newest 100 ran from 2026-05-13 to
# 2026-11-19. The site sent no robots.txt and the page links no terms of use.

MEETING_FIELDS = (
    "EventId",
    "EventBodyName",
    "EventDate",
    "EventTime",
    "EventLocation",
    "EventAgendaStatusName",
    "EventMinutesStatusName",
    "EventAgendaFile",
    "EventMinutesFile",
    "EventInSiteURL",
    "EventMedia",
    "EventVideoPath",
    "EventComment",
    "EventLastModifiedUtc",
)
ITEM_FIELDS = (
    "EventItemId",
    "EventItemAgendaNumber",
    "EventItemAgendaSequence",
    "EventItemTitle",
    "EventItemMatterFile",
    "EventItemMatterName",
    "EventItemMatterType",
    "EventItemMatterStatus",
    "EventItemConsent",
    "EventItemActionName",
    "EventItemActionText",
    "EventItemPassedFlagName",
    "EventItemRollCallFlag",
    "EventItemTally",
    "EventItemMover",
    "EventItemSeconder",
    "EventItemAgendaNote",
    "EventItemMinutesNote",
    "EventItemLastModifiedUtc",
)
VOTE_FIELDS = (
    "VotePersonName",
    "VoteValueName",
    "VoteResult",
    "VoteLastModifiedUtc",
)
MATTER_FIELDS = (
    "MatterId",
    "MatterFile",
    "MatterName",
    "MatterTitle",
    "MatterTypeName",
    "MatterStatusName",
    "MatterBodyName",
    "MatterIntroDate",
    "MatterAgendaDate",
    "MatterPassedDate",
    "MatterEnactmentDate",
    "MatterEnactmentNumber",
    "MatterRequester",
    "MatterLastModifiedUtc",
)

MEETING_GRAIN = "One meeting of one Legistar body (a council, committee, board, or commission)."
ITEM_GRAIN = (
    "One agenda item of one meeting, with the action, result, mover, and seconder the clerk "
    "recorded for it."
)
VOTE_GRAIN = "One member's recorded vote on one agenda item."
MATTER_GRAIN = "One Legistar matter: a piece of legislation, report, or other file item."
# checked 2026-11-20: the API serves these as the events, eventitems, votes, and matters
# resources; a matter's MatterTypeName names its kind, such as Informational Report.

PUBLIC_ONLY_WARNING = (
    "Legistar returns only the records the clerk marked public and available on the "
    "jurisdiction's Legistar site."
)
# official: webapi.legistar.com/Home/Examples, "Items returned to GET requests are limited
# to those items marked as public and available for view on InSite."

CLERK_EDITS_WARNING = (
    "These are the records the clerk's office entered, and they can change after this "
    "retrieval. Each record carries a LastModifiedUtc time, and each meeting's minutes carry "
    "a status such as DRAFT or FINAL. Give the minutes status and the LastModifiedUtc time "
    "beside what you report."
)
# checked 2026-11-20: Oakland meeting 9617, held 2026-11-19, had minutes status DRAFT, and
# its vote records were edited at 18:33 and 22:42 UTC that day, after they were created.

LOCAL_TIME_WARNING = (
    "EventDate is the meeting's calendar date and EventTime its local start time as text. "
    "Fields ending in LastModifiedUtc are in UTC."
)
# checked 2026-11-20: Oakland EventDate values carry a midnight time, with the start time
# in EventTime, such as "10:30 AM".

EMPTY_RESULT_WARNING = (
    "An empty action, result, or tally does not show that nothing happened. Many items are "
    "only scheduled or received, and EventItemTally and EventItemRollCallFlag can be empty "
    "even when member votes exist. Call get_item_votes for any item with a result, and "
    "check the minutes file."
)
# checked 2026-11-20: at Oakland meeting 9617, 52 of 83 items had action Scheduled and 21
# had none; item 240300 passed with EventItemTally empty and EventItemRollCallFlag 0, yet
# the votes endpoint listed member votes.

VOTE_RECORD_WARNING = (
    "Each vote is the value the clerk recorded for that member. Compare the list with the "
    "meeting's minutes file before reporting how a member voted."
)
# official: webapi.legistar.com/Home/Examples sends readers to the EventItems Votes
# endpoint "to see the vote tally"; checked 2026-11-20: Oakland item 240300 listed one
# VoteValueName per member, such as Aye.

SUBSTRING_WARNING = (
    "The text search finds the words anywhere inside MatterTitle or MatterName, ignoring "
    "case and including inside longer words: 'rent' also matches 'Current'. Read each "
    "title before counting it as relevant, and say the count is of matters whose title "
    "contains the text."
)
# checked 2026-11-20: substringof('rent', MatterTitle) at Oakland returned a matter whose
# title held 'Current' and no word 'rent'; substringof('city council', EventBodyName)
# matched 'City Council'.

TOKEN_LINK_WARNING = (
    "This jurisdiction requires its own Legistar API token, which the server adds to each "
    "request and leaves out of query_url. Opening query_url without a token returns an "
    "error; cite the meeting or matter page instead."
)
# checked 2026-11-20: nyc returned HTTP 403 to a request without a token.

NO_DATE_WARNING = "A date filter leaves out matters that have no introduction date in Legistar."
# checked 2026-11-20: Oakland lists matters, mostly from before 2008, with an empty
# MatterIntroDate; a filter on that field cannot match them.


def stale_warning(client: str, newest: date) -> str:
    return (
        f"The newest matter that the Legistar API holds for {client} was introduced on "
        f"{newest.isoformat()}, more than 180 days ago. If {client} introduced matters after "
        "that day, this API does not hold them, whatever dates you ask for; "
        f"https://{client}.legistar.com may list them. An empty or short result here does "
        "not show that no newer legislation exists."
    )


def stale_rule(client: str, newest: date) -> str:
    return (
        f"STALE API COPY. Say that the Legistar API for {client} holds no matter introduced "
        f"after {newest.isoformat()}, and send the reader to https://{client}.legistar.com "
        "for anything newer. Never report that no newer legislation exists."
    )


NO_VOTES_RULE = (
    "NO MEMBER VOTES RECORDED. Say that Legistar lists no member votes for this item. Do "
    "not say the item had no vote; check the action, the result, and the minutes file."
)

LEGISTAR_RULE = (
    "Cite each meeting by its EventInSiteURL, and each matter by its LegislationDetailURL. "
    "Give the minutes status beside any action or vote you report."
)
VOTE_RULE = (
    "Name the agenda item and the meeting date beside any vote you report, and say the "
    "votes are as the clerk recorded them."
)

CALENDAR_FIELDS = (
    "EventBodyName",
    "EventDate",
    "EventTime",
    "EventLocation",
    "EventInSiteURL",
    "EventAgendaFile",
    "EventMinutesFile",
)
CALENDAR_GRAIN = (
    "One meeting of one Legistar body, as a row of the jurisdiction's public Legistar "
    "calendar page."
)


def calendar_warning(client: str) -> str:
    return (
        f"The Legistar API for {client} cannot list meetings because of a setting on the "
        f"jurisdiction's side, so these meetings come from its public calendar page, "
        f"https://{client}.legistar.com/Calendar.aspx. The page gives each meeting's agenda "
        "and minutes files but no agenda items or member votes, and get_meeting_items and "
        "get_item_votes cannot read this client's meetings. Take items, actions, and votes "
        "from the agenda and minutes files."
    )


def calendar_rule(client: str) -> str:
    return (
        f"WEBSITE CALENDAR. Say that these meetings come from the Legistar calendar page of "
        f"{client} because its API cannot list meetings. Cite each meeting by its "
        "EventInSiteURL, and take agenda items, actions, and votes only from the agenda and "
        "minutes files."
    )


CALENDAR_MINUTES_WARNING = (
    "The clerk's office can change these meetings and files after this retrieval, and the "
    "calendar page does not say whether minutes are draft or final. The minutes file says "
    "so on its first page; give that status beside any action or vote you report."
)
# checked 2026-11-22: the sfgov minutes file for the 2026-11-17 Board meeting is headed
# "MEETING MINUTES - DRAFT".

CALENDAR_PAGE_WARNING = (
    "query_url opens the calendar page on the current month. To see these meetings there, "
    "choose the year in the page's year menu. EventDate is the meeting's calendar date and "
    "EventTime its local start time as text."
)

UNREAD_LINK_WARNING = (
    "Some agenda, minutes, or meeting links on the calendar page pointed somewhere other "
    "than the jurisdiction's Legistar site and were left out. A missing link here does not "
    "show that the document does not exist; open the meeting page."
)


def calendar_gap_warning(gaps: list[tuple[date, date]]) -> str:
    spans = "; ".join(f"{start.isoformat()} to {end.isoformat()}" for start, end in gaps)
    return (
        f"This tool could not read the calendar page's meetings from {spans}: the page "
        "shows a limited number of meetings per view and lists years only to the next one. "
        "The list leaves out some or all meetings in that span."
    )


def calendar_gap_rule(gaps: list[tuple[date, date]]) -> str:
    spans = "; ".join(f"{start.isoformat()} to {end.isoformat()}" for start, end in gaps)
    return (
        f"PARTIAL CALENDAR. Say that meetings from {spans} were not read, and never report "
        "that no meeting took place then."
    )


def _invalid(argument: str, message: str) -> SourceQueryError:
    return SourceQueryError(
        message, code="INVALID_QUERY_ARGUMENT", argument=argument, public_message=message
    )


def validate_client(client: str) -> str:
    normalized = client.strip().lower()
    if not _CLIENT.match(normalized):
        raise _invalid(
            "client",
            "Give the Legistar client name: the first part of the jurisdiction's Legistar "
            "address, such as oakland for oakland.legistar.com.",
        )
    return normalized


def _date(argument: str, value: str) -> date:
    if not _DATE.match(value.strip()):
        raise _invalid(argument, f"Give {argument} as YYYY-MM-DD.")
    try:
        day = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise _invalid(argument, f"{argument} is not a real calendar date.") from exc
    if not EARLIEST_DATE <= day <= LATEST_DATE:
        raise _invalid(argument, f"Give {argument} between 1900-01-01 and 2100-12-31.")
    return day


def _bounded(params: dict[str, str]) -> dict[str, str]:
    """Refuse a request whose query string the server would not accept."""
    if len(urlencode(params)) > MAX_QUERY_LENGTH:
        raise _invalid("text", "The search is too long once encoded. Use fewer or shorter words.")
    return params


def _limit(limit: int) -> int:
    if not 1 <= limit <= MAX_LIMIT:
        raise _invalid("limit", f"Give limit from 1 to {MAX_LIMIT}.")
    return limit


def _positive_id(argument: str, value: int) -> int:
    if not 1 <= value <= 2**31 - 1:
        raise _invalid(argument, f"Give {argument} as the positive number Legistar returned.")
    return value


def _odata_text(argument: str, text: str) -> str:
    """Return text as an OData string literal body, with single quotes doubled."""
    normalized = " ".join(text.split())
    if not 2 <= len(normalized) <= MAX_TEXT_LENGTH or any(
        unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in normalized
    ):
        raise _invalid(argument, f"Give {argument} as 2 to {MAX_TEXT_LENGTH} characters of text.")
    return normalized.replace("'", "''")


def _datetime_literal(day: date) -> str:
    return f"datetime'{day.isoformat()}'"


def _project(record: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise SourceError("The Legistar Web API returned a malformed record")
    return {name: record.get(name) for name in fields}


def _schema_hash(kind: str, fields: tuple[str, ...]) -> str:
    schema = {"kind": kind, "fields": list(fields)}
    return hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _meeting_label(event: dict[str, Any]) -> str:
    """Body, date, and minutes status of one meeting, for the dataset name."""
    return (
        f"{event.get('EventBodyName')}, meeting {event.get('EventId')} on "
        f"{str(event.get('EventDate'))[:10]}, minutes status {event.get('EventMinutesStatusName')}"
    )


def _meeting_url(client: str, event: dict[str, Any]) -> str:
    url = event.get("EventInSiteURL")
    expected = f"https://{client}.legistar.com/"
    return url if isinstance(url, str) and url.startswith(expected) else f"{expected}Calendar.aspx"


def _item_label(item: dict[str, Any], item_id: int) -> str:
    """Agenda number, file, title, and recorded result of one item, for the dataset name."""
    title = " ".join(str(item.get("EventItemTitle") or "").split())
    if len(title) > 160:
        title = title[:160] + "..."
    return (
        f"agenda item {item.get('EventItemAgendaNumber') or item_id} "
        f"(EventItemId {item_id}, file {item.get('EventItemMatterFile')}): {title}; "
        f"action {item.get('EventItemActionName')}, result {item.get('EventItemPassedFlagName')}"
    )


@dataclass
class _Cell:
    parts: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        """The cell's text, one entry per line break, whitespace collapsed."""
        text = "".join(self.parts)
        return [" ".join(line.split()) for line in text.split("\n") if line.strip()]


class _CalendarGrid(HTMLParser):
    """The header labels and meeting rows of a Legistar calendar page.

    Only the grid table itself is read. Its pager sits in nested tables, whose cells are
    never taken for meeting rows.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.year: str | None = None
        self.body: str | None = None
        self.first_body: str | None = None
        self.headers: list[str] = []
        self.rows: list[list[_Cell]] = []
        self.has_pager = False
        self._depth = 0
        self._header: list[str] | None = None
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._body_menu = False
        self._first_body: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("id") == _YEAR_INPUT_ID:
            self.year = attributes.get("value")
        if tag == "input" and attributes.get("id") == _BODY_INPUT_ID:
            self.body = attributes.get("value")
        if tag == "div" and attributes.get("id") == _BODY_MENU_ID:
            self._body_menu = True
        if tag == "li" and self._body_menu and self.first_body is None:
            self._first_body = []
        if tag == "table":
            if self._depth:
                self._depth += 1
            elif attributes.get("id") == _GRID_ID:
                self._depth = 1
            return
        if not self._depth:
            return
        classes = (attributes.get("class") or "").split()
        if tag == "tr" and "rgPager" in classes:
            self.has_pager = True
        if self._depth != 1:
            return
        if tag == "tr" and ("rgRow" in classes or "rgAltRow" in classes):
            if self._row is not None:
                # An unclosed row would otherwise vanish without a trace.
                raise SourceError("A Legistar calendar row was never closed")
            self._row = []
        elif tag == "th" and "rgHeader" in classes:
            self._header = []
        elif tag == "td" and self._row is not None:
            self._cell = _Cell()
        elif tag == "a" and self._cell is not None and attributes.get("href"):
            self._cell.links.append(str(attributes["href"]))
        elif tag == "br" and self._cell is not None:
            self._cell.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._first_body is not None:
            self.first_body = " ".join("".join(self._first_body).split())
            self._first_body = None
            self._body_menu = False
        if not self._depth:
            return
        if tag == "table":
            self._depth -= 1
            return
        if self._depth != 1:
            return
        if tag == "th" and self._header is not None:
            self.headers.append(" ".join("".join(self._header).split()))
            self._header = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._first_body is not None:
            self._first_body.append(data)
        if self._depth != 1:
            return
        if self._header is not None:
            self._header.append(data)
        elif self._cell is not None:
            self._cell.parts.append(data)


@dataclass(frozen=True)
class _CalendarPage:
    meetings: list[tuple[date, dict[str, Any]]]
    may_continue: bool
    """True when the view may hold more meetings than this page shows."""
    unread_links: int


def _calendar_date(text: str) -> date:
    match = _CALENDAR_DATE.match(text)
    if not match:
        raise SourceError("The Legistar calendar page has a meeting date in an unknown form")
    month, day, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise SourceError("The Legistar calendar page has an impossible meeting date") from exc


def _calendar_link(client: str, cell: _Cell | None, path: str) -> tuple[str | None, int]:
    """The cell's first link to path on the client's own site, and how many it refused."""
    if cell is None:
        return None, 0
    host = f"{client}.legistar.com"
    refused = 0
    for href in cell.links:
        url = urljoin(f"https://{host}/", href)
        parts = urlsplit(url)
        if parts.scheme == "https" and parts.netloc == host and parts.path == path:
            return url, refused
        if not href.startswith(("javascript:", "#")):
            refused += 1
    return None, refused


def _parse_calendar(client: str, html: str, year: str, *, descending: bool) -> _CalendarPage:
    grid = _CalendarGrid()
    grid.feed(html)
    grid.close()
    if grid.year != year:
        # The year arrives by cookie; a page that ignored it would show another span.
        raise SourceError("The Legistar calendar page did not show the requested year")
    if grid.body is None or grid.body != grid.first_body:
        # So does the body; a page showing one body would pass for the whole calendar.
        raise SourceError("The Legistar calendar page did not show every body")
    column = {label: index for index, label in enumerate(grid.headers) if label}
    if "Name" not in column or "Meeting Date" not in column:
        raise SourceError("The Legistar calendar page has no Name or Meeting Date column")
    meetings: list[tuple[date, dict[str, Any]]] = []
    unread = 0
    for cells in grid.rows:
        if len(cells) != len(grid.headers):
            raise SourceError("A Legistar calendar row does not match the page's columns")

        def cell(label: str, cells: list[_Cell] = cells) -> _Cell | None:
            return cells[column[label]] if label in column else None

        day = _calendar_date(" ".join(cells[column["Meeting Date"]].lines()))
        detail, refused_detail = _calendar_link(
            client, cell("Meeting Details"), "/MeetingDetail.aspx"
        )
        agenda, refused_agenda = _calendar_link(client, cell("Agenda"), "/View.ashx")
        minutes, refused_minutes = _calendar_link(client, cell("Minutes"), "/View.ashx")
        unread += refused_detail + refused_agenda + refused_minutes
        time_cell, place_cell = cell("Meeting Time"), cell("Meeting Location")
        meetings.append(
            (
                day,
                {
                    "EventBodyName": " ".join(cells[column["Name"]].lines()) or None,
                    "EventDate": day.isoformat(),
                    "EventTime": " ".join(time_cell.lines()) or None if time_cell else None,
                    "EventLocation": "; ".join(place_cell.lines()) or None if place_cell else None,
                    "EventInSiteURL": detail,
                    "EventAgendaFile": agenda,
                    "EventMinutesFile": minutes,
                },
            )
        )
    days = [day for day, _ in meetings]
    if days != sorted(days, reverse=descending):
        # The order arrives by cookie too; the coverage reasoning depends on it.
        raise SourceError("The Legistar calendar page did not sort meetings as requested")
    return _CalendarPage(
        meetings=meetings,
        # A full page counts as possibly continued even when no pager row was found.
        may_continue=grid.has_pager or len(meetings) >= CALENDAR_PAGE_ROWS,
        unread_links=unread,
    )


class Legistar:
    """Read meetings, agenda items, votes, and legislation from the Legistar Web API."""

    host = LEGISTAR_HOST

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        tokens: Mapping[str, str] | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._http = FixedHostClient(source_name=SOURCE_NAME, client=client)
        self._today = today
        self._calendar_lock = asyncio.Lock()
        self._tokens: dict[str, str] = {}
        for name, token in (tokens or {}).items():
            if not _CLIENT.match(name) or not _TOKEN.match(token):
                raise ValueError("A Legistar token needs a valid client name and token text")
            self._tokens[name] = token

    def _client_warnings(self, client: str) -> list[str]:
        """Caveats that depend on the client rather than on the result."""
        return [TOKEN_LINK_WARNING] if client in self._tokens else []

    async def _get(
        self, client: str, path: str, params: dict[str, str], *, by_id: bool = False
    ) -> tuple[Any, str]:
        """Fetch one Legistar resource. Returns the parsed body and the public URL."""
        url = f"{_BASE}/{client}/{path}"
        # The public link never carries the token, so it can be cited and frozen.
        public_url = f"{url}?{urlencode(params)}" if params else url
        token = self._tokens.get(client)
        sent = {**params, "token": token} if token else params
        status, content = await self._http.send("GET", url, params=sent or None)
        try:
            payload = json.loads(content) if content else None
        except ValueError:
            payload = None
        if status == 200:
            if payload is None:
                raise SourceError("The Legistar Web API returned a body that is not JSON")
            return payload, public_url
        exception = payload.get("ExceptionMessage") if isinstance(payload, dict) else None
        if status == 500 and isinstance(exception, str) and _UNKNOWN_CLIENT in exception:
            raise SourceQueryError(
                f"Unknown Legistar client {client}",
                code="UNKNOWN_CLIENT",
                argument="client",
                http_status=status,
                public_message=(
                    f"Legistar has no active client named {client}. Use the first part of "
                    "the jurisdiction's own Legistar address, such as oakland for "
                    "oakland.legistar.com. Some cities have moved to other systems."
                ),
            )
        if status in {401, 403} and token:
            raise SourceQueryError(
                f"Legistar client {client} refused this server's token (HTTP {status})",
                code="TOKEN_REJECTED",
                argument="client",
                http_status=status,
                public_message=(
                    f"The Legistar client {client} refused the token this server holds. "
                    "Use the jurisdiction's Legistar website until the token is renewed."
                ),
            )
        if status in {401, 403}:
            # checked 2026-11-20: nyc and phila returned HTTP 403 without a token.
            raise SourceQueryError(
                f"Legistar client {client} refused the request (HTTP {status})",
                code="TOKEN_REQUIRED",
                argument="client",
                http_status=status,
                public_message=(
                    f"The Legistar client {client} requires its own API token, which this "
                    "server does not hold. Use the jurisdiction's Legistar website instead."
                ),
            )
        if status == 404 and by_id and payload is not None:
            # checked 2026-11-20: an unknown meeting ID returned a JSON 404; an overlong
            # query returned an HTML 404, which is not about any record.
            raise SourceQueryError(
                f"Legistar returned 404 for {path}",
                code="NOT_FOUND",
                http_status=status,
                public_message=(
                    "Legistar has no public record with that ID for this client. Use an ID "
                    "that find_meetings or get_meeting_items returned for the same client."
                ),
            )
        text = content[:300].decode("utf-8", "replace") if payload is None else ""
        if status == 400 and _CLIENT_SETTING in text:
            reason = " ".join(text.split())
            raise SourceQueryError(
                f"Legistar client {client} is misconfigured for {path}: {reason}",
                code="CLIENT_MISCONFIGURED",
                argument="client",
                http_status=status,
                public_message=(
                    f"The Legistar API for {client} cannot answer this request because of a "
                    f"setting on the jurisdiction's side. Legistar says: {reason} Use "
                    f"https://{client}.legistar.com instead, and ask the clerk's office to "
                    "fix the API setting."
                ),
            )
        message = payload.get("Message") if isinstance(payload, dict) else None
        raise SourceQueryError(
            f"Legistar HTTP {status}: {message}",
            code="QUERY_REJECTED" if status == 400 else "SOURCE_UNAVAILABLE",
            http_status=status,
            public_message=(
                f"The Legistar Web API could not complete the request for {client} (HTTP {status})."
            ),
        )

    async def _list(
        self, client: str, path: str, params: dict[str, str], *, by_id: bool = False
    ) -> tuple[list, str]:
        payload, public_url = await self._get(client, path, params, by_id=by_id)
        if not isinstance(payload, list):
            raise SourceError("The Legistar Web API returned something other than a list")
        return payload, public_url

    def _result(
        self,
        *,
        client: str,
        kind: str,
        name: str,
        source_url: str,
        public_url: str,
        rows: list[dict[str, Any]],
        completeness: Completeness,
        fields: tuple[str, ...],
    ) -> SourceQueryResult:
        return SourceQueryResult(
            dataset_id=f"{client}/{kind}",
            dataset_name=name,
            domain=LEGISTAR_HOST,
            source_url=source_url,
            query=public_url.removeprefix(f"https://{LEGISTAR_HOST}"),
            query_url=public_url,
            rows=rows,
            returned_row_count=len(rows),
            completeness=completeness,
            schema_sha256=_schema_hash(kind, fields),
        )

    def find_meetings(
        self,
        *,
        client: str,
        start_date: str,
        end_date: str,
        body: str | None = None,
        limit: int = 50,
    ) -> PreparedLookup:
        name = validate_client(client)
        first = _date("start_date", start_date)
        last = _date("end_date", end_date)
        if last < first:
            raise _invalid("end_date", "end_date must be on or after start_date.")
        count = _limit(limit)
        body_text = _odata_text("body", body) if body is not None else None
        conditions = [
            f"EventDate ge {_datetime_literal(first)}",
            f"EventDate lt {_datetime_literal(last + timedelta(days=1))}",
        ]
        if body_text is not None:
            conditions.append(f"substringof('{body_text}',EventBodyName)")
        params = _bounded(
            {
                "$filter": " and ".join(conditions),
                "$orderby": "EventDate desc,EventId desc",
                "$top": str(count + 1),
                "$select": ",".join(MEETING_FIELDS),
            }
        )

        async def run() -> Lookup:
            try:
                records, public_url = await self._list(name, "events", params)
            except SourceQueryError as error:
                if error.code != "CLIENT_MISCONFIGURED":
                    raise
                return await self._calendar_meetings(
                    name, first, last, body=body, count=count, api_error=error
                )
            rows = []
            for record in records[:count]:
                row = _project(record, MEETING_FIELDS)
                if _meeting_url(name, row) != row["EventInSiteURL"]:
                    row["EventInSiteURL"] = None
                rows.append(row)
            result = self._result(
                client=name,
                kind="events",
                name=f"Legistar meetings, client {name}",
                source_url=f"https://{name}.legistar.com/Calendar.aspx",
                public_url=public_url,
                rows=rows,
                completeness=(
                    Completeness.TRUNCATED if len(records) > count else Completeness.COMPLETE
                ),
                fields=MEETING_FIELDS,
            )
            return Lookup(
                result=result,
                row_grain=MEETING_GRAIN,
                warnings=[CLERK_EDITS_WARNING, PUBLIC_ONLY_WARNING, LOCAL_TIME_WARNING]
                + self._client_warnings(name),
                rules=[LEGISTAR_RULE],
            )

        return PreparedLookup(
            host=LEGISTAR_HOST,
            arguments={
                "client": name,
                "start_date": first.isoformat(),
                "end_date": last.isoformat(),
                "body": body,
                "limit": count,
            },
            run=run,
        )

    async def _calendar_meetings(
        self,
        client: str,
        first: date,
        last: date,
        *,
        body: str | None,
        count: int,
        api_error: SourceQueryError,
    ) -> Lookup:
        """Meetings from the client's public calendar page, for an API that cannot list them.

        Each year is read newest first and, when that view does not reach back far enough,
        oldest first. Dates neither view reached are named, and the result is then partial.
        """
        # The year menu lists each year to the current one, then Next Year; later dates
        # go unread. The year comes from the server's clock, so near New Year a menu that
        # has not turned over makes the page refuse the year and the API's error stands.
        next_year = self._today().year + 1
        years = [year for year in range(last.year, first.year - 1, -1) if year <= next_year]
        if not years:
            raise api_error
        if len(years) > MAX_CALENDAR_YEARS:
            raise _invalid(
                "start_date",
                f"The Legistar API for {client} cannot list meetings, so this tool reads its "
                f"calendar page, at most {MAX_CALENDAR_YEARS} calendar years per request. "
                "Give dates within two calendar years.",
            )
        page_url = f"https://{client}.legistar.com/Calendar.aspx"
        wanted = " ".join(body.split()).casefold() if body is not None else None
        found: dict[Any, tuple[date, dict[str, Any]]] = {}
        gaps: list[tuple[date, date]] = []
        if last.year > next_year:
            gaps.append((date(next_year + 1, 1, 1), last))
        views: list[str] = []
        unread = 0
        try:
            # One calendar read at a time per server process keeps a no-login public
            # instance from loading a city's site with parallel page requests.
            async with asyncio.timeout(CALENDAR_TIMEOUT_SECONDS), self._calendar_lock:
                prefix = await self._calendar_prefix(page_url)
                for year in years:
                    label = NEXT_YEAR if year == next_year else str(year)
                    start = max(first, date(year, 1, 1))
                    end = min(last, date(year, 12, 31))
                    newest_first = await self._calendar_page(client, page_url, prefix, label, True)
                    views.append(f"{label} newest first")
                    pages = [newest_first]
                    if newest_first.may_continue and newest_first.meetings:
                        oldest = newest_first.meetings[-1][0]
                        # Meetings on the oldest date shown may continue on the next page.
                        if oldest >= start:
                            oldest_first = await self._calendar_page(
                                client, page_url, prefix, label, False
                            )
                            views.append(f"{label} oldest first")
                            pages.append(oldest_first)
                            if oldest_first.may_continue and oldest_first.meetings:
                                newest = oldest_first.meetings[-1][0]
                                gap = (max(start, newest), min(end, oldest))
                                if newest <= oldest and gap[0] <= gap[1]:
                                    gaps.append(gap)
                    for page in pages:
                        unread += page.unread_links
                        for day, row in page.meetings:
                            body_name = str(row["EventBodyName"] or "").casefold()
                            if not start <= day <= end or (wanted and wanted not in body_name):
                                continue
                            key = row["EventInSiteURL"] or tuple(row.values())
                            found.setdefault(key, (day, row))
        except (SourceError, TimeoutError) as error:
            raise api_error from error
        gaps.sort()
        ordered = sorted(found.values(), key=lambda pair: pair[0], reverse=True)
        rows = [row for _, row in ordered[:count]]
        result = SourceQueryResult(
            dataset_id=f"{client}/calendar",
            dataset_name=f"Legistar website calendar, client {client}",
            domain=f"{client}.legistar.com",
            source_url=page_url,
            query=f"Calendar.aspx year views: {', '.join(views)}",
            query_url=page_url,
            rows=rows,
            returned_row_count=len(rows),
            completeness=(
                Completeness.TRUNCATED if gaps or len(ordered) > count else Completeness.COMPLETE
            ),
            schema_sha256=_schema_hash("calendar", CALENDAR_FIELDS),
        )
        warnings = [
            calendar_warning(client),
            CALENDAR_MINUTES_WARNING,
            PUBLIC_ONLY_WARNING,
            CALENDAR_PAGE_WARNING,
        ]
        rules = [calendar_rule(client)]
        escalations = ["LEGISTAR_WEB_CALENDAR"]
        if gaps:
            warnings.insert(0, calendar_gap_warning(gaps))
            rules.insert(0, calendar_gap_rule(gaps))
            escalations.append("LEGISTAR_CALENDAR_GAP")
        if unread:
            warnings.append(UNREAD_LINK_WARNING)
        return Lookup(
            result=result,
            row_grain=CALENDAR_GRAIN,
            warnings=warnings,
            rules=rules,
            escalations=escalations,
        )

    async def _calendar_prefix(self, page_url: str) -> str:
        """The site's settings number, which its calendar cookies carry in their names."""
        status, headers, _ = await self._http.send_with_headers(
            "GET", page_url, headers={"Accept": "text/html"}
        )
        if status != 200:
            raise SourceError(f"The Legistar calendar page answered HTTP {status}")
        for value in headers.get_list("set-cookie"):
            match = _SETTING_PREFIX.match(value)
            if match:
                return match.group(1)
        raise SourceError("The Legistar calendar page set no calendar cookie")

    async def _calendar_page(
        self, client: str, page_url: str, prefix: str, year: str, descending: bool
    ) -> _CalendarPage:
        order = "DESC" if descending else "ASC"
        cookie = (
            f"Setting-{prefix}-Calendar Year={year}; Setting-{prefix}-Calendar Body=All; "
            f"Setting-{prefix}-ASP.calendar_aspx.gridCalendar.SortExpression="
            f"MeetingStartDate {order}"
        )
        status, content = await self._http.send(
            "GET", page_url, headers={"Accept": "text/html", "Cookie": cookie}
        )
        if status != 200:
            raise SourceError(f"The Legistar calendar page answered HTTP {status}")
        return _parse_calendar(
            client, content.decode("utf-8", "replace"), year, descending=descending
        )

    def get_meeting_items(self, *, client: str, event_id: int) -> PreparedLookup:
        name = validate_client(client)
        meeting = _positive_id("event_id", event_id)
        params = {"AgendaNote": "1", "MinutesNote": "1", "$top": str(MAX_REPLY_ROWS)}

        async def run() -> Lookup:
            event, _ = await self._get(name, f"events/{meeting}", {}, by_id=True)
            if not isinstance(event, dict):
                raise SourceError("The Legistar Web API returned a malformed meeting")
            records, public_url = await self._list(
                name, f"events/{meeting}/eventitems", params, by_id=True
            )
            rows = [_project(record, ITEM_FIELDS) for record in records]
            result = self._result(
                client=name,
                kind="eventitems",
                name=f"Legistar agenda items, client {name}: {_meeting_label(event)}",
                source_url=_meeting_url(name, event),
                public_url=public_url,
                rows=rows,
                completeness=(
                    Completeness.TRUNCATED
                    if len(records) >= MAX_REPLY_ROWS
                    else Completeness.COMPLETE
                ),
                fields=ITEM_FIELDS,
            )
            return Lookup(
                result=result,
                row_grain=ITEM_GRAIN,
                warnings=[
                    EMPTY_RESULT_WARNING,
                    CLERK_EDITS_WARNING,
                    PUBLIC_ONLY_WARNING,
                    LOCAL_TIME_WARNING,
                    *self._client_warnings(name),
                ],
                rules=[LEGISTAR_RULE],
            )

        return PreparedLookup(
            host=LEGISTAR_HOST,
            arguments={"client": name, "event_id": meeting},
            run=run,
        )

    def get_item_votes(self, *, client: str, event_id: int, event_item_id: int) -> PreparedLookup:
        name = validate_client(client)
        meeting = _positive_id("event_id", event_id)
        item_id = _positive_id("event_item_id", event_item_id)

        async def run() -> Lookup:
            event, _ = await self._get(name, f"events/{meeting}", {}, by_id=True)
            item, _ = await self._get(
                name, f"events/{meeting}/eventitems/{item_id}", {}, by_id=True
            )
            if not isinstance(event, dict) or not isinstance(item, dict):
                raise SourceError("The Legistar Web API returned a malformed record")
            if item.get("EventItemEventId") != meeting:
                # checked 2026-11-20: Legistar returned item 240300 of meeting 9617 when
                # asked for it under meeting 9609.
                raise SourceQueryError(
                    f"Item {item_id} belongs to another meeting",
                    code="NOT_FOUND",
                    argument="event_item_id",
                    public_message=(
                        f"Agenda item {item_id} is not part of meeting {meeting}. Use an "
                        "EventItemId that get_meeting_items returned for this meeting."
                    ),
                )
            records, public_url = await self._list(
                name, f"eventitems/{item_id}/votes", {}, by_id=True
            )
            rows = [_project(record, VOTE_FIELDS) for record in records]
            result = self._result(
                client=name,
                kind="votes",
                name=(
                    f"Legistar votes, client {name}: {_item_label(item, item_id)}; "
                    f"{_meeting_label(event)}"
                ),
                source_url=_meeting_url(name, event),
                public_url=public_url,
                rows=rows,
                completeness=(
                    Completeness.TRUNCATED
                    if len(records) >= MAX_REPLY_ROWS
                    else Completeness.COMPLETE
                ),
                fields=VOTE_FIELDS,
            )
            return Lookup(
                result=result,
                row_grain=VOTE_GRAIN,
                warnings=[
                    VOTE_RECORD_WARNING,
                    CLERK_EDITS_WARNING,
                    PUBLIC_ONLY_WARNING,
                    *self._client_warnings(name),
                ],
                rules=([NO_VOTES_RULE] if not rows else []) + [VOTE_RULE, LEGISTAR_RULE],
                escalations=["NO_VOTES_RECORDED"] if not rows else [],
            )

        return PreparedLookup(
            host=LEGISTAR_HOST,
            arguments={"client": name, "event_id": meeting, "event_item_id": item_id},
            run=run,
        )

    def search_legislation(
        self,
        *,
        client: str,
        text: str,
        introduced_from: str | None = None,
        introduced_to: str | None = None,
        limit: int = 50,
    ) -> PreparedLookup:
        name = validate_client(client)
        literal = _odata_text("text", text)
        first = _date("introduced_from", introduced_from) if introduced_from else None
        last = _date("introduced_to", introduced_to) if introduced_to else None
        if first and last and last < first:
            raise _invalid("introduced_to", "introduced_to must be on or after introduced_from.")
        count = _limit(limit)
        conditions = [
            f"(substringof('{literal}',MatterTitle) or substringof('{literal}',MatterName) "
            f"or MatterFile eq '{literal}')"
        ]
        if first:
            conditions.append(f"MatterIntroDate ge {_datetime_literal(first)}")
        if last:
            conditions.append(f"MatterIntroDate lt {_datetime_literal(last + timedelta(days=1))}")
        params = _bounded(
            {
                "$filter": " and ".join(conditions),
                "$orderby": "MatterIntroDate desc,MatterId desc",
                "$top": str(count + 1),
                "$select": ",".join((*MATTER_FIELDS, "MatterGuid")),
            }
        )

        async def run() -> Lookup:
            check = asyncio.create_task(self._newest_matter(name))
            try:
                records, public_url = await self._list(name, "matters", params)
            except BaseException:
                check.cancel()
                raise
            # Bounded by the check's own timeout, counted from when both started.
            newest = await check
            rows = []
            for record in records[:count]:
                row = _project(record, MATTER_FIELDS)
                guid = record.get("MatterGuid")
                row["LegislationDetailURL"] = (
                    f"https://{name}.legistar.com/LegislationDetail.aspx?"
                    + urlencode({"ID": row["MatterId"], "GUID": guid})
                    if row["MatterId"] is not None and isinstance(guid, str)
                    else None
                )
                # checked 2026-11-20: this pattern returned HTTP 200 for Oakland matter
                # 37760 and Seattle matter 1785.
                rows.append(row)
            result = self._result(
                client=name,
                kind="matters",
                name=f"Legistar legislation, client {name}",
                source_url=f"https://{name}.legistar.com/Legislation.aspx",
                public_url=public_url,
                rows=rows,
                completeness=(
                    Completeness.TRUNCATED if len(records) > count else Completeness.COMPLETE
                ),
                fields=(*MATTER_FIELDS, "LegislationDetailURL"),
            )
            warnings = [SUBSTRING_WARNING, CLERK_EDITS_WARNING, PUBLIC_ONLY_WARNING]
            if first or last:
                warnings.insert(1, NO_DATE_WARNING)
            warnings.extend(self._client_warnings(name))
            rules = [LEGISTAR_RULE]
            escalations: list[str] = []
            if newest is not None and newest < self._today() - STALE_AFTER:
                warnings.insert(0, stale_warning(name, newest))
                rules.insert(0, stale_rule(name, newest))
                escalations.append("LEGISTAR_API_STALE")
            return Lookup(
                result=result,
                row_grain=MATTER_GRAIN,
                warnings=warnings,
                rules=rules,
                escalations=escalations,
            )

        return PreparedLookup(
            host=LEGISTAR_HOST,
            arguments={
                "client": name,
                "text": text,
                "introduced_from": first.isoformat() if first else None,
                "introduced_to": last.isoformat() if last else None,
                "limit": count,
            },
            run=run,
        )

    async def _newest_matter(self, client: str) -> date | None:
        """The introduction date of the newest matter the API holds, or None if unknown.

        The date only adds a caveat, so its failure must not sink a search that already
        succeeded.
        """
        # A mis-keyed future date would otherwise hide a stopped copy.
        tomorrow = self._today() + timedelta(days=1)
        params = {
            "$filter": f"MatterIntroDate lt {_datetime_literal(tomorrow)}",
            "$top": "1",
            "$orderby": "MatterIntroDate desc",
            "$select": "MatterIntroDate",
        }
        try:
            async with asyncio.timeout(NEWEST_MATTER_TIMEOUT_SECONDS):
                records, _ = await self._list(client, "matters", params)
        except (SourceError, TimeoutError):
            return None
        value = (
            records[0].get("MatterIntroDate") if records and isinstance(records[0], dict) else None
        )
        if not isinstance(value, str) or not _DATE.match(value[:10]):
            return None
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None

    async def close(self) -> None:
        await self._http.close()

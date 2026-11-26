"""Granicus Legistar Web API: city and county meetings, agenda items, votes, legislation.

Most Legistar clients answer without a key. Every request goes to one fixed host; the
caller names only a client, which is checked against a strict pattern before it becomes
a path segment.

When a client's API refuses a request because of a setting on the jurisdiction's side, or
its copy of the legislation has stopped updating, the tools read the same client's public
pages instead: the calendar, the legislation search, and the pages of one matter, one
meeting, and one action. Those pages live at <client>.legistar.com, a host the Web API has
just confirmed by answering for that client. legistar_pages.py holds the page parsing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from opencivicquery.models import Completeness, SourceQueryResult
from opencivicquery.sources import legistar_pages as pages
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

PAGE_TIMEOUT_SECONDS = 25.0
CALL_BUDGET_SECONDS = 50.0
# Page reading follows one or two API requests, each with its own timeout. The call
# budget ends page reading before the service's 60-second deadline even when those ran
# long, so a finished API answer or a clear error is what the caller gets.
MAX_DETAIL_PAGES = 10
# checked 2026-11-26: a sfgov legislation search answered in 0.4 to 1.0 seconds with 370
# to 620 KB, a matter page in 0.3 seconds with 109 KB, a Board meeting page of 88 items in
# 0.8 seconds with 541 KB, and an action page in 0.3 seconds with 56 KB. A search costs
# three or four requests before any matter page, so ten matter pages keep one call near
# fourteen requests. The site sent no robots.txt.

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
    "EventId",
    "EventGuid",
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
        "and minutes files. For a meeting's agenda items and member votes, pass its EventId "
        "as event_id and its EventGuid as event_guid to get_meeting_items and "
        "get_item_votes, which read the meeting's own page."
    )


def calendar_rule(client: str) -> str:
    return (
        f"WEBSITE CALENDAR. Say that these meetings come from the Legistar calendar page of "
        f"{client} because its API cannot list meetings. Cite each meeting by its "
        "EventInSiteURL."
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


WEB_MATTER_FIELDS = (
    "MatterId",
    "MatterFile",
    "MatterVersion",
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
    "Sponsors",
    "History",
    "DetailPageRead",
    "LegislationDetailURL",
)
WEB_ITEM_FIELDS = (
    "EventItemId",
    "EventItemAgendaNumber",
    "EventItemMatterFile",
    "EventItemVersion",
    "EventItemMatterName",
    "EventItemMatterType",
    "EventItemMatterStatus",
    "EventItemTitle",
    "EventItemActionName",
    "EventItemPassedFlagName",
    "EventItemMatterId",
    "LegislationDetailURL",
    "ActionDetailURL",
)
WEB_VOTE_FIELDS = ("VotePersonName", "VoteValueName")
# checked 2026-11-26: the sfgov search grid sorts its File #, Introduced, and Final Action
# columns on MatterLegacyFile, MatterIntroDate, and MatterPassedDate, and the matter page
# labels its fields File #, Version, Name, Type, Status, Introduced, In control, On agenda,
# Final action, Enactment date, Enactment #, Title, and Sponsors. The rows keep the API's
# names for those.

WEB_MATTER_GRAIN = (
    "One Legistar matter, as a row of the jurisdiction's public legislation search page. "
    "Where DetailPageRead is true, the matter's own page filled in its sponsors and other "
    "details."
)
WEB_ITEM_GRAIN = (
    "One action the clerk recorded on one legislative file at one meeting, as a row of the "
    "meeting's public Legistar page. A file with two actions at the meeting fills two rows."
)
# checked 2026-11-26: the sfgov Rules Committee meeting of 2026-09-14 listed 7 rows for 5
# files; files 260862 and 260705 each had one row per version and action.
WEB_VOTE_GRAIN = (
    "One member's recorded vote on one action, as a row of the action's public Legistar page."
)

SETTING_REASON = "cannot answer this request because of a setting on the jurisdiction's side"


def stale_reason(newest: date) -> str:
    return f"holds no matter introduced after {newest.isoformat()}"


def web_warning(client: str, reason: str) -> str:
    return (
        f"The Legistar API copy for {client} {reason}, so this answer comes from the "
        f"jurisdiction's public Legistar pages at https://{client}.legistar.com. The clerk's "
        "office can change these pages after this retrieval."
    )


def web_rule(client: str, reason: str) -> str:
    return (
        f"WEBSITE PAGES. Say that this comes from the public Legistar pages of {client} "
        f"because its API copy {reason}. Cite each matter by its LegislationDetailURL, each "
        "action by its ActionDetailURL, and each meeting by its meeting page."
    )


WEB_SEARCH_WARNING = (
    "How the page's search matches text is not documented. On 2026-11-26 San Francisco's "
    "page matched whole words of a matter's full title, and matched a file or enactment "
    "number anywhere inside it. This tool searches the title and, when the text holds a "
    "digit, the file or enactment number too. Read each title before counting it as "
    "relevant, and say the count is of matters the page's search returned."
)
# checked 2026-11-26: a sfgov title search for 'rent' over matters introduced 2026-11-01
# to 2026-11-26 returned 4 matters and none through a longer word such as 'current'; a
# search for 'housing' also returned titles that hold 'Housing'. A File/enactment # search
# for 260875 set the cookie "Search-File=%260875%" and returned that one file.

WEB_TITLE_CUT_WARNING = (
    "The search page cuts a title at about 600 characters, so a word the search matched can "
    "sit in the part that is cut. MatterTitle is the full title only where DetailPageRead is "
    "true."
)
# checked 2026-11-26: 3 of 22 sfgov matches for 'housing' showed titles of about 600
# characters without the word.

WEB_SEARCH_LINK_WARNING = (
    "query_url opens the search form, which shows no result until the criteria in query are "
    "entered under Advanced search."
)

WEB_ITEM_ROWS_WARNING = (
    "Each row is one action the clerk recorded on one file, so a file with two actions "
    "fills two rows. An empty action or result does not show that nothing happened: many "
    "items are only scheduled or heard. Call get_item_votes with the same event_guid for "
    "any row with a result, and check the minutes file."
)
WEB_ITEMS_WARNING = (
    "The meeting page says it shows legislation only. Procedural items such as roll call "
    f"and public comment are in the agenda file. {WEB_ITEM_ROWS_WARNING}"
)


def items_view_warning(view: str | None) -> str:
    """What the list leaves out, as far as the page's own view label says."""
    if view == pages.LEGISLATION_VIEW:
        return WEB_ITEMS_WARNING
    shown = f"reads {view}" if view else "is not stated"
    return (
        f"The meeting page's view {shown}, so this tool cannot say which agenda items the "
        f"list leaves out. Check the agenda file. {WEB_ITEM_ROWS_WARNING}"
    )


WEB_ITEM_COUNT_RULE = (
    "Count files by distinct EventItemMatterFile, and say so when a figure counts recorded "
    "actions instead."
)
# checked 2026-11-26: the sfgov meeting page's view menu read "Show: Legislation only",
# with "All agenda items" as its other choice. The Rules Committee meeting of 2026-09-14
# listed 7 records and the Board meeting of 2026-09-29 listed 88 on one page without a
# pager; 35 of the 88 had no action and no result.

WEB_MINUTES_RULE = (
    "Give the meeting's minutes status, which dataset_name states, beside any action or "
    "vote you report."
)

PARTIAL_PAGE_WARNING = (
    "The page showed only part of this list, and this tool read the part it showed. Open "
    "query_url for the rest."
)


def detail_warning(read: int, listed: int) -> str:
    return (
        "Sponsors, MatterName, MatterBodyName, MatterAgendaDate, and the enactment fields "
        f"come from each matter's own page. This call read that page for {read} of the "
        f"{listed} matters listed, marked DetailPageRead true. For the others these fields "
        "are empty because their pages were not read. Search for one file number to read "
        "that matter's page and its action history."
    )


DETAIL_RULE = (
    "UNREAD MATTER PAGES. Where DetailPageRead is false, say the sponsors were not read, "
    "and never say the matter has no sponsor."
)
WEB_SPONSOR_RULE = (
    "Sponsors holds the names on the matter's own page. Where DetailPageRead is true and "
    "Sponsors is empty or null, say that the matter's page names no sponsor."
)
SITE_UNREAD_WARNING = (
    "This server tried to read the jurisdiction's current Legistar pages and could not just "
    "now, so this answer is the API's older copy. Try again in a minute for current matters."
)
HISTORY_GAP_RULE = (
    "PARTIAL HISTORY. Say that the action history listed for the matter is partial, and "
    "never present it as the matter's full history."
)


def search_gap_warning(total: int, read: int, newest_first: bool) -> str:
    found = f"{total} or more" if total >= pages.SEARCH_MAX_RESULTS else str(total)
    order = (
        "highest matter ID first, which is about newest first"
        if newest_first
        else "in the page's own order"
    )
    return (
        f"The page found {found} matters for one search and shows {pages.SEARCH_PAGE_ROWS} "
        f"per view. This tool read the first {read}, {order}. Narrow the dates or the text "
        "to reach the rest."
    )


def search_gap_rule(total: int, read: int) -> str:
    found = f"{total} or more" if total >= pages.SEARCH_MAX_RESULTS else str(total)
    return (
        f"PARTIAL SEARCH. Say that the page found {found} matching matters and this list "
        f"holds only the first {read} it showed. Never present it as the full list."
    )


def search_dates_warning(first: date, last: date) -> str:
    return (
        f"The page's date field accepts {pages.EARLIEST_SEARCH_DATE.isoformat()} to "
        f"{pages.LATEST_SEARCH_DATE.isoformat()}. The search covered matters introduced from "
        f"{first.isoformat()} to {last.isoformat()}, both days included."
    )


def history_gap_warning(files: list[str]) -> str:
    return (
        f"The matter page showed only part of the action history of file {', '.join(files)}. "
        "The History list is partial; open LegislationDetailURL for the rest."
    )


def _pages_unread(client: str, reason: str, cause: BaseException) -> SourceQueryError:
    """The error for pages that were needed and could not be read. It invites a retry."""
    late = isinstance(cause, TimeoutError) or getattr(cause, "code", None) == "SOURCE_TIMEOUT"
    return SourceQueryError(
        f"Legistar pages of {client} could not be read ({type(cause).__name__})",
        code="SOURCE_TIMEOUT" if late else "SOURCE_UNAVAILABLE",
        argument="client",
        public_message=(
            f"The Legistar API copy for {client} {reason}, so this tool reads the "
            f"jurisdiction's pages at https://{client}.legistar.com, and they could not be "
            "read just now. The site may be slow, or this server may be reading it for "
            "another request. Try again in a minute, or open the site."
        ),
    )


def _page_deadline(started: float) -> float:
    """When page reading must end for a call that began at started, on the loop's clock."""
    now = asyncio.get_running_loop().time()
    return min(now + PAGE_TIMEOUT_SECONDS, started + CALL_BUDGET_SECONDS)


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


def _search_text(argument: str, text: str) -> str:
    """Return text with whitespace collapsed, refused when empty, long, or unprintable."""
    normalized = " ".join(text.split())
    if not 2 <= len(normalized) <= MAX_TEXT_LENGTH or any(
        unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in normalized
    ):
        raise _invalid(argument, f"Give {argument} as 2 to {MAX_TEXT_LENGTH} characters of text.")
    return normalized


def _odata_text(argument: str, text: str) -> str:
    """Return text as an OData string literal body, with single quotes doubled."""
    return _search_text(argument, text).replace("'", "''")


def _guid(argument: str, value: str) -> str:
    normalized = value.strip().upper()
    if not pages.GUID.match(normalized):
        raise _invalid(
            argument,
            f"Give {argument} as the GUID that Legistar shows with the ID, such as "
            "9A52EBCA-0730-4056-9FDE-A7AF7B764B17.",
        )
    return normalized


def _keep_cookies(jar: dict[str, str], headers: httpx.Headers) -> None:
    """Add the cookies one answer set to the cookies of the call that received it."""
    for value in headers.get_list("set-cookie"):
        name, found, content = value.split(";", 1)[0].partition("=")
        name, content = name.strip(), content.strip()
        printable = all(32 <= ord(c) < 127 and c != ";" for c in name + content)
        fits = len(name) + len(content) <= 1000 and (name in jar or len(jar) < 20)
        if found and name and printable and fits:
            jar[name] = content


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


def _web_meeting_label(meeting: int, header: dict[str, Any]) -> str:
    """Body, date, and minutes status of one meeting page, for the dataset name."""
    status = header["EventMinutesStatusName"] or "not stated on the page"
    return (
        f"{header['EventBodyName']}, meeting {meeting} on {header['EventDate']}, "
        f"minutes status {status}"
    )


def _web_action_label(item_id: int, header: dict[str, Any]) -> str:
    """File, name, action, and result of one action page, for the dataset name."""
    name = str(header["Name"] or "")
    if len(name) > 160:
        name = name[:160] + "..."
    return (
        f"EventItemId {item_id}, file {header['File']} version {header['Version']}: {name}; "
        f"action {header['Action']}, result {header['Result']}, mover {header['Mover']}, "
        f"seconder {header['Seconder']}"
    )


def _same_file(row: dict[str, Any], text: str) -> bool:
    return str(row["MatterFile"] or "").casefold() == text.casefold()


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
        try:
            url = urljoin(f"https://{host}/", href)
            parts = urlsplit(url)
        except ValueError:
            # A link the URL parser refuses counts as one that was left out.
            refused += 1
            continue
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
        meeting = pages.reference(client, detail, pages.MEETING_PATH)
        meetings.append(
            (
                day,
                {
                    "EventId": meeting[0] if meeting else None,
                    "EventGuid": meeting[1] if meeting else None,
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
        self._page_lock = asyncio.Lock()
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
            # One page-reading call at a time per server process keeps a no-login public
            # instance from loading a city's site with parallel page requests.
            async with asyncio.timeout(CALENDAR_TIMEOUT_SECONDS), self._page_lock:
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

    @asynccontextmanager
    async def _page_turn(self, deadline: float) -> AsyncIterator[None]:
        """Hold the one page-reading turn of this process, waiting no later than deadline.

        One page-reading call at a time per server process keeps a no-login public
        instance from loading a city's site with parallel page requests.
        """
        if asyncio.get_running_loop().time() >= deadline:
            # A budget already spent never starts a request to the site.
            raise TimeoutError
        async with asyncio.timeout_at(deadline):
            await self._page_lock.acquire()
        try:
            yield
        finally:
            self._page_lock.release()

    async def _page(
        self,
        client: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        jar: dict[str, str] | None = None,
        learn: bool = False,
    ) -> str:
        """Fetch one page of the client's own Legistar site and return its HTML.

        The host is always <client>.legistar.com and the path one of four page types; no
        part of the address comes from page content. jar holds the cookies this call has
        received from the site, and they travel back only to that site within the call.
        """
        if path not in pages.PAGE_PATHS:
            raise SourceError("That Legistar page is not one this server reads")
        headers = {"Accept": "text/html"}
        if jar:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in jar.items())
        status, answer, content = await self._http.send_with_headers(
            "POST" if form is not None else "GET",
            f"{pages.site(client)}/{path}",
            params=params,
            form_body=form,
            headers=headers,
        )
        if status != 200:
            # A redirect is never followed; it reads as a failure here.
            raise SourceError(f"The Legistar page {path} answered HTTP {status}")
        if learn and jar is not None:
            _keep_cookies(jar, answer)
        return content.decode("utf-8", "replace")

    async def _page_reason(
        self, client: str, error: SourceQueryError, guid: str | None
    ) -> str | None:
        """Why this client's meeting must be read from its pages, or None to keep the error.

        Pages stand in only for an API that cannot answer because of a client setting, or
        whose copy has stopped updating and so holds no record of the meeting.
        """
        if error.code == "CLIENT_MISCONFIGURED":
            if guid is None:
                raise SourceQueryError(
                    f"Legistar client {client} needs event_guid to read its meeting pages",
                    code="EVENT_GUID_REQUIRED",
                    argument="event_guid",
                    http_status=error.http_status,
                    public_message=(
                        f"The Legistar API for {client} cannot answer this request because "
                        "of a setting on the jurisdiction's side, so this tool reads the "
                        f"meeting's page on https://{client}.legistar.com, which needs the "
                        "meeting's GUID. Pass event_guid: the EventGuid that find_meetings "
                        "returned with this EventId, or the GUID in the meeting page's address."
                    ),
                ) from error
            return SETTING_REASON
        if error.code == "NOT_FOUND" and guid is not None:
            newest = await self._newest_matter(client)
            if newest is not None and newest < self._today() - STALE_AFTER:
                return stale_reason(newest)
        return None

    async def _meeting_page(
        self, client: str, meeting: int, guid: str, deadline: float
    ) -> tuple[pages.Meeting, str]:
        """One meeting's page and its address. The caller holds the page turn."""
        async with asyncio.timeout_at(deadline):
            html = await self._page(
                client, pages.MEETING_PATH, params={"ID": str(meeting), "GUID": guid}
            )
        try:
            parsed = pages.parse_meeting(client, html)
        except pages.PageNotFound as error:
            raise SourceQueryError(
                f"Legistar site of {client} has no meeting {meeting} with that GUID",
                code="NOT_FOUND",
                argument="event_guid",
                public_message=(
                    f"The Legistar site of {client} has no meeting page for event_id "
                    f"{meeting} with that event_guid. Use the EventId and EventGuid that "
                    "find_meetings returned together."
                ),
            ) from error
        return parsed, pages.page_url(client, pages.MEETING_PATH, meeting, guid)

    async def _web_meeting_items(
        self, client: str, meeting: int, guid: str, reason: str, started: float
    ) -> Lookup:
        """The actions on a meeting's legislative files, from its public page."""
        deadline = _page_deadline(started)
        try:
            async with self._page_turn(deadline):
                found, url = await self._meeting_page(client, meeting, guid, deadline)
        except SourceQueryError as error:
            if error.code == "NOT_FOUND":
                raise
            raise _pages_unread(client, reason, error) from error
        except (SourceError, TimeoutError) as error:
            raise _pages_unread(client, reason, error) from error
        rows = [{name: item[name] for name in WEB_ITEM_FIELDS} for item in found.items]
        result = SourceQueryResult(
            dataset_id=f"{client}/web-eventitems",
            dataset_name=(
                f"Legistar website agenda items, client {client}: "
                f"{_web_meeting_label(meeting, found.header)}"
            ),
            domain=f"{client}.legistar.com",
            source_url=url,
            query=(
                f"{pages.MEETING_PATH}?ID={meeting}&GUID={guid}, view: {found.view or 'not stated'}"
            ),
            query_url=url,
            rows=rows,
            returned_row_count=len(rows),
            completeness=Completeness.TRUNCATED if found.unread else Completeness.COMPLETE,
            schema_sha256=_schema_hash("web-eventitems", WEB_ITEM_FIELDS),
        )
        warnings = [
            web_warning(client, reason),
            items_view_warning(found.view),
            PUBLIC_ONLY_WARNING,
        ]
        if found.unread:
            warnings.insert(0, PARTIAL_PAGE_WARNING)
        return Lookup(
            result=result,
            row_grain=WEB_ITEM_GRAIN,
            warnings=warnings,
            rules=[web_rule(client, reason), WEB_ITEM_COUNT_RULE, WEB_MINUTES_RULE],
            escalations=["LEGISTAR_WEB_PAGES"],
        )

    async def _web_item_votes(
        self,
        client: str,
        meeting: int,
        guid: str,
        item_id: int,
        reason: str,
        started: float,
    ) -> Lookup:
        """Each member's vote on one action, from the action's public page.

        The meeting's page is read first: it shows that the item belongs to the meeting
        and gives the GUID of the item's action page.
        """
        deadline = _page_deadline(started)
        try:
            async with self._page_turn(deadline):
                found, _ = await self._meeting_page(client, meeting, guid, deadline)
                item_guid = found.item_guids.get(item_id)
                if item_guid is None and found.unread:
                    # The item may sit on the part of the page that was not shown.
                    raise SourceQueryError(
                        f"The page of meeting {meeting} was only partly shown",
                        code="SOURCE_UNAVAILABLE",
                        argument="event_item_id",
                        public_message=(
                            f"The page of meeting {meeting} showed only part of its list, "
                            f"and EventItemId {item_id} was not in that part, so this tool "
                            "cannot say whether the item belongs to the meeting. Open the "
                            "meeting page for the rest."
                        ),
                    )
                if item_guid is None:
                    raise SourceQueryError(
                        f"Item {item_id} is not on the page of meeting {meeting}",
                        code="NOT_FOUND",
                        argument="event_item_id",
                        public_message=(
                            f"The page of meeting {meeting} lists no legislative file with "
                            f"EventItemId {item_id}. Use an EventItemId that "
                            "get_meeting_items returned for this meeting and event_guid."
                        ),
                    )
                (item,) = [row for row in found.items if row["EventItemId"] == item_id][:1]
                async with asyncio.timeout_at(deadline):
                    html = await self._page(
                        client, pages.ACTION_PATH, params={"ID": str(item_id), "GUID": item_guid}
                    )
                action = pages.parse_action(html)
                if action.header["File"] != item["EventItemMatterFile"]:
                    # The action page must describe the file the meeting page listed.
                    raise SourceError("A Legistar action page shows another file")
        except SourceQueryError as error:
            if error.argument == "event_item_id" or error.code == "NOT_FOUND":
                raise
            raise _pages_unread(client, reason, error) from error
        except (SourceError, TimeoutError) as error:
            raise _pages_unread(client, reason, error) from error
        url = pages.page_url(client, pages.ACTION_PATH, item_id, item_guid)
        rows = [{name: vote[name] for name in WEB_VOTE_FIELDS} for vote in action.votes]
        result = SourceQueryResult(
            dataset_id=f"{client}/web-votes",
            dataset_name=(
                f"Legistar website votes, client {client}: "
                f"{_web_action_label(item_id, action.header)}; "
                f"{_web_meeting_label(meeting, found.header)}"
            ),
            domain=f"{client}.legistar.com",
            source_url=url,
            query=f"{pages.ACTION_PATH}?ID={item_id}&GUID={item_guid}",
            query_url=url,
            rows=rows,
            returned_row_count=len(rows),
            completeness=Completeness.TRUNCATED if action.unread else Completeness.COMPLETE,
            schema_sha256=_schema_hash("web-votes", WEB_VOTE_FIELDS),
        )
        warnings = [web_warning(client, reason), VOTE_RECORD_WARNING, PUBLIC_ONLY_WARNING]
        if action.unread:
            warnings.insert(0, PARTIAL_PAGE_WARNING)
        return Lookup(
            result=result,
            row_grain=WEB_VOTE_GRAIN,
            warnings=warnings,
            rules=([NO_VOTES_RULE] if not rows else [])
            + [web_rule(client, reason), VOTE_RULE, WEB_MINUTES_RULE],
            escalations=["LEGISTAR_WEB_PAGES"] + (["NO_VOTES_RECORDED"] if not rows else []),
        )

    async def _web_legislation(
        self,
        client: str,
        text: str,
        first: date | None,
        last: date | None,
        count: int,
        reason: str,
        started: float,
    ) -> Lookup:
        """Legislation from the client's public search page and matter pages.

        The search runs by title and, when the text holds a digit, by file or enactment
        number too. Only the first view of each search is read. The matter pages of the
        first matters listed are then read for sponsors, and for the action history of a
        matter whose file number is the text itself.
        """
        deadline = _page_deadline(started)
        span = None
        if first or last:
            span = (
                max(first or pages.EARLIEST_SEARCH_DATE, pages.EARLIEST_SEARCH_DATE),
                min(last or pages.LATEST_SEARCH_DATE, pages.LATEST_SEARCH_DATE),
            )
            if span[0] > span[1]:
                raise SourceError("The dates lie outside what the Legistar search page accepts")
        dates = {"first": span[0], "last": span[1]} if span else {}
        searches = [pages.Criteria(title=text, **dates)]
        if any(character.isdigit() for character in text):
            searches.insert(0, pages.Criteria(file=text, **dates))
        async with self._page_turn(deadline):
            async with asyncio.timeout_at(deadline):
                jar: dict[str, str] = {}
                form = pages.parse_search_form(
                    await self._page(client, pages.SEARCH_PATH, jar=jar, learn=True)
                )
                if not form.advanced:
                    form = pages.parse_search_form(
                        await self._page(
                            client,
                            pages.SEARCH_PATH,
                            form=pages.switch_fields(form),
                            jar=jar,
                            learn=True,
                        )
                    )
                if not form.advanced:
                    raise SourceError("The Legistar legislation page has no advanced search")
                answers = []
                for criteria in searches:
                    # Each search posts the same form and cookies, so one search's
                    # criteria never carry into the next.
                    html = await self._page(
                        client,
                        pages.SEARCH_PATH,
                        form=pages.search_fields(form, criteria),
                        jar=jar,
                    )
                    answers.append(pages.parse_search_results(client, html, criteria))
            found: dict[int, dict[str, Any]] = {}
            exact: set[int] = set()
            for criteria, answer in zip(searches, answers, strict=True):
                rows = answer.rows
                if criteria.file:
                    # A matter whose file number is the text itself comes first.
                    rows = sorted(rows, key=lambda row: not _same_file(row, text))
                    exact.update(row["MatterId"] for row in rows if _same_file(row, text))
                for row in rows:
                    found.setdefault(row["MatterId"], row)
            listed = list(found.values())[:count]
            read = 0
            cut_history: list[str] = []
            for row in listed[:MAX_DETAIL_PAGES]:
                try:
                    async with asyncio.timeout_at(deadline):
                        html = await self._page(
                            client,
                            pages.MATTER_PATH,
                            params={"ID": str(row["MatterId"]), "GUID": row["_guid"]},
                        )
                    matter = pages.parse_matter(client, html)
                    if matter.details["MatterFile"] != row["MatterFile"]:
                        raise SourceError("A Legistar matter page shows another file")
                except (SourceError, TimeoutError):
                    # The matters stay listed; the pages not read are named below.
                    break
                for name, value in matter.details.items():
                    if value is not None or row.get(name) is None:
                        row[name] = value
                row["DetailPageRead"] = True
                if row["MatterId"] in exact:
                    row["History"] = matter.history
                    if matter.history_unread:
                        cut_history.append(str(row["MatterFile"]))
                read += 1
        rows = [
            {
                name: row.get(name, False if name == "DetailPageRead" else None)
                for name in WEB_MATTER_FIELDS
            }
            for row in listed
        ]
        gaps = [
            (answer.total, len(answer.rows), answer.newest_first)
            for answer in answers
            if answer.total > len(answer.rows)
        ]
        page_url = f"{pages.site(client)}/{pages.SEARCH_PATH}"
        result = SourceQueryResult(
            dataset_id=f"{client}/web-matters",
            dataset_name=f"Legistar website legislation, client {client}",
            domain=f"{client}.legistar.com",
            source_url=page_url,
            query=f"{pages.SEARCH_PATH} advanced search: "
            + " | ".join(criteria.describe() for criteria in searches),
            query_url=page_url,
            rows=rows,
            returned_row_count=len(rows),
            completeness=(
                Completeness.TRUNCATED
                if gaps or cut_history or len(found) > count
                else Completeness.COMPLETE
            ),
            schema_sha256=_schema_hash("web-matters", WEB_MATTER_FIELDS),
        )
        warnings = [web_warning(client, reason), WEB_SEARCH_WARNING]
        rules = [web_rule(client, reason), WEB_SPONSOR_RULE]
        escalations = ["LEGISTAR_WEB_PAGES"]
        for total, shown, newest_first in gaps:
            warnings.insert(0, search_gap_warning(total, shown, newest_first))
            rules.insert(0, search_gap_rule(total, shown))
        if gaps:
            escalations.append("LEGISTAR_WEB_SEARCH_GAP")
        if cut_history:
            warnings.insert(0, history_gap_warning(cut_history))
            rules.insert(0, HISTORY_GAP_RULE)
            escalations.append("LEGISTAR_WEB_HISTORY_GAP")
        if read < len(rows):
            warnings.append(detail_warning(read, len(rows)))
            warnings.append(WEB_TITLE_CUT_WARNING)
            rules.append(DETAIL_RULE)
            escalations.append("LEGISTAR_WEB_DETAILS_UNREAD")
        if span:
            warnings.append(NO_DATE_WARNING)
            if (first, last) != span:
                warnings.append(search_dates_warning(*span))
        warnings.extend([PUBLIC_ONLY_WARNING, WEB_SEARCH_LINK_WARNING])
        return Lookup(
            result=result,
            row_grain=WEB_MATTER_GRAIN,
            warnings=warnings,
            rules=rules,
            escalations=escalations,
        )

    def get_meeting_items(
        self, *, client: str, event_id: int, event_guid: str | None = None
    ) -> PreparedLookup:
        name = validate_client(client)
        meeting = _positive_id("event_id", event_id)
        guid = _guid("event_guid", event_guid) if event_guid is not None else None
        params = {"AgendaNote": "1", "MinutesNote": "1", "$top": str(MAX_REPLY_ROWS)}

        async def run() -> Lookup:
            started = asyncio.get_running_loop().time()
            try:
                event, _ = await self._get(name, f"events/{meeting}", {}, by_id=True)
            except SourceQueryError as error:
                reason = await self._page_reason(name, error, guid)
                if reason is None or guid is None:
                    raise
                return await self._web_meeting_items(name, meeting, guid, reason, started)
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
            arguments={"client": name, "event_id": meeting, "event_guid": guid},
            run=run,
        )

    def get_item_votes(
        self, *, client: str, event_id: int, event_item_id: int, event_guid: str | None = None
    ) -> PreparedLookup:
        name = validate_client(client)
        meeting = _positive_id("event_id", event_id)
        item_id = _positive_id("event_item_id", event_item_id)
        guid = _guid("event_guid", event_guid) if event_guid is not None else None

        async def run() -> Lookup:
            started = asyncio.get_running_loop().time()
            try:
                event, _ = await self._get(name, f"events/{meeting}", {}, by_id=True)
            except SourceQueryError as error:
                reason = await self._page_reason(name, error, guid)
                if reason is None or guid is None:
                    raise
                return await self._web_item_votes(name, meeting, guid, item_id, reason, started)
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
            arguments={
                "client": name,
                "event_id": meeting,
                "event_item_id": item_id,
                "event_guid": guid,
            },
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
        words = _search_text("text", text)
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
            started = asyncio.get_running_loop().time()
            check = asyncio.create_task(self._newest_matter(name))
            try:
                records, public_url = await self._list(name, "matters", params)
            except SourceQueryError as error:
                check.cancel()
                if error.code != "CLIENT_MISCONFIGURED":
                    raise
                try:
                    return await self._web_legislation(
                        name, words, first, last, count, SETTING_REASON, started
                    )
                except (SourceError, TimeoutError) as failure:
                    raise _pages_unread(name, SETTING_REASON, failure) from failure
            except BaseException:
                check.cancel()
                raise
            # Bounded by the check's own timeout, counted from when both started.
            newest = await check
            stale = newest is not None and newest < self._today() - STALE_AFTER
            site_unread = False
            if stale and newest is not None:
                try:
                    return await self._web_legislation(
                        name, words, first, last, count, stale_reason(newest), started
                    )
                except (SourceError, TimeoutError):
                    # The site could not be read, so the API's older copy stands below,
                    # under its warning that it has stopped updating.
                    site_unread = True
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
            if stale and newest is not None:
                if site_unread:
                    warnings.insert(0, SITE_UNREAD_WARNING)
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

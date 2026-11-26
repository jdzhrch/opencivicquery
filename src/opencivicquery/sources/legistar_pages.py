"""Public Legistar pages: the legislation search, one matter, one meeting, one action.

legistar.py reads these pages for a client whose Web API copy has stopped updating or
cannot answer. This module touches no network. It builds the form fields of a search and
parses the HTML that comes back; legistar.py sends each request through FixedHostClient to
https://<client>.legistar.com.

The request sequence and the page landmarks used here (the switch to advanced search, the
search form's field names, the state of its date fields, the ids of the grids and labels,
and the popup link of an action's details) are adapted from python-legistar-scraper,
https://github.com/opencivicdata/python-legistar-scraper, which carries this license:

    Copyright (c) 2014-2018, Sunlight Foundation, Mjumbe Poe, Forest Gregg, DataMade

    All rights reserved.

    Redistribution and use in source and binary forms, with or without modification,
    are permitted provided that the following conditions are met:

        * Redistributions of source code must retain the above copyright notice,
          this list of conditions and the following disclaimer.
        * Redistributions in binary form must reproduce the above copyright notice,
          this list of conditions and the following disclaimer in the documentation
          and/or other materials provided with the distribution.
        * Neither the name of legistar nor the names of its contributors
          may be used to endorse or promote products derived from this software
          without specific prior written permission.

    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
    "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
    LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
    A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR
    CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
    EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
    PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
    PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
    LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
    NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
    SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from functools import wraps
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from opencivicquery.sources.base import SourceError

SEARCH_PATH = "Legislation.aspx"
MATTER_PATH = "LegislationDetail.aspx"
MEETING_PATH = "MeetingDetail.aspx"
ACTION_PATH = "HistoryDetail.aspx"
PAGE_PATHS = frozenset({SEARCH_PATH, MATTER_PATH, MEETING_PATH, ACTION_PATH})
"""The only pages of a client's site that the page reader requests."""

SEARCH_MAX_RESULTS = 500
SEARCH_PAGE_ROWS = 100
# checked 2026-11-26: the sfgov advanced search offers 10, 100, 500, 1000, 10000, or All
# results. With 500 chosen, a search with 363 matches showed "363 records" and a pager
# reading "Page 1 of 4, items 1 to 100 of 363"; with 100 chosen the same page gave no sign
# that more existed. Rows came highest matter ID first.
TITLE_CUT = 600
# checked 2026-11-26: the longest titles in a 100-row sfgov result were all exactly 600
# characters, and one of them ended mid-sentence.
EARLIEST_SEARCH_DATE = date(1980, 1, 1)
LATEST_SEARCH_DATE = date(2099, 12, 30)
# checked 2026-11-26: each date field of the sfgov advanced search lists its range as
# [[1980,1,1],[2099,12,30]]. A search "between" 2026-11-24 and 2026-11-24 returned the two
# matters introduced that day, so both ends are included.

GUID = re.compile(r"^[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\Z")
_ID = re.compile(r"^[1-9][0-9]{0,9}\Z")
_PAGE_DATE = re.compile(r"^([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})\Z")
_RECORDS = re.compile(r"^([0-9]{1,7}) records?\Z")
_VIEW = re.compile(r"^Show: (.{1,60})\Z")
_POPUP = re.compile(r"radopen\('([^']+)'")
# checked 2026-11-26: sfgov writes an action's details link as
# onclick="var obj = radopen('HistoryDetail.aspx?ID=...&GUID=...', 'HistoryDetail'); ...".
_MISSING_PAGE = ("Invalid parameters!", "This record no longer exists")
# checked 2026-11-26: sfgov answered LegislationDetail.aspx?ID=8156781 without its GUID
# with HTTP 200 and the 19-byte body "Invalid parameters!".

_PREFIX = "ctl00_ContentPlaceHolder1_"
_FORM = "ctl00$ContentPlaceHolder1$"
_SWITCH = f"{_PREFIX}btnSwitch"
_SEARCH_GRID = f"{_PREFIX}gridMain_ctl00"
_HISTORY_GRID = f"{_PREFIX}gridLegislation_ctl00"
_MEETING_GRID = f"{_PREFIX}gridMain_ctl00"
_VOTE_GRID = f"{_PREFIX}gridVote_ctl00"
_NO_MATCH = "No records were found."
# checked 2026-11-26: the sfgov search page shows "Please enter your search criteria."
# until a search has run, and "No records were found." after a search with no match. Any
# other line in an empty result is taken as a search that did not finish.
LEGISLATION_VIEW = "Legislation only"
# checked 2026-11-26: the sfgov meeting page's view menu read "Show: Legislation only",
# with "All agenda items" as its other choice.

HISTORY_FIELDS = (
    "ActionDate",
    "Version",
    "ActionBy",
    "Action",
    "Result",
    "EventItemId",
    "EventId",
    "EventGuid",
    "ActionDetailURL",
    "MeetingDetailURL",
)


class PageNotFound(SourceError):
    """The site has no page for that ID and GUID."""


def site(client: str) -> str:
    return f"https://{client}.legistar.com"


def page_url(client: str, path: str, record_id: int, guid: str) -> str:
    """The address of one record's page, built only from a checked ID and GUID."""
    if path not in PAGE_PATHS or not GUID.match(guid):
        raise SourceError("A Legistar page address was asked for with an unchecked part")
    return f"{site(client)}/{path}?" + urlencode({"ID": record_id, "GUID": guid.upper()})


def reference(client: str, href: str | None, path: str) -> tuple[int, str] | None:
    """The ID and GUID a link carries, when it points at path on the client's own site."""
    if not href:
        return None
    try:
        parts = urlsplit(urljoin(f"{site(client)}/", href))
    except ValueError:
        # A link the URL parser refuses is a link that carries nothing.
        return None
    if parts.scheme != "https" or parts.netloc != f"{client}.legistar.com":
        return None
    if parts.path != f"/{path}":
        return None
    query = parse_qs(parts.query)
    record_id = (query.get("ID") or [""])[0]
    guid = (query.get("GUID") or [""])[0]
    if not _ID.match(record_id) or int(record_id) > 2**31 - 1 or not GUID.match(guid):
        return None
    return int(record_id), guid.upper()


def _file_link(client: str, href: str, path: str) -> str | None:
    """A link to an agenda or minutes file, kept only when it stays on the client's site."""
    try:
        url = urljoin(f"{site(client)}/", href)
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme == "https" and parts.netloc == f"{client}.legistar.com" and parts.path == path:
        return url
    return None


@dataclass
class Cell:
    parts: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    popups: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)

    def text(self) -> str:
        return " ".join("".join(self.parts).split())


class _Page(HTMLParser):
    """The form fields, labelled values, and one grid of a Legistar page.

    Only the named grid table is read as rows. A pager sits in a nested table, whose cells
    are never taken for rows.
    """

    def __init__(self, grid_id: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.grid_id = grid_id
        self.hidden: dict[str, str] = {}
        self.values: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.checked: dict[str, str] = {}
        self.fields: dict[str, Cell] = {}
        self.records: int | None = None
        self.view: str | None = None
        self.has_grid = False
        self.has_pager = False
        self.headers: list[str] = []
        self.rows: list[list[Cell]] = []
        self.empty_note: str | None = None
        self._depth = 0
        self._header: list[str] | None = None
        self._row: list[Cell] | None = None
        self._cell: Cell | None = None
        self._note: list[str] | None = None
        self._field: Cell | None = None
        self._field_tag = ""
        self._field_depth = 0
        self._anchor: list[str] | None = None
        self._label: list[str] | None = None
        self._unseen = 0

    def _input(self, attributes: dict[str, str | None]) -> None:
        name, value = attributes.get("name"), attributes.get("value") or ""
        kind = (attributes.get("type") or "").lower()
        if kind == "hidden" and name:
            self.hidden[name] = value
        if kind == "radio" and name and "checked" in attributes:
            self.checked[name] = value
        identifier = attributes.get("id")
        if identifier:
            self.values[identifier] = value
            if name:
                self.names[identifier] = name

    def _target(self) -> Cell | None:
        return self._cell if self._cell is not None else self._field

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag in {"script", "style"}:
            # Their text is code, never a value to report.
            self._unseen += 1
            return
        if tag == "input":
            self._input(attributes)
            return
        if tag == "span" and "rmText" in classes:
            self._label = []
        if tag == "table":
            if self._depth:
                self._depth += 1
            elif self.grid_id is not None and attributes.get("id") == self.grid_id:
                self._depth = 1
                self.has_grid = True
            return
        if self._depth:
            self._grid_start(tag, attributes, classes)
            return
        identifier = attributes.get("id") or ""
        if self._field is not None:
            if tag == self._field_tag:
                self._field_depth += 1
        elif tag in {"span", "a"} and identifier.startswith(_PREFIX):
            self._field = Cell()
            self._field_tag = tag
            self._field_depth = 1
            self.fields[identifier.removeprefix(_PREFIX)] = self._field
        self._inline(tag, attributes)

    def _grid_start(self, tag: str, attributes: dict[str, str | None], classes: list[str]) -> None:
        if tag == "tr" and "rgPager" in classes:
            self.has_pager = True
        if self._depth != 1:
            # A table inside a cell still belongs to that cell.
            if self._cell is not None:
                self._inline(tag, attributes)
            return
        if tag == "tr" and ("rgRow" in classes or "rgAltRow" in classes):
            if self._row is not None:
                # An unclosed row would otherwise vanish without a trace.
                raise SourceError("A Legistar page row was never closed")
            self._row = []
        elif tag == "tr" and "rgNoRecords" in classes:
            self._note = []
        elif tag == "th" and "rgHeader" in classes:
            self._header = []
        elif tag == "td" and self._row is not None:
            self._cell = Cell()
        else:
            self._inline(tag, attributes)

    def _inline(self, tag: str, attributes: dict[str, str | None]) -> None:
        target = self._target()
        if target is None:
            return
        if tag == "a":
            if attributes.get("href"):
                target.links.append(str(attributes["href"]))
            popup = _POPUP.search(attributes.get("onclick") or "")
            if popup:
                target.popups.append(popup.group(1))
            self._anchor = []
        elif tag == "br":
            target.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._unseen = max(0, self._unseen - 1)
            return
        if tag == "span" and self._label is not None:
            label = " ".join("".join(self._label).split())
            records, view = _RECORDS.match(label), _VIEW.match(label)
            if records and self.records is None:
                self.records = int(records.group(1))
            if view and self.view is None:
                self.view = view.group(1)
            self._label = None
        if tag == "a" and self._anchor is not None:
            target = self._target()
            name = " ".join("".join(self._anchor).split())
            if target is not None and name:
                target.anchors.append(name)
            self._anchor = None
        if self._depth:
            if tag == "table":
                self._depth -= 1
            elif self._depth == 1:
                self._grid_end(tag)
            return
        if self._field is not None and tag == self._field_tag:
            self._field_depth -= 1
            if not self._field_depth:
                self._field = None

    def _grid_end(self, tag: str) -> None:
        if tag == "th" and self._header is not None:
            self.headers.append(" ".join("".join(self._header).split()))
            self._header = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "tr" and self._note is not None:
            self.empty_note = " ".join("".join(self._note).split())
            self._note = None

    def handle_data(self, data: str) -> None:
        if self._unseen:
            return
        if self._label is not None:
            self._label.append(data)
        if self._anchor is not None:
            self._anchor.append(data)
        if self._depth:
            if self._cell is not None:
                self._cell.parts.append(data)
            elif self._depth == 1 and self._header is not None:
                self._header.append(data)
            elif self._depth == 1 and self._note is not None:
                self._note.append(data)
        elif self._field is not None:
            self._field.parts.append(data)

    def value(self, name: str) -> str | None:
        """The text of one labelled value, or None when the page has no such label."""
        cell = self.fields.get(name)
        return (cell.text() or None) if cell is not None else None

    def table(self) -> list[dict[str, Cell]]:
        """The grid's rows by column label."""
        if not self.has_grid:
            raise SourceError("The Legistar page has no table where one was expected")
        if self._row is not None:
            raise SourceError("A Legistar page row was never closed")
        rows = []
        for cells in self.rows:
            if len(cells) != len(self.headers):
                raise SourceError("A Legistar page row does not match the page's columns")
            rows.append({label: cell for label, cell in zip(self.headers, cells, strict=True)})
        return rows

    def unread_rows(self) -> bool:
        """True when the grid may hold more rows than this page shows."""
        return self.has_pager or (self.records is not None and self.records > len(self.rows))


def _parse(html: str, grid_id: str | None) -> _Page:
    if len(html) < 2000 and any(marker in html for marker in _MISSING_PAGE):
        raise PageNotFound("The Legistar site has no page for that ID and GUID")
    page = _Page(grid_id)
    page.feed(html)
    page.close()
    return page


def _fail_closed[**P, T](parse: Callable[P, T]) -> Callable[P, T]:
    """Turn any error a malformed page provokes into the one error the callers handle."""

    @wraps(parse)
    def closed(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return parse(*args, **kwargs)
        except SourceError:
            raise
        except (ValueError, LookupError, TypeError, AssertionError, RecursionError) as exc:
            raise SourceError("A Legistar page could not be read as expected") from exc

    return closed


def _require(table: list[dict[str, Cell]], columns: tuple[str, ...], page: str) -> None:
    """Refuse a grid that lacks a column its rows are built from."""
    if table and any(column not in table[0] for column in columns):
        raise SourceError(f"The Legistar {page} page lacks a column this server reads")


def _page_date(text: str | None) -> str | None:
    """A page date such as 7/28/2026 as 2026-07-28, or None for an empty cell."""
    if not text:
        return None
    match = _PAGE_DATE.match(text)
    if not match:
        raise SourceError("A Legistar page has a date in an unknown form")
    month, day, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError as exc:
        raise SourceError("A Legistar page has an impossible date") from exc


def _text(row: dict[str, Cell], label: str) -> str | None:
    cell = row.get(label)
    return (cell.text() or None) if cell is not None else None


# --- The legislation search form ---------------------------------------------------


@dataclass(frozen=True)
class SearchForm:
    hidden: dict[str, str]
    advanced: bool
    switch: tuple[str, str] | None
    """The name and value of the button that switches between simple and advanced search."""


@_fail_closed
def parse_search_form(html: str) -> SearchForm:
    page = _parse(html, None)
    label = page.values.get(_SWITCH)
    name = page.names.get(_SWITCH)
    if label is None or name is None or "__VIEWSTATE" not in page.hidden:
        raise SourceError("The Legistar legislation page has no search form")
    # The button names the mode it switches to, so "Simple search" marks the advanced form.
    return SearchForm(
        hidden=dict(page.hidden), advanced="simple search" in label.lower(), switch=(name, label)
    )


def switch_fields(form: SearchForm) -> dict[str, str]:
    """The form post that switches the page to advanced search."""
    if form.switch is None:
        raise SourceError("The Legistar legislation page has no search mode switch")
    return {**form.hidden, form.switch[0]: form.switch[1]}


@dataclass(frozen=True)
class Criteria:
    """One advanced search: text inside the title, or inside the file or enactment number."""

    title: str = ""
    file: str = ""
    first: date | None = None
    last: date | None = None

    def describe(self) -> str:
        parts = []
        if self.title:
            parts.append(f"Title contains {json.dumps(self.title)}")
        if self.file:
            parts.append(f"File/enactment # contains {json.dumps(self.file)}")
        if self.first and self.last:
            parts.append(f"Introduced between {self.first.isoformat()} and {self.last.isoformat()}")
        parts.append(f"Number of results {SEARCH_MAX_RESULTS}")
        parts.append("Date All Years")
        return "; ".join(parts)


def _date_state(day: date) -> str:
    stamp = f"{day.isoformat()}-00-00-00"
    return json.dumps(
        {
            "enabled": True,
            "emptyMessage": "",
            "validationText": stamp,
            "valueAsString": stamp,
            "minDateStr": "1980-01-01-00-00-00",
            "maxDateStr": "2099-12-31-00-00-00",
            "lastSetTextBoxValue": f"{day.month}/{day.day}/{day.year}",
        }
    )


def search_fields(form: SearchForm, criteria: Criteria) -> dict[str, str]:
    """The form post that runs one advanced search."""
    if not form.advanced:
        raise SourceError("The Legistar legislation page is not on advanced search")
    fields = {
        **form.hidden,
        f"{_FORM}txtTit": criteria.title,
        f"{_FORM}txtFil": criteria.file,
        f"{_PREFIX}lstMax_ClientState": json.dumps({"value": str(SEARCH_MAX_RESULTS)}),
        f"{_PREFIX}lstYearsAdvanced_ClientState": json.dumps({"value": "All"}),
        f"{_FORM}btnSearch": "Search Legislation",
    }
    if criteria.first and criteria.last:
        for number, day in ((1, criteria.first), (2, criteria.last)):
            fields[f"{_FORM}txtFileCreated{number}"] = day.isoformat()
            fields[f"{_FORM}txtFileCreated{number}$dateInput"] = f"{day.month}/{day.day}/{day.year}"
            fields[f"{_PREFIX}txtFileCreated{number}_dateInput_ClientState"] = _date_state(day)
        fields[f"{_FORM}radFileCreated"] = "between"
    return fields


@dataclass(frozen=True)
class SearchResults:
    rows: list[dict[str, Any]]
    total: int
    """The page's own count of matching matters, at most SEARCH_MAX_RESULTS."""
    newest_first: bool
    """True when the rows come highest matter ID first, as sfgov's did on 2026-11-26."""


def _check_echo(page: _Page, criteria: Criteria) -> None:
    """Refuse a results page that does not show the search that was asked for.

    The page prints the criteria it ran back into its form. A page that ignored the post
    would otherwise pass for an empty or unrelated result.
    """
    expected = {
        "txtTit": criteria.title,
        "txtFil": criteria.file,
        "lstMax_Input": str(SEARCH_MAX_RESULTS),
        "lstYearsAdvanced_Input": "All Years",
        "txtFileCreated1": criteria.first.isoformat() if criteria.first else "",
        "txtFileCreated2": criteria.last.isoformat() if criteria.last else "",
    }
    for name, value in expected.items():
        if page.values.get(f"{_PREFIX}{name}") != value:
            raise SourceError("The Legistar legislation page did not run the requested search")
    operator = page.checked.get(f"{_FORM}radFileCreated")
    if criteria.first and criteria.last and operator != "between":
        raise SourceError("The Legistar legislation page did not apply the date range")


@_fail_closed
def parse_search_results(client: str, html: str, criteria: Criteria) -> SearchResults:
    page = _parse(html, _SEARCH_GRID)
    _check_echo(page, criteria)
    table = page.table()
    if page.records is None:
        raise SourceError("The Legistar legislation page gave no count of matches")
    if not table and (page.records or page.empty_note != _NO_MATCH):
        # Only the page's own no-match line reads as no match.
        raise SourceError("The Legistar legislation page showed no result for the search")
    if len(table) > page.records or (not page.has_pager and len(table) != page.records):
        raise SourceError("The Legistar legislation page's rows do not match its count")
    _require(
        table, ("File #", "Title", "Type", "Status", "Introduced", "Final Action"), "legislation"
    )
    rows = []
    for row in table:
        matter = None
        for href in row["File #"].links:
            matter = matter or reference(client, href, MATTER_PATH)
        if matter is None:
            raise SourceError("A Legistar legislation row has no link to its matter page")
        matter_id, guid = matter
        rows.append(
            {
                "MatterId": matter_id,
                "MatterFile": _text(row, "File #"),
                "MatterName": _text(row, "Name"),
                "MatterTitle": _text(row, "Title"),
                "MatterTypeName": _text(row, "Type"),
                "MatterStatusName": _text(row, "Status"),
                "MatterIntroDate": _page_date(_text(row, "Introduced")),
                "MatterPassedDate": _page_date(_text(row, "Final Action")),
                "LegislationDetailURL": page_url(client, MATTER_PATH, matter_id, guid),
                "_guid": guid,
            }
        )
    identifiers = [row["MatterId"] for row in rows]
    return SearchResults(
        rows=rows,
        total=page.records,
        newest_first=identifiers == sorted(identifiers, reverse=True),
    )


# --- One matter ---------------------------------------------------------------------


@dataclass(frozen=True)
class Matter:
    details: dict[str, Any]
    history: list[dict[str, Any]]
    """The matter's actions, in the page's order."""
    history_unread: bool
    """True when the page shows only part of the matter's action history."""


@_fail_closed
def parse_matter(client: str, html: str) -> Matter:
    page = _parse(html, _HISTORY_GRID)
    file = page.value("lblFile2")
    if file is None:
        raise SourceError("The Legistar matter page has no file number")
    sponsors = page.fields.get("lblSponsors2")
    # None means the page has no sponsors field at all; an empty list, a field left empty.
    names = None
    if sponsors is not None:
        names = sponsors.anchors or ([sponsors.text()] if sponsors.text() else [])
    details = {
        "MatterFile": file,
        "MatterVersion": page.value("lblVersion2"),
        "MatterName": page.value("lblName2"),
        "MatterTitle": page.value("lblTitle2"),
        "MatterTypeName": page.value("lblType2"),
        "MatterStatusName": page.value("lblStatus2"),
        "MatterBodyName": page.value("hypInControlOf2"),
        "MatterIntroDate": _page_date(page.value("lblIntroduced2")),
        "MatterAgendaDate": _page_date(page.value("lblOnAgenda2")),
        "MatterPassedDate": _page_date(page.value("lblPassed2")),
        "MatterEnactmentDate": _page_date(page.value("lblEnactmentDate2")),
        "MatterEnactmentNumber": page.value("lblEnactmentNumber2"),
        "Sponsors": names,
    }
    table = page.table()
    _require(table, ("Date", "Action By", "Action", "Result"), "matter")
    history = []
    for row in table:
        action = meeting = None
        for popup in row["Action Details"].popups if "Action Details" in row else []:
            action = action or reference(client, popup, ACTION_PATH)
        for href in row["Meeting Details"].links if "Meeting Details" in row else []:
            meeting = meeting or reference(client, href, MEETING_PATH)
        history.append(
            {
                "ActionDate": _page_date(_text(row, "Date")),
                "Version": _text(row, "Ver."),
                "ActionBy": _text(row, "Action By"),
                "Action": _text(row, "Action"),
                "Result": _text(row, "Result"),
                "EventItemId": action[0] if action else None,
                "EventId": meeting[0] if meeting else None,
                "EventGuid": meeting[1] if meeting else None,
                "ActionDetailURL": page_url(client, ACTION_PATH, *action) if action else None,
                "MeetingDetailURL": page_url(client, MEETING_PATH, *meeting) if meeting else None,
            }
        )
    return Matter(details=details, history=history, history_unread=page.unread_rows())


# --- One meeting --------------------------------------------------------------------


@dataclass(frozen=True)
class Meeting:
    header: dict[str, Any]
    items: list[dict[str, Any]]
    item_guids: dict[int, str]
    """The GUID of each item's action page, by EventItemId."""
    unread: bool
    """True when the page shows only part of the meeting's items."""
    view: str | None
    """The view the page says it shows, such as Legislation only. None when it says none."""


@_fail_closed
def parse_meeting(client: str, html: str) -> Meeting:
    page = _parse(html, _MEETING_GRID)
    body, day = page.value("hypName"), _page_date(page.value("lblDate"))
    if body is None or day is None:
        raise SourceError("The Legistar meeting page names no body or date")

    def file_link(name: str) -> str | None:
        cell = page.fields.get(name)
        for href in cell.links if cell is not None else []:
            link = _file_link(client, href, "/View.ashx")
            if link:
                return link
        return None

    header = {
        "EventBodyName": body,
        "EventDate": day,
        "EventTime": page.value("lblTime"),
        "EventLocation": page.value("lblLocation"),
        "EventAgendaStatusName": page.value("lblAgendaStatus"),
        "EventMinutesStatusName": page.value("lblMinutesStatus"),
        "EventAgendaFile": file_link("hypAgenda"),
        "EventMinutesFile": file_link("hypMinutes"),
    }
    table = page.table()
    _require(table, ("File #", "Title", "Action", "Result", "Action Details"), "meeting")
    items = []
    guids: dict[int, str] = {}
    for row in table:
        action = matter = None
        for popup in row["Action Details"].popups if "Action Details" in row else []:
            action = action or reference(client, popup, ACTION_PATH)
        for href in row["File #"].links:
            matter = matter or reference(client, href, MATTER_PATH)
        if action:
            guids[action[0]] = action[1]
        items.append(
            {
                "EventItemId": action[0] if action else None,
                "EventItemAgendaNumber": _text(row, "Agenda #"),
                "EventItemMatterFile": _text(row, "File #"),
                "EventItemVersion": _text(row, "Ver."),
                "EventItemMatterName": _text(row, "Name"),
                "EventItemMatterType": _text(row, "Type"),
                "EventItemMatterStatus": _text(row, "Status"),
                "EventItemTitle": _text(row, "Title"),
                "EventItemActionName": _text(row, "Action"),
                "EventItemPassedFlagName": _text(row, "Result"),
                "EventItemMatterId": matter[0] if matter else None,
                "LegislationDetailURL": page_url(client, MATTER_PATH, *matter) if matter else None,
                "ActionDetailURL": page_url(client, ACTION_PATH, *action) if action else None,
            }
        )
    return Meeting(
        header=header, items=items, item_guids=guids, unread=page.unread_rows(), view=page.view
    )


# --- One action ---------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    header: dict[str, Any]
    votes: list[dict[str, Any]]
    unread: bool
    """True when the page shows only part of the action's votes."""


@_fail_closed
def parse_action(html: str) -> Action:
    page = _parse(html, _VOTE_GRID)
    if "lblAction" not in page.fields and "hypFile" not in page.fields:
        raise SourceError("The Legistar action page names no file or action")
    if not page.has_grid:
        # checked 2026-11-26: the sfgov page of an action without a roll call, the mayor's
        # approval of file 260875, still carried the vote table, with "No records to
        # display." So a page without the table is one this server cannot read.
        raise SourceError("The Legistar action page has no vote table")
    header = {
        "File": page.value("hypFile"),
        "Version": page.value("lblVersion2"),
        "Type": page.value("lblType"),
        "Name": page.value("lblTitle"),
        "Mover": page.value("hypMover"),
        "Seconder": page.value("hypSeconder"),
        "Result": page.value("lblResult"),
        "Action": page.value("lblAction"),
        "ActionText": page.value("lblActionText"),
        "AgendaNote": page.value("lblAgendaNote"),
        "MinutesNote": page.value("lblMinutesNote"),
    }
    table = page.table()
    _require(table, ("Person Name", "Vote"), "action")
    votes = [
        {"VotePersonName": _text(row, "Person Name"), "VoteValueName": _text(row, "Vote")}
        for row in table
    ]
    return Action(header=header, votes=votes, unread=page.unread_rows())

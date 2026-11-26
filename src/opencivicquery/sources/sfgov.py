"""SF.gov pages API: news, meeting pages, and titles on San Francisco's city website.

SF.gov serves its own pages from a content interface at api.sf.gov. It answers plain GET
requests with JSON and needs no key. This project found no public documentation for it, so
every statement here about a field or a filter comes from a dated live check, and every
response shape this module computes with is validated before it is used.

The host is fixed, so no caller can steer a request elsewhere. Documents, images, and
videos live on other hosts; this module returns their links and never requests them.

The module reads the content interface behind www.sf.gov on the one host api.sf.gov and
identifies itself as OpenCivicQuery. Each server process runs one lookup at a time with at
most one request a second.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx

from opencivicquery.models import Completeness, SourceQueryResult, normalize_domain
from opencivicquery.sources.base import Lookup, PreparedLookup, SourceError, SourceQueryError
from opencivicquery.sources.http import FixedHostClient

SFGOV_HOST = "api.sf.gov"
PUBLIC_SITE = "https://www.sf.gov"
MEDIA_HOST = "media.api.sf.gov"
PAGES_URL = f"https://{SFGOV_HOST}/api/v2/pages/"
DOCUMENTS_URL = f"https://{SFGOV_HOST}/api/v2/documents/"
NEWS_INDEX_URL = f"{PUBLIC_SITE}/news"
# checked 2026-11-26: https://www.sf.gov/news answered HTTP 308 to /news--all.
SOURCE_NAME = "SF.gov pages API"

MIN_REQUEST_INTERVAL_SECONDS = 1.0
# Our own bound, not the source's: the interface sent no rate-limit header on 2026-11-26,
# and this module keeps its volume low.

NEWS_READ_LIMIT = 500
# checked 2026-11-26: limit=500 returned 500 news items in one answer of about 540 KB, so
# the interface enforces no smaller page size. One request avoids paging through items
# that share a date, whose order between pages the interface does not fix.
MEETING_PAGE_SIZE = 100
MAX_MEETING_PAGES = 5
# Our own bounds: five requests of 100 meeting pages per call. Each request after the first
# repeats the last page already read, so a call reads at most 496 pages of one body.
# checked 2026-11-26: the Police Commission had 200 English meeting pages.
MEETING_FIELDS_PARAM = "date_time,cancelled,videos,related_documents"
# checked 2026-11-26: the meeting list returns id, meta, and title by default and adds the
# fields named in `fields`. `agenda` is left out: on one Police Commission page (id 110072)
# the agenda blocks were more than half of the answer.
SEARCH_LIMIT = 50
AGENCY_CANDIDATES = 50
MAX_NAMED_CANDIDATES = 10
MIN_TEXT_LENGTH = 2
MAX_TEXT_LENGTH = 100
MAX_AGENCY_LENGTH = 120
MAX_RAW_INPUT_LENGTH = 1000
MAX_LINK_LENGTH = 2000
# Our own bounds: one character matches nearly everything, and the longest of the 330 agency
# titles read on 2026-11-26 had 97 characters.
EARLIEST_DATE = date(1990, 1, 1)
LATEST_DATE = date(2100, 12, 31)

SEARCH_KINDS = ("pages", "documents")

NEWS_FIELDS = ("date", "title", "news_type", "abstract", "page_url", "page_id")
MEETING_FIELDS = (
    "meeting_date",
    "end_date",
    "start_time",
    "other_dates",
    "title",
    "cancelled",
    "page_url",
    "documents",
    "video_links",
    "page_id",
)
PAGE_SEARCH_FIELDS = ("title", "page_type", "page_url", "first_published_at", "page_id")
DOCUMENT_SEARCH_FIELDS = (
    "title",
    "published_date",
    "uploaded_at",
    "file_url",
    "document_id",
)

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME = re.compile(r"[0-9]{2}:[0-9]{2}(:[0-9]{2})?")
_POSSESSIVE = re.compile(r"['’]s\b", re.IGNORECASE)
_NOT_WORD = re.compile(r"[\W_]+", re.UNICODE)
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_LINK_BREAKERS = frozenset("()[]<>\"'`\\")
# Characters that end or reopen a link in Markdown or HTML. A link that holds one is not
# passed on.

NEWS_GRAIN = (
    "One English news page on SF.gov, with the date, type, and summary its publishing "
    "agency gave it."
)
MEETING_GRAIN = (
    "One English meeting page on SF.gov whose primary agency is the named body. A meeting "
    "can have more than one page, a page can cover several dates, and a page can announce "
    "that no meeting takes place."
)
# checked 2026-11-26: the Police Commission had two pages titled "May 25, 2022 Police
# Commission meeting - No meeting notice" (ids 5312 and 5362).
PAGE_SEARCH_GRAIN = "One English page on SF.gov, of any kind, whose title matched the words."
DOCUMENT_SEARCH_GRAIN = (
    "One file in SF.gov's document library whose title or description matched the words."
)

UNDOCUMENTED_WARNING = (
    "This project found no public documentation for this interface. What each field means "
    "here comes from live checks on 2026-11-26, not from a statement by the city, so "
    "confirm a detail that matters on the public page before you publish it."
)
ENGLISH_ONLY_WARNING = (
    "Only English pages are read. SF.gov keeps translated copies as separate pages, and "
    "they are not in this result."
)
# checked 2026-11-26: the news list held 12,919 items without a locale filter and 2,511
# with locale=en.

NEWS_AGENCY_WARNING = (
    "Rows are news pages whose primary agency on SF.gov is the one named. A page that "
    "lists it only as a partner agency is not included."
)
# checked 2026-11-26: a news page carries primary_agency and a separate partner_agencies
# list; primary_agency=559 returned 277 items for the Office of the Mayor.
NEWS_NO_AGENCY_WARNING = (
    "The news list does not name the publishing agency on a row. Open page_url to see it, "
    "or ask again with an agency."
)
# checked 2026-11-26: the news list ignored the fields parameter and returned only id,
# html_url, title, news_type, image, date, abstract, and a shortened body.
NEWS_TYPE_WARNING = (
    "news_type is the label the publishing agency set. On 2026-11-26 the 500 newest items "
    "carried news, press_release, or no label, and all 11 Office of the Mayor items dated "
    "October 2026 were labeled news. Do not count press releases by this label alone."
)
# checked 2026-11-26: of the 500 newest English items 314 were news, 97 press_release, and
# 89 empty; a lookup for the Office of the Mayor, 2026-10-01 to 2026-10-31, returned 11
# items, all labeled news.
NO_NEWS_WARNING = (
    "No news page matched. This shows only that SF.gov lists no English news page with "
    "these dates and this primary agency; an agency can announce news elsewhere."
)

MEETING_DOCUMENTS_WARNING = (
    "SF.gov has no minutes field. documents lists the files staff attached to the meeting "
    "page, each with its group heading and title in their words. A file under a minutes "
    "heading can be the minutes of an earlier meeting, and an empty list does not show "
    "that no minutes exist. Read the title and open the file before you call it the "
    "minutes of that meeting."
)
# checked 2026-11-26: the March 9, 2022 Police Commission page (id 5209) listed a file
# titled "2/9/22 Minutes" under the heading Minutes; pages for August 20 and 27, 2025
# listed no file.
MEETING_AGENDA_WARNING = (
    "page_url is the meeting page, where the body publishes its agenda. This result does "
    "not hold the agenda items or the files attached to single items; open the page for "
    "those."
)
# checked 2026-11-26: the November 18, 2026 Police Commission page (id 110072) held 11
# agenda_item blocks with their own document lists and an empty related_documents list.
MEETING_CANCELLED_WARNING = (
    "cancelled is the page's own flag. A page can announce in its title that no meeting "
    "takes place while cancelled is false, so read the title as well."
)
# checked 2026-11-26: 57 of the 63 Police Commission pages whose titles hold "No meeting
# notice" had cancelled false.
MEETING_VIDEO_WARNING = (
    "video_links holds only the links the page lists as videos. An empty list does not "
    "show that no recording exists."
)
# checked 2026-11-26: 25 Police Commission pages from 2025 and 2026 listed no video, and
# the Health Commission page for November 2, 2026 (id 109274) linked one Granicus clip.
MEETING_SPAN_WARNING = (
    "A row with end_date set is a page that covers a span of dates, such as a notice that "
    "no meeting takes place for several weeks. It is kept when any day from meeting_date "
    "through end_date falls in the dates asked for. Read its title for the dates it covers."
)
# checked 2026-11-26: 13 of 199 Police Commission date blocks had an end_date after the
# start_date, such as page 6892, "from July 26, 2023 through August 30, 2023 ... No meeting
# notice". 32 had an end_date before the start_date, which this lookup ignores.
MEETING_BODY_WARNING = (
    "Rows are meeting pages whose primary agency is the body named. Some committees have "
    "agency pages of their own on SF.gov, so a committee's meetings may sit under the "
    "committee's name."
)
# checked 2026-11-26: the agency list held "Health Commission Finance and Planning
# Committee" and seven Arts Commission committees as agency pages of their own.
NO_MEETINGS_WARNING = (
    "No meeting page matched. This shows only that SF.gov lists no English meeting page "
    "for this body in these dates. San Francisco bodies also publish meetings on other "
    "sites, such as the city's Legistar calendar, so it does not show that the body did "
    "not meet."
)
# checked 2026-11-22 (README, Legistar section): the city's Legistar calendar page listed
# 181 meetings for 2026.

PAGE_SEARCH_WARNING = (
    "The search matches words in page titles, and every word must match. In a check on "
    "2026-11-26 it did not match a word that stood only in a page's summary. Rows come in "
    "the interface's own order and cover every kind of page: news, meetings, reports, "
    "agency pages, events, and others. Punctuation and possessive endings are removed "
    "before the search, so 1.5 is searched as 1 and 5."
)
# checked 2026-11-26: "Ellis Act" stood in the summary of news page 110692 and not in its
# title, and the search returned only a 2022 page with both words in its title. "mayor"
# returned six agency pages and "mayor office" the five of them whose titles hold both
# words, including Office of the Mayor; "mayor's office" returned three and left Office of
# the Mayor out. "point-in-time count" and "point in time count" both returned 9 pages.
SEARCH_NO_AGENCY_WARNING = (
    "The interface refuses to combine a title search with an agency, so this search covers "
    "all of SF.gov. For one agency's news or meetings, use the SF.gov news or meeting "
    "lookup and read the titles."
)
# checked 2026-11-26: search together with primary_agency answered HTTP 400, "cannot
# filter by 'primary_agency_id' while searching (field is not indexed)".
FIRST_PUBLISHED_WARNING = (
    "first_published_at is a timestamp SF.gov stores for the page. It is empty on many "
    "pages, several pages can share one value, and it is not the date of the event or "
    "record the page describes."
)
# checked 2026-11-26: 115 of 200 Police Commission meeting pages had first_published_at
# empty, and 12 pages for meetings from June to December 2026 shared the value
# 2026-05-01T13:04:49.
DOCUMENT_SEARCH_WARNING = (
    "The search matches words in a document's title or description, and every word must "
    "match. Rows come in the interface's own order. A document row names no agency."
)
# checked 2026-11-26: "sheltered challenges" returned two documents (ids 31928 and 32004)
# whose titles lack "challenges" and whose descriptions hold it.
DOCUMENT_DATE_WARNING = (
    "published_date is set by staff and is often empty. uploaded_at is when the file was "
    "added to SF.gov's document library, which can be years after the document was issued: "
    "a March 2022 report carried 2024-11-22. Take a document's date from the document "
    "itself."
)
# checked 2026-11-26: document 3603, a March 2022 report, had published_date 2022-03-16
# and created_at 2024-11-22; document 4387 had published_date empty.
NO_SEARCH_MATCH_WARNING = (
    "Nothing matched. Before you report that SF.gov has no such page or file, try fewer "
    "words or the plain form of each word."
)

LINK_RULE = (
    "Link each row to its page_url or file_url, and each meeting document to its url, the "
    "addresses a reader can open. Name SF.gov as the source."
)


@dataclass(frozen=True)
class _Agency:
    page_id: int
    title: str
    page_url: str


def _invalid(argument: str | None, message: str) -> SourceQueryError:
    return SourceQueryError(
        f"Invalid {argument or 'request'}",
        code="INVALID_QUERY_ARGUMENT",
        argument=argument,
        public_message=message,
    )


def _has_unsafe_character(text: str) -> bool:
    return any(unicodedata.category(character) in _UNSAFE_CATEGORIES for character in text)


def _date(argument: str, value: Any) -> date:
    text = value.strip() if isinstance(value, str) else ""
    if not _DATE.fullmatch(text):
        raise _invalid(argument, f"Give {argument} as YYYY-MM-DD.")
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise _invalid(argument, f"{argument} is not a real calendar date.") from exc
    if not EARLIEST_DATE <= day <= LATEST_DATE:
        raise _invalid(
            argument,
            f"Give {argument} between {EARLIEST_DATE.isoformat()} and {LATEST_DATE.isoformat()}.",
        )
    return day


def _date_range(start_date: Any, end_date: Any) -> tuple[date, date]:
    first = _date("start_date", start_date)
    last = _date("end_date", end_date)
    if last < first:
        raise _invalid("end_date", "end_date must be on or after start_date.")
    return first, last


def _search_words(value: str) -> str:
    """The words as the interface matches them: no possessive endings, no punctuation.

    checked 2026-11-26: "mayor office" matched Office of the Mayor and "mayor's office" did
    not, so a possessive ending narrows a search without the caller meaning it to.
    """
    return " ".join(_NOT_WORD.sub(" ", _POSSESSIVE.sub("", value)).split())


def _plain(value: Any) -> str | None:
    """The input as one line of composed characters, or None when it cannot be used."""
    if not isinstance(value, str) or len(value) > MAX_RAW_INPUT_LENGTH:
        return None
    text = " ".join(unicodedata.normalize("NFC", value).split())
    return None if _has_unsafe_character(text) else text


def _has_two_letters_or_digits(text: str) -> bool:
    return sum(character.isalnum() for character in text) >= MIN_TEXT_LENGTH


def validate_agency(value: Any) -> str:
    """Return the agency name with whitespace collapsed, or raise before any request."""
    name = _plain(value)
    if (
        name is None
        or not MIN_TEXT_LENGTH <= len(name) <= MAX_AGENCY_LENGTH
        or not _has_two_letters_or_digits(_search_words(name))
    ):
        raise _invalid(
            "agency",
            f"Give agency as the name SF.gov uses for the department or public body, "
            f"{MIN_TEXT_LENGTH} to {MAX_AGENCY_LENGTH} characters, for example Police "
            "Commission.",
        )
    return name


def validate_text(value: Any) -> str:
    """Return the search words the interface will receive, or raise before any request."""
    text = _plain(value)
    words = _search_words(text) if text is not None else ""
    if not MIN_TEXT_LENGTH <= len(words) <= MAX_TEXT_LENGTH or not _has_two_letters_or_digits(
        words
    ):
        raise _invalid(
            "text",
            f"Give text as {MIN_TEXT_LENGTH} to {MAX_TEXT_LENGTH} characters of plain words "
            "from the title you are looking for.",
        )
    return words


def validate_kind(value: Any) -> str:
    kind = value.strip().lower() if isinstance(value, str) else None
    if kind not in SEARCH_KINDS:
        raise _invalid(
            "kind",
            "Give kind as pages, for page titles, or documents, for files in SF.gov's "
            "document library.",
        )
    return kind


def _same_name(left: str, right: str) -> bool:
    """Equal apart from case, repeated whitespace, and curly or straight apostrophes."""

    def normalized(text: str) -> str:
        return " ".join(text.replace("’", "'").casefold().split())

    return normalized(left) == normalized(right)


def _schema_hash(kind: str, fields: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps({"kind": kind, "fields": list(fields)}, separators=(",", ":")).encode()
    ).hexdigest()


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _listing(payload: Any, what: str) -> tuple[list[dict[str, Any]], int]:
    """Return a list answer's items and its total count, or raise on any other shape."""
    meta = payload.get("meta") if isinstance(payload, dict) else None
    items = payload.get("items") if isinstance(payload, dict) else None
    total = _integer(meta.get("total_count")) if isinstance(meta, dict) else None
    if not isinstance(items, list) or total is None or total < 0:
        raise SourceError(f"The {SOURCE_NAME} returned no usable {what}")
    if any(not isinstance(item, dict) for item in items) or len(items) > total:
        raise SourceError(f"The {SOURCE_NAME} returned a malformed {what}")
    return items, total


def _page_url(html_url: Any) -> str:
    """The public www.sf.gov address of a page, from the address the interface reports.

    checked 2026-11-26: the interface reports http://api.sf.gov/<path>/, and the same path
    on www.sf.gov answered HTTP 200 for a news page, a meeting page, an agency page, and a
    news page nested under its agency; a made-up path answered 404.
    """
    try:
        parts = urlsplit(html_url) if _is_clean_link(html_url) else None
    except ValueError:
        parts = None
    path = parts.path if parts is not None else ""
    if (
        parts is None
        or parts.scheme not in {"http", "https"}
        or parts.netloc != SFGOV_HOST
        or parts.query
        or parts.fragment
        or not path.startswith("/")
        or "//" in path
        or any(segment in {".", ".."} for segment in path.split("/"))
    ):
        raise SourceError(f"The {SOURCE_NAME} returned a page without a usable address")
    # checked 2026-11-26: all 1,030 page addresses read that day (500 news, 330 agencies,
    # 200 meetings) held only letters, digits, hyphens, and slashes. Anything else is
    # percent-encoded, so the address always stays a path on the public site.
    return f"{PUBLIC_SITE}{quote(path.rstrip('/'), safe='/-._~%')}"


def _is_clean_link(link: Any) -> bool:
    """A string of sane length with no space, control character, or link-breaking mark."""
    return (
        isinstance(link, str)
        and len(link) <= MAX_LINK_LENGTH
        and not any(character.isspace() for character in link)
        and not _has_unsafe_character(link)
        and not _LINK_BREAKERS.intersection(link)
    )


def _document_link(document: dict[str, Any]) -> str | None:
    """The document's own link when it sits on a known SF.gov host, else None.

    checked 2026-11-26: each document carried file on media.api.sf.gov and
    meta.download_url on api.sf.gov. The file link comes first: some readers' tools cannot
    fetch addresses on api.sf.gov.
    """
    meta = document.get("meta")
    candidates = (
        (document.get("file"), f"https://{MEDIA_HOST}/"),
        (meta.get("download_url") if isinstance(meta, dict) else None, f"https://{SFGOV_HOST}/"),
    )
    for link, prefix in candidates:
        if _is_clean_link(link) and link.startswith(prefix):
            return link
    return None


def _external_link(value: Any) -> str | None:
    """A link staff typed into a page, kept only when it is a plain public web address."""
    if not _is_clean_link(value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or "@" in parts.netloc or parts.port:
            return None
        # A bare public hostname: no IP literal, no localhost.
        normalize_domain(parts.hostname or "")
    except ValueError:
        return None
    return value


def _iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _news_row(item: dict[str, Any]) -> tuple[date, dict[str, Any]]:
    # checked 2026-11-26: a news list item carries its id inside meta, unlike other lists.
    meta = item.get("meta")
    page_id = _integer(meta.get("id")) if isinstance(meta, dict) else None
    title = _text(item.get("title"))
    day = _iso_date(item.get("date"))
    if not isinstance(meta, dict) or page_id is None or title is None or day is None:
        raise SourceError(f"The {SOURCE_NAME} returned a malformed news item")
    news_type = item.get("news_type")
    abstract = item.get("abstract")
    if not isinstance(news_type, str | None) or not isinstance(abstract, str | None):
        raise SourceError(f"The {SOURCE_NAME} returned a malformed news item")
    return day, {
        "date": day.isoformat(),
        "title": title,
        "news_type": news_type,
        "abstract": abstract,
        "page_url": _page_url(meta.get("html_url")),
        "page_id": page_id,
    }


@dataclass(frozen=True)
class _Span:
    """One date block of a meeting page: its first day, last day, and start time."""

    start: date
    end: date
    start_time: str | None


def _meeting_dates(blocks: Any) -> list[_Span]:
    """Each date block as a span of days, or no spans when any block is unreadable.

    Staff fill these blocks in by hand. A page with a block this lookup cannot read is
    treated like a page with no date: it is listed, never placed in or out of a date range.
    """
    if not isinstance(blocks, list):
        raise SourceError(f"The {SOURCE_NAME} returned a meeting page without a date list")
    spans = []
    for block in blocks:
        value = block.get("value") if isinstance(block, dict) else None
        if not isinstance(value, dict) or block.get("type") != "date_time":
            return []
        day = _iso_date(value.get("start_date"))
        last_day = _iso_date(value.get("end_date"))
        all_day = value.get("is_all_day")
        start_time = value.get("start_time")
        if (
            day is None
            or (last_day is None and value.get("end_date") is not None)
            or not isinstance(all_day, bool)
            or not (
                start_time is None or (isinstance(start_time, str) and _TIME.fullmatch(start_time))
            )
        ):
            return []
        # checked 2026-11-26: of 199 Police Commission blocks, 13 had an end_date after the
        # start_date, each on a page whose title names a span of dates, whatever the
        # include_end_date_time flag said; 32 had an end_date before the start_date, left
        # over from an earlier meeting. 23 were all-day and still carried a start_time.
        end = last_day if last_day is not None and last_day > day else day
        spans.append(_Span(day, end, None if all_day else start_time))
    return spans


def _meeting_documents(blocks: Any) -> tuple[list[dict[str, Any]], int]:
    """The files attached to a meeting page, and how many entries could not be read."""
    if not isinstance(blocks, list):
        raise SourceError(f"The {SOURCE_NAME} returned a meeting page without a document list")
    documents: list[dict[str, Any]] = []
    unread = 0
    for block in blocks:
        value = block.get("value") if isinstance(block, dict) else None
        files = value.get("documents") if isinstance(value, dict) else None
        if not isinstance(block, dict) or block.get("type") != "downloadable_files":
            unread += 1
            continue
        if not isinstance(value, dict) or not isinstance(files, list):
            raise SourceError(f"The {SOURCE_NAME} returned a malformed document group")
        group = _text(value.get("title"))
        for entry in files:
            document = entry.get("value") if isinstance(entry, dict) else None
            link = _document_link(document) if isinstance(document, dict) else None
            if not isinstance(document, dict) or link is None:
                unread += 1
                continue
            documents.append({"group": group, "title": _text(document.get("title")), "url": link})
    return documents, unread


def _meeting_videos(blocks: Any) -> tuple[list[dict[str, Any]], int]:
    """The video links a meeting page lists, and how many entries could not be read."""
    if not isinstance(blocks, list):
        raise SourceError(f"The {SOURCE_NAME} returned a meeting page without a video list")
    videos: list[dict[str, Any]] = []
    unread = 0
    for block in blocks:
        value = block.get("value") if isinstance(block, dict) else None
        sources = value.get("video_type") if isinstance(value, dict) else None
        if not isinstance(value, dict) or not isinstance(sources, list):
            unread += 1
            continue
        for source in sources:
            detail = source.get("value") if isinstance(source, dict) else None
            link = _external_link(detail.get("url")) if isinstance(detail, dict) else None
            if not isinstance(source, dict) or source.get("type") != "external_link" or not link:
                unread += 1
                continue
            videos.append({"title": _text(value.get("title")), "url": link})
    return videos, unread


@dataclass(frozen=True)
class _Meeting:
    dates: list[_Span]
    row: dict[str, Any]
    unread: int


def _meeting(item: dict[str, Any]) -> _Meeting:
    meta = item.get("meta")
    page_id = _integer(item.get("id"))
    title = _text(item.get("title"))
    cancelled = item.get("cancelled")
    if (
        not isinstance(meta, dict)
        or page_id is None
        or title is None
        or not isinstance(cancelled, bool)
        or meta.get("type") != "sf.Meeting"
    ):
        raise SourceError(f"The {SOURCE_NAME} returned a malformed meeting page")
    dates = _meeting_dates(item.get("date_time"))
    documents, unread_documents = _meeting_documents(item.get("related_documents"))
    videos, unread_videos = _meeting_videos(item.get("videos"))
    row = {
        "meeting_date": None,
        "end_date": None,
        "start_time": None,
        "other_dates": [],
        "title": title,
        "cancelled": cancelled,
        "page_url": _page_url(meta.get("html_url")),
        "documents": documents,
        "video_links": videos,
        "page_id": page_id,
    }
    return _Meeting(dates=dates, row=row, unread=unread_documents + unread_videos)


def _page_search_row(item: dict[str, Any]) -> dict[str, Any]:
    meta = item.get("meta")
    page_id = _integer(item.get("id"))
    title = _text(item.get("title"))
    page_type = _text(meta.get("type")) if isinstance(meta, dict) else None
    if not isinstance(meta, dict) or page_id is None or title is None or page_type is None:
        raise SourceError(f"The {SOURCE_NAME} returned a malformed page")
    published = meta.get("first_published_at")
    if not isinstance(published, str | None):
        raise SourceError(f"The {SOURCE_NAME} returned a malformed page")
    return {
        "title": title,
        "page_type": page_type,
        "page_url": _page_url(meta.get("html_url")),
        "first_published_at": published,
        "page_id": page_id,
    }


def _document_search_row(item: dict[str, Any]) -> dict[str, Any]:
    document_id = _integer(item.get("id"))
    title = _text(item.get("title"))
    link = _document_link(item)
    published = item.get("published_date")
    uploaded = item.get("created_at")
    if (
        document_id is None
        or title is None
        or link is None
        or not isinstance(published, str | None)
        or not isinstance(uploaded, str | None)
    ):
        raise SourceError(f"The {SOURCE_NAME} returned a malformed document")
    return {
        "title": title,
        "published_date": published,
        "uploaded_at": uploaded,
        "file_url": link,
        "document_id": document_id,
    }


class SfGov:
    """List SF.gov news and meeting pages and search SF.gov titles."""

    host = SFGOV_HOST

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        min_request_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = FixedHostClient(source_name=SOURCE_NAME, client=client)
        self._min_interval = min_request_interval
        self._clock = clock
        self._sleep = sleep
        self._next_start = 0.0
        self._lookup_lock = asyncio.Lock()

    def _one_at_a_time(
        self, run: Callable[[], Awaitable[Lookup]]
    ) -> Callable[[], Awaitable[Lookup]]:
        """Run lookups one after another, so a caller that gives up in the queue sent nothing."""

        async def queued() -> Lookup:
            async with self._lookup_lock:
                return await run()

        return queued

    async def _get(self, url: str, params: dict[str, str]) -> Any:
        """Send one request, at least the interval after the one before it ended."""
        wait = self._next_start - self._clock()
        if wait > 0:
            await self._sleep(wait)
        try:
            payload = await self._http.json("GET", url, params=params)
            # A lone surrogate from a JSON escape would fail later, when the snapshot is
            # written, so it is refused here.
            json.dumps(payload, ensure_ascii=False).encode()
        except (UnicodeEncodeError, RecursionError) as exc:
            raise SourceError(f"The {SOURCE_NAME} returned text that cannot be stored") from exc
        finally:
            self._next_start = self._clock() + self._min_interval
        return payload

    async def _agency(self, name: str) -> _Agency:
        """Return the one agency page titled exactly as named, or raise with candidates.

        Matching ignores only case, repeated whitespace, and the style of an apostrophe. It
        never picks among near matches.
        """
        params = {
            "type": "sf.Agency",
            "locale": "en",
            "search": _search_words(name),
            "limit": str(AGENCY_CANDIDATES),
        }
        items, total = _listing(await self._get(PAGES_URL, params), "agency list")
        candidates: list[_Agency] = []
        for item in items:
            meta = item.get("meta")
            page_id = _integer(item.get("id"))
            title = _text(item.get("title"))
            if (
                not isinstance(meta, dict)
                or page_id is None
                or title is None
                or meta.get("type") != "sf.Agency"
            ):
                raise SourceError(f"The {SOURCE_NAME} returned a malformed agency page")
            candidates.append(_Agency(page_id, title, _page_url(meta.get("html_url"))))
        exact = [agency for agency in candidates if _same_name(agency.title, name)]
        if len(exact) == 1:
            return exact[0]
        if exact:
            # checked 2026-11-26: no two of the 330 English agency pages shared a title.
            raise SourceQueryError(
                f"Agency {name!r} matches {len(exact)} pages",
                code="AMBIGUOUS_AGENCY",
                argument="agency",
                public_message=(
                    f"SF.gov has {len(exact)} agency pages titled {name}, so this lookup "
                    "cannot tell which one is meant."
                ),
            )
        shown = candidates[:MAX_NAMED_CANDIDATES]
        if shown:
            count = f"{len(shown)} of {total}" if total > len(shown) else f"{total}"
            scope = (
                f"None of the first {len(candidates)} agency pages SF.gov returned for those "
                f"words is titled exactly {name}"
                if total > len(candidates)
                else f"SF.gov has no agency page titled exactly {name}"
            )
            found = (
                f"{scope}. Agency pages whose "
                f"titles hold those words ({count}): "
                f"{'; '.join(agency.title for agency in shown)}. Give one of these names "
                "exactly, or change the words to see other names."
            )
        else:
            found = (
                f"SF.gov has no agency page whose title holds the words in {name}. Use "
                "fewer words, for example one distinctive word of the name, to see the "
                "names SF.gov uses."
            )
        raise SourceQueryError(
            f"Agency {name!r} not found",
            code="NOT_FOUND",
            argument="agency",
            public_message=found,
        )

    def find_news(
        self, *, start_date: str, end_date: str, agency: str | None = None
    ) -> PreparedLookup:
        """Validate one news lookup without touching the network and return it ready to run."""
        first, last = _date_range(start_date, end_date)
        # An empty agency means no agency filter, as an assistant may send "" for "none".
        blank = agency is None or (isinstance(agency, str) and not agency.strip())
        name = None if blank else validate_agency(agency)

        async def run() -> Lookup:
            found = await self._agency(name) if name is not None else None
            params = {"type": "sf.News", "locale": "en"}
            if found is not None:
                params["primary_agency"] = str(found.page_id)
            params |= {"order": "-date", "limit": str(NEWS_READ_LIMIT)}
            items, total = _listing(await self._get(PAGES_URL, params), "news list")
            if total and not items:
                raise SourceError(f"The {SOURCE_NAME} returned an empty page of a news list")
            read = [_news_row(item) for item in items]
            if any(later[0] < earlier[0] for later, earlier in zip(read, read[1:], strict=False)):
                raise SourceError(f"The {SOURCE_NAME} returned news out of date order")
            if len({row["page_id"] for _, row in read}) != len(read):
                raise SourceError(f"The {SOURCE_NAME} returned a news page twice")
            rows = [row for day, row in read if first <= day <= last]
            oldest = read[-1][0] if read else None
            # Newest first: once the oldest item read is older than the range, every item
            # in the range has been read.
            complete = len(read) == total or (oldest is not None and oldest < first)
            scope = found.title if found is not None else "all agencies"
            result = SourceQueryResult(
                dataset_id="pages/sf.News",
                dataset_name=(f"SF.gov news: {scope}, {first.isoformat()} to {last.isoformat()}"),
                domain=SFGOV_HOST,
                source_url=found.page_url if found is not None else NEWS_INDEX_URL,
                query=(
                    f"{urlencode(params)}; rows kept where date is {first.isoformat()} "
                    f"through {last.isoformat()}"
                ),
                query_url=f"{PAGES_URL}?{urlencode(params)}",
                rows=rows,
                returned_row_count=len(rows),
                completeness=Completeness.COMPLETE if complete else Completeness.TRUNCATED,
                schema_sha256=_schema_hash("news", NEWS_FIELDS),
            )
            warnings = []
            if not complete and oldest is not None:
                warnings.append(
                    f"PARTIAL: this lookup read the {len(read):,} newest of {total:,} news "
                    f"pages, back to {oldest.isoformat()}. News dated {oldest.isoformat()} "
                    "or earlier may be missing from this result. "
                    + (
                        "This lookup cannot reach further back for this agency."
                        if found is not None
                        else "Ask again with an agency to reach further back."
                    )
                )
            elif not rows:
                warnings.append(NO_NEWS_WARNING)
            warnings.append(NEWS_AGENCY_WARNING if found is not None else NEWS_NO_AGENCY_WARNING)
            warnings += [NEWS_TYPE_WARNING, ENGLISH_ONLY_WARNING, UNDOCUMENTED_WARNING]
            return Lookup(result=result, row_grain=NEWS_GRAIN, warnings=warnings, rules=[LINK_RULE])

        return PreparedLookup(
            host=SFGOV_HOST,
            arguments={
                "agency": name,
                "start_date": first.isoformat(),
                "end_date": last.isoformat(),
            },
            run=self._one_at_a_time(run),
        )

    def find_meetings(self, *, agency: str, start_date: str, end_date: str) -> PreparedLookup:
        """Validate one meeting lookup without touching the network and return it."""
        name = validate_agency(agency)
        first, last = _date_range(start_date, end_date)

        async def run() -> Lookup:
            found = await self._agency(name)
            params = {
                "type": "sf.Meeting",
                "locale": "en",
                "primary_agency": str(found.page_id),
                "order": "-id",
                "fields": MEETING_FIELDS_PARAM,
                "limit": str(MEETING_PAGE_SIZE),
            }
            # checked 2026-11-26: date_time is a list of blocks, order=-date_time did not
            # return pages in date order, and the interface refused a range filter on the
            # news date (date__gte). So the lookup reads the body's pages newest id first
            # and keeps the ones dated in the range.
            items: list[dict[str, Any]] = []
            total = 0
            for page in range(MAX_MEETING_PAGES):
                # Each request after the first starts on the last page already read. If
                # that page is not where it was, the list moved and the read is void.
                offset = {"offset": str(len(items) - 1)} if items else {}
                batch, count = _listing(await self._get(PAGES_URL, params | offset), "meeting list")
                if page and (
                    count != total or not batch or batch[0].get("id") != items[-1].get("id")
                ):
                    raise SourceError(
                        f"The {SOURCE_NAME} meeting list changed while it was read. Try again."
                    )
                total = count
                fresh = batch[1:] if page else batch
                items.extend(fresh)
                if len(items) >= total:
                    break
                if not fresh:
                    raise SourceError(f"The {SOURCE_NAME} returned an empty page of a meeting list")
            meetings = [_meeting(item) for item in items]
            ids = [meeting.row["page_id"] for meeting in meetings]
            if any(later >= earlier for earlier, later in zip(ids, ids[1:], strict=False)):
                raise SourceError(f"The {SOURCE_NAME} returned meeting pages out of id order")
            complete = len(meetings) == total

            dated: list[tuple[date, str, dict[str, Any]]] = []
            undated: list[dict[str, Any]] = []
            unread = 0
            for meeting in meetings:
                if not meeting.dates:
                    undated.append(meeting.row)
                    unread += meeting.unread
                    continue
                inside = [
                    span for span in meeting.dates if span.start <= last and span.end >= first
                ]
                if not inside:
                    continue
                shown = inside[0]
                row = meeting.row | {
                    "meeting_date": shown.start.isoformat(),
                    "end_date": shown.end.isoformat() if shown.end > shown.start else None,
                    "start_time": shown.start_time,
                    "other_dates": [
                        span.start.isoformat() for span in meeting.dates if span is not shown
                    ],
                }
                dated.append((shown.start, shown.start_time or "", row))
                unread += meeting.unread
            dated.sort(key=lambda entry: (entry[0], entry[1], entry[2]["page_id"]), reverse=True)
            rows = [row for _, _, row in dated] + undated

            result = SourceQueryResult(
                dataset_id="pages/sf.Meeting",
                dataset_name=(
                    f"SF.gov meeting pages: {found.title}, {first.isoformat()} to "
                    f"{last.isoformat()}"
                ),
                domain=SFGOV_HOST,
                source_url=found.page_url,
                query=(
                    f"{urlencode(params)}; read {MEETING_PAGE_SIZE} pages at a time by "
                    f"offset; rows kept where the page's dates reach into "
                    f"{first.isoformat()} through {last.isoformat()}, or the page has no "
                    "readable date"
                ),
                query_url=f"{PAGES_URL}?{urlencode(params)}",
                rows=rows,
                returned_row_count=len(rows),
                completeness=Completeness.COMPLETE if complete else Completeness.TRUNCATED,
                schema_sha256=_schema_hash("meetings", MEETING_FIELDS),
            )
            warnings = []
            if not complete:
                warnings.append(
                    f"PARTIAL: {found.title} has {total:,} meeting pages and this lookup "
                    f"read the {len(meetings):,} with the highest page ids. The interface "
                    "does not sort them by meeting date, so a meeting in these dates can be "
                    "missing from this result. Check the body's page on SF.gov."
                )
            if not dated:
                warnings.append(NO_MEETINGS_WARNING)
            if undated:
                warnings.append(
                    f"{len(undated)} meeting page(s) of this body carry no date this lookup "
                    "could read, so they could not be placed inside or outside these dates. "
                    "They are listed last with meeting_date empty. Do not count them as "
                    "meetings in the range."
                )
            if unread:
                warnings.append(
                    f"{unread} attachment or video entr{'y' if unread == 1 else 'ies'} on "
                    "these pages had a form this lookup does not read and are missing from "
                    "documents or video_links. Open the meeting page for the full list."
                )
            if any(row["end_date"] for row in rows):
                warnings.append(MEETING_SPAN_WARNING)
            warnings += [
                MEETING_DOCUMENTS_WARNING,
                MEETING_AGENDA_WARNING,
                MEETING_CANCELLED_WARNING,
                MEETING_VIDEO_WARNING,
                MEETING_BODY_WARNING,
                ENGLISH_ONLY_WARNING,
                UNDOCUMENTED_WARNING,
            ]
            return Lookup(
                result=result, row_grain=MEETING_GRAIN, warnings=warnings, rules=[LINK_RULE]
            )

        return PreparedLookup(
            host=SFGOV_HOST,
            arguments={
                "agency": name,
                "start_date": first.isoformat(),
                "end_date": last.isoformat(),
            },
            run=self._one_at_a_time(run),
        )

    def search(self, *, text: str, kind: str = "pages") -> PreparedLookup:
        """Validate one title search without touching the network and return it."""
        words = validate_text(text)
        chosen = validate_kind(kind)

        async def run() -> Lookup:
            if chosen == "pages":
                url, params = PAGES_URL, {"search": words, "locale": "en"}
                what, fields, grain = "page search", PAGE_SEARCH_FIELDS, PAGE_SEARCH_GRAIN
                build = _page_search_row
            else:
                url, params = DOCUMENTS_URL, {"search": words}
                what, fields, grain = (
                    "document search",
                    DOCUMENT_SEARCH_FIELDS,
                    DOCUMENT_SEARCH_GRAIN,
                )
                build = _document_search_row
            params["limit"] = str(SEARCH_LIMIT)
            items, total = _listing(await self._get(url, params), what)
            if total and not items:
                raise SourceError(f"The {SOURCE_NAME} returned an empty page of a {what}")
            rows = [build(item) for item in items]
            result = SourceQueryResult(
                dataset_id=chosen,
                dataset_name=(
                    f"SF.gov {'page title' if chosen == 'pages' else 'document'} search for "
                    f"{words}: {total:,} match"
                ),
                domain=SFGOV_HOST,
                source_url=PUBLIC_SITE,
                query=urlencode(params),
                query_url=f"{url}?{urlencode(params)}",
                rows=rows,
                returned_row_count=len(rows),
                completeness=(
                    Completeness.TRUNCATED if total > len(rows) else Completeness.COMPLETE
                ),
                schema_sha256=_schema_hash(f"search-{chosen}", fields),
            )
            warnings = [] if rows else [NO_SEARCH_MATCH_WARNING]
            if total > len(rows):
                warnings.append(
                    f"PARTIAL: {total:,} {chosen} match and this result shows the "
                    f"interface's first {len(rows)}. Add a word to narrow the search."
                )
            if chosen == "pages":
                warnings += [PAGE_SEARCH_WARNING, SEARCH_NO_AGENCY_WARNING]
                warnings += [FIRST_PUBLISHED_WARNING, ENGLISH_ONLY_WARNING]
            else:
                warnings += [DOCUMENT_SEARCH_WARNING, DOCUMENT_DATE_WARNING]
            warnings.append(UNDOCUMENTED_WARNING)
            return Lookup(result=result, row_grain=grain, warnings=warnings, rules=[LINK_RULE])

        return PreparedLookup(
            host=SFGOV_HOST, arguments={"text": words, "kind": chosen}, run=self._one_at_a_time(run)
        )

    async def close(self) -> None:
        await self._http.close()

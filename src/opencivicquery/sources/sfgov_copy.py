"""A daily copy of SF.gov news and meeting pages, for a server that cannot reach the site.

`opencivicquery sfgov-copy` runs once a day on a computer that can reach SF.gov. For a list
of agencies it reads their English news pages with each page's text, their meeting pages with
the agenda text, and the files attached to those meeting pages. It writes one compressed copy
to the server's snapshot storage under `sfgov-copy/`. It reads with the same module, the same
identity, and the same pace as the live tools, and after the first run it asks only for news
pages that are new or whose listing changed.

The server's SF.gov tools answer from the copy when the site cannot be reached. Every
such answer names the copy's date and the agencies it covers, says what it leaves out, and is
never marked complete.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from opencivicquery import PROJECT_URL
from opencivicquery.models import Completeness, SourceQueryResult
from opencivicquery.sources.base import Lookup, SourceError, SourceQueryError
from opencivicquery.sources.sfgov import (
    _DATE,
    DOCUMENT_DATE_WARNING,
    DOCUMENT_SEARCH_FIELDS,
    DOCUMENT_SEARCH_GRAIN,
    LINK_RULE,
    MEDIA_HOST,
    MEETING_FIELDS,
    MEETING_FIELDS_PARAM,
    MEETING_GRAIN,
    NEWS_AGENCY_WARNING,
    NEWS_FIELDS,
    NEWS_GRAIN,
    NEWS_INDEX_URL,
    NEWS_NO_AGENCY_WARNING,
    NEWS_TYPE_WARNING,
    NO_MEETINGS_WARNING,
    PAGE_SEARCH_FIELDS,
    PAGE_SEARCH_GRAIN,
    PAGE_TEXT_FIELDS,
    PAGE_TEXT_GRAIN,
    PAGE_TEXT_RULE,
    PAGE_TEXT_WARNING,
    PAGES_URL,
    PUBLIC_SITE,
    SEARCH_LIMIT,
    SFGOV_HOST,
    SOURCE_NAME,
    UNDOCUMENTED_WARNING,
    SfGov,
    _agenda_text,
    _capped,
    _cut_warning,
    _document_link,
    _external_link,
    _filter_meetings,
    _integer,
    _is_clean_link,
    _listing,
    _Meeting,
    _meeting,
    _meeting_dates,
    _meeting_warnings,
    _news_row,
    _same_name,
    _schema_hash,
    _search_words,
    _Span,
    _text,
    _unread_warning,
    clean_text,
    html_to_text,
    meeting_page_notes,
    no_text_warning,
)

logger = logging.getLogger(__name__)

COPY_OBJECT = "sfgov-copy/current.json.gz"
"""Where the copy lives inside the snapshot storage. The bucket's lifecycle rule deletes only
`temporary/`, so the copy stays until the next run replaces it."""

DEFAULT_AGENCIES = (
    "Office of the Mayor",
    "Controller's Office",
    "Board of Supervisors",
    "Department of Public Health",
    "City Attorney",
    "Homelessness and Supportive Housing",
    "Police Commission",
    "San Francisco Health Commission",
    "Board of Appeals",
)
# checked 2026-11-26: these are the exact titles of the agencies' SF.gov agency pages.

NEWS_LIST_PAGE_SIZE = 500
MEETING_LIST_PAGE_SIZE = 50
# checked 2026-11-27: 20 Police Commission meeting pages with their agenda text came to 403 KB,
# about 10 KB a page, so 50 pages stay near half a megabyte, well inside the 4 MB answer cap.
SYNC_MAX_PAGES = 100
SECONDS_PER_REQUEST = 1.3
# The one-second spacing plus about 0.3 seconds of answer time measured on 2026-11-26.
COPY_CACHE_SECONDS = 600.0
# Our own bound: a server process reads the copy again after ten minutes, so a new daily copy
# is used the same morning without a download on every call.
MAX_COPY_BYTES = 40 * 1024 * 1024
MAX_STORED_BYTES = 16 * 1024 * 1024
# Our own bounds on the unpacked and the stored copy, so a damaged or oversized file cannot
# exhaust a 512 MiB server: reading takes about six times the unpacked size at its peak. A
# full copy of the nine agencies came to about 14 MB unpacked in a test on 2026-11-27.
MAX_TEXT_FAILURES_IN_A_ROW = 5
# Our own bound: one news page whose text cannot be read is skipped, but five in a row mean
# the site stopped answering, and the agency's read stops instead of waiting out every page.
# The extension is published as this repository's latest release.
EXTENSION_URL = f"{PROJECT_URL}/releases/latest"

NO_COPY_MEETINGS_WARNING = (
    "No meeting page in the copy matched these dates. This shows only what SF.gov listed for "
    "this body when the copy was taken. San Francisco bodies also publish meetings on other "
    "sites, such as the city's Legistar calendar, so it does not show that the body did not "
    "meet."
)
COPY_SEARCH_PAGES_WARNING = (
    "From the copy, the search matches whole words in the titles of the copied news and "
    "meeting pages, and every word must match. The live search also covers reports, agency "
    "pages, events, and other kinds of page. Rows come newest first. The copy does not keep "
    "first_published_at."
)
COPY_SEARCH_DOCUMENTS_WARNING = (
    "From the copy, the search matches whole words in the title or description of the files "
    "attached to the copied meeting pages, and every word must match. The live search covers "
    "every file in SF.gov's document library. A document row names no agency."
)
NO_COPY_MATCH_WARNING = (
    "Nothing in the copy matched. The copy covers only the agencies named above, so this "
    "does not show that SF.gov has no such page or file."
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _public_page(url: str) -> str:
    if not (_is_clean_link(url) and url.startswith(f"{PUBLIC_SITE}/")):
        raise ValueError("not a page on www.sf.gov")
    return url


def _sfgov_file(url: str) -> str:
    if not (
        _is_clean_link(url) and url.startswith((f"https://{MEDIA_HOST}/", f"https://{SFGOV_HOST}/"))
    ):
        raise ValueError("not a file on an SF.gov host")
    return url


def _public_link(url: str) -> str:
    if _external_link(url) != url:
        raise ValueError("not a plain public web address")
    return url


class CopyAgency(_Strict):
    page_id: int
    title: str
    page_url: str
    copied_at: datetime
    """When this agency's pages were last read. An agency whose read failed keeps its older
    pages and date."""
    news_total: int
    meetings_total: int
    news_complete: bool
    """Every news page the site listed for the agency is in the copy."""
    meetings_complete: bool

    _page = field_validator("page_url")(_public_page)


class CopyNews(_Strict):
    page_id: int
    agency_id: int
    date: date
    title: str
    news_type: str | None
    abstract: str | None
    page_url: str
    text: str | None
    """The page's text, "" when the page has none, or None when it could not be read.
    get_sfgov_page returns it."""
    text_read_at: datetime | None
    """When the text was read, or None when it could not be. A daily run reads it again only
    when the page's date, title, type label, or summary changed, so it can be older than the
    agency's copied_at."""

    _page = field_validator("page_url")(_public_page)


class CopySpan(_Strict):
    start: date
    end: date
    start_time: str | None


class CopyFile(_Strict):
    group: str | None
    title: str | None
    url: str

    _url = field_validator("url")(_sfgov_file)


class CopyVideo(_Strict):
    title: str | None
    url: str

    _url = field_validator("url")(_public_link)


class CopyMeeting(_Strict):
    page_id: int
    agency_id: int
    title: str
    cancelled: bool
    page_url: str
    spans: list[CopySpan]
    documents: list[CopyFile]
    video_links: list[CopyVideo]
    unread: int
    text: str
    agenda_unread: int
    """Agenda blocks of a form the copy does not read, so their text is missing."""

    _page = field_validator("page_url")(_public_page)


class CopyDocument(_Strict):
    document_id: int
    title: str
    description: str | None
    file_url: str
    published_date: str | None
    uploaded_at: str | None
    page_ids: list[int]

    _file = field_validator("file_url")(_sfgov_file)


class SfGovCopy(_Strict):
    format: Literal[2] = 2
    """2 since news text carries its read date and meeting pages their unread agenda blocks.
    A copy of another format is refused, and the next daily run reads everything again."""
    copied_at: datetime
    agencies: list[CopyAgency] = Field(min_length=1)
    news: list[CopyNews]
    meetings: list[CopyMeeting]
    documents: list[CopyDocument]


def encode_copy(copy: SfGovCopy) -> bytes:
    raw = copy.model_dump_json().encode("utf-8")
    return gzip.compress(raw, mtime=0)


def decode_copy(data: bytes) -> SfGovCopy:
    """Unpack and validate a stored copy, or raise SourceError.

    The validation checks every link again, so a copy changed in storage cannot make the
    server pass on links to other sites as SF.gov links."""
    if len(data) > MAX_STORED_BYTES:
        raise SourceError("The stored SF.gov daily copy is larger than this server reads")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as packed:
            raw = packed.read(MAX_COPY_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise SourceError("The SF.gov daily copy could not be unpacked") from exc
    if len(raw) > MAX_COPY_BYTES:
        raise SourceError("The SF.gov daily copy is larger than this server reads")
    try:
        return SfGovCopy.model_validate_json(raw)
    except ValidationError as exc:
        raise SourceError("The SF.gov daily copy is not in the expected form") from exc


class CopyStore(Protocol):
    location: str

    async def read(self) -> bytes | None: ...

    async def write(self, data: bytes) -> str: ...


class LocalCopyStore:
    """The copy in a local snapshot folder."""

    def __init__(self, root: Path) -> None:
        self._path = root.expanduser().resolve() / COPY_OBJECT
        self.location = self._path.as_uri()

    async def read(self) -> bytes | None:
        def load() -> bytes | None:
            try:
                return self._path.read_bytes()
            except FileNotFoundError:
                return None

        return await asyncio.to_thread(load)

    async def write(self, data: bytes) -> str:
        def save() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            partial = self._path.with_name(self._path.name + ".partial")
            with partial.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(partial, self._path)

        await asyncio.to_thread(save)
        return self.location


class GcsCopyStore:
    """The copy in the snapshot bucket, beside the snapshots."""

    def __init__(self, bucket_name: str, *, client: Any = None) -> None:
        from google.cloud import storage

        if not bucket_name or "/" in bucket_name:
            raise SourceError("Invalid bucket name for the SF.gov daily copy")
        self._client = client or storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self._blob = self._bucket.blob(COPY_OBJECT)
        self.location = f"gs://{bucket_name}/{COPY_OBJECT}"

    async def read(self) -> bytes | None:
        from google.api_core.exceptions import NotFound

        def load() -> bytes | None:
            blob = self._bucket.get_blob(COPY_OBJECT)
            if blob is None:
                return None
            if blob.size is not None and blob.size > MAX_STORED_BYTES:
                raise SourceError("The stored SF.gov daily copy is larger than this server reads")
            return blob.download_as_bytes()

        try:
            return await asyncio.to_thread(load)
        except NotFound:
            return None

    async def write(self, data: bytes) -> str:
        await asyncio.to_thread(
            self._blob.upload_from_string, data, content_type="application/gzip"
        )
        return self.location


def copy_store(*, backend: str, path: Path | None, bucket: str | None) -> CopyStore:
    """The copy store that sits in the same place as the server's snapshots."""
    if backend == "gcs":
        if not bucket:
            raise SourceError("A bucket is required for the SF.gov daily copy")
        return GcsCopyStore(bucket)
    if path is None:
        raise SourceError("A folder is required for the SF.gov daily copy")
    return LocalCopyStore(path)


class CopyReader:
    """Read the copy from storage and keep it in memory for a few minutes."""

    def __init__(
        self,
        store: CopyStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = COPY_CACHE_SECONDS,
    ) -> None:
        self._store = store
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._copy: SfGovCopy | None = None
        self._loaded_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def has_copy(self) -> bool:
        """Whether the last read found a copy. Before the first read, False."""
        return self._copy is not None

    async def load(self) -> SfGovCopy | None:
        """The current copy, or None when there is none. A read failure keeps the last copy."""
        async with self._lock:
            now = self._clock()
            if self._loaded_at is not None and now - self._loaded_at < self._cache_seconds:
                return self._copy
            try:
                data = await self._store.read()
                self._copy = decode_copy(data) if data is not None else None
            except Exception:
                logger.warning("The SF.gov daily copy could not be read", exc_info=True)
            self._loaded_at = now
            return self._copy

    async def news(self, *, name: str | None, first: date, last: date) -> Lookup | None:
        copy = await self.load()
        return None if copy is None else news_from_copy(copy, name=name, first=first, last=last)

    async def meetings(self, *, name: str, first: date, last: date) -> Lookup | None:
        copy = await self.load()
        if copy is None:
            return None
        return meetings_from_copy(copy, name=name, first=first, last=last)

    async def search(self, *, words: str, kind: str) -> Lookup | None:
        copy = await self.load()
        return None if copy is None else search_from_copy(copy, words=words, kind=kind)

    async def page(self, *, page_id: int) -> Lookup | None:
        copy = await self.load()
        return None if copy is None else page_from_copy(copy, page_id=page_id)


# Answers from the copy


def _day(moment: datetime) -> str:
    return moment.date().isoformat()


def _resolve(copy: SfGovCopy, name: str) -> CopyAgency:
    for agency in copy.agencies:
        if _same_name(agency.title, name):
            return agency
    covered = "; ".join(agency.title for agency in copy.agencies)
    raise SourceQueryError(
        f"Agency {name!r} not in the SF.gov daily copy",
        code="NOT_FOUND",
        argument="agency",
        public_message=(
            "This server could not get a live answer from SF.gov, so this lookup used the "
            f"daily copy, which covers only these agencies: {covered}. {name} is not one of "
            "them. "
            "For any agency, use the OpenCivicQuery SF.gov extension for Claude Desktop: "
            f"{EXTENSION_URL}"
        ),
    )


def _as_of(agencies: Sequence[CopyAgency]) -> datetime:
    return min(agency.copied_at for agency in agencies)


def _copy_warning(copy: SfGovCopy, scope: Sequence[CopyAgency]) -> str:
    days = sorted({_day(agency.copied_at) for agency in scope})
    if len(days) == 1:
        taken = f"on {days[0]}"
    else:
        each = "; ".join(f"{agency.title} on {_day(agency.copied_at)}" for agency in scope)
        taken = f"between {days[0]} and {days[-1]} ({each})"
    names = "; ".join(agency.title for agency in copy.agencies)
    return (
        "FROM THE DAILY COPY. This server could not get a live answer from SF.gov, so this "
        f"answer comes from a copy of SF.gov taken {taken}. The copy holds the English news and "
        f"meeting pages of {len(copy.agencies)} agencies ({names}) and the files attached to "
        "those meeting pages. It leaves out every other agency, every other kind of page, "
        "translated pages, and anything SF.gov published or changed after the copy was taken."
    )


def _copy_rules(as_of: datetime) -> list[str]:
    day = _day(as_of)
    return [
        f"FROM A DAILY COPY, NOT LIVE. Say in your reply that these SF.gov results come from "
        f"a copy of SF.gov taken on {day}, not from the live site, and do not call them "
        "complete or current. Where a later rule speaks of the source's update time, that "
        "time is when the copy was taken, not an update time SF.gov reported. The copy has "
        "no live-result URL: cite each row's own link instead.",
        LINK_RULE,
        "End your reply with this sentence, word for word: "
        f'"These SF.gov results come from a copy taken on {day}; for live results that cover '
        "every SF.gov agency, use the OpenCivicQuery SF.gov extension for Claude Desktop: "
        f'{EXTENSION_URL}"',
    ]


def _late_range(last: date, scope: Sequence[CopyAgency]) -> str | None:
    """A warning when the range reaches the day the copy was taken or later."""
    day = _as_of(scope).date()
    if last < day:
        return None
    return (
        f"PARTIAL: the copy was taken on {day.isoformat()}, so pages SF.gov published or "
        "changed on or after that day may be missing from this result."
    )


def _incomplete(agencies: Sequence[CopyAgency], *, news: bool) -> str | None:
    missing = [
        agency.title
        for agency in agencies
        if (news and not agency.news_complete) or not agency.meetings_complete
    ]
    if not missing:
        return None
    return (
        "PARTIAL: the copy does not hold every page SF.gov listed for "
        f"{'; '.join(missing)}, so a match can be missing."
    )


def _lookup(
    copy: SfGovCopy,
    scope: Sequence[CopyAgency],
    result: SourceQueryResult,
    grain: str,
    warnings: list[str],
    rules: Sequence[str] = (),
) -> Lookup:
    return Lookup(
        result=result,
        row_grain=grain,
        warnings=[_copy_warning(copy, scope), *warnings],
        rules=[*_copy_rules(_as_of(scope)), *rules],
        escalations=["SFGOV_DAILY_COPY"],
    )


def news_from_copy(copy: SfGovCopy, *, name: str | None, first: date, last: date) -> Lookup:
    agency = _resolve(copy, name) if name is not None else None
    scope = [agency] if agency is not None else copy.agencies
    ids = {entry.page_id for entry in scope}
    items = sorted(
        (item for item in copy.news if item.agency_id in ids and first <= item.date <= last),
        key=lambda item: (item.date, item.page_id),
        reverse=True,
    )
    rows = [
        {
            "date": item.date.isoformat(),
            "title": item.title,
            "news_type": item.news_type,
            "abstract": item.abstract,
            "page_url": item.page_url,
            "page_id": item.page_id,
        }
        for item in items
    ]
    partial = [entry for entry in scope if not entry.news_complete]
    label = agency.title if agency is not None else "all agencies in the copy"
    result = SourceQueryResult(
        dataset_id="sfgov-copy/news",
        dataset_name=(
            f"SF.gov news from the daily copy: {label}, {first.isoformat()} to {last.isoformat()}"
        ),
        domain=SFGOV_HOST,
        source_url=agency.page_url if agency is not None else NEWS_INDEX_URL,
        query=(
            f"daily copy of SF.gov taken {_as_of(scope).isoformat()}; news pages of {label} "
            f"dated {first.isoformat()} through {last.isoformat()}"
        ),
        query_url=None,
        rows=rows,
        returned_row_count=len(rows),
        completeness=(
            Completeness.TRUNCATED if partial or _late_range(last, scope) else Completeness.LIMITED
        ),
        source_updated_at=_as_of(scope).isoformat(),
        schema_sha256=_schema_hash("news", NEWS_FIELDS),
    )
    held = {entry.page_id: 0 for entry in scope}
    for item in copy.news:
        if item.agency_id in held:
            held[item.agency_id] += 1
    warnings = [
        f"PARTIAL: the copy holds {held[entry.page_id]} of the {entry.news_total} news pages "
        f"SF.gov listed for {entry.title}, so news may be missing."
        for entry in partial
    ]
    if late := _late_range(last, scope):
        warnings.append(late)
    if not rows:
        warnings.append(NO_COPY_MATCH_WARNING)
    warnings += [
        NEWS_AGENCY_WARNING if agency is not None else NEWS_NO_AGENCY_WARNING,
        NEWS_TYPE_WARNING,
        UNDOCUMENTED_WARNING,
    ]
    return _lookup(copy, scope, result, NEWS_GRAIN, warnings)


def meetings_from_copy(copy: SfGovCopy, *, name: str, first: date, last: date) -> Lookup:
    agency = _resolve(copy, name)
    meetings = [
        _Meeting(
            dates=[_Span(span.start, span.end, span.start_time) for span in item.spans],
            row={
                "meeting_date": None,
                "end_date": None,
                "start_time": None,
                "other_dates": [],
                "title": item.title,
                "cancelled": item.cancelled,
                "page_url": item.page_url,
                "documents": [document.model_dump() for document in item.documents],
                "video_links": [video.model_dump() for video in item.video_links],
                "page_id": item.page_id,
            },
            unread=item.unread,
        )
        for item in sorted(copy.meetings, key=lambda item: item.page_id, reverse=True)
        if item.agency_id == agency.page_id
    ]
    dated, undated, unread = _filter_meetings(meetings, first, last)
    rows = dated + undated
    result = SourceQueryResult(
        dataset_id="sfgov-copy/meetings",
        dataset_name=(
            f"SF.gov meeting pages from the daily copy: {agency.title}, {first.isoformat()} "
            f"to {last.isoformat()}"
        ),
        domain=SFGOV_HOST,
        source_url=agency.page_url,
        query=(
            f"daily copy of SF.gov taken {agency.copied_at.isoformat()}; meeting pages of "
            f"{agency.title} whose dates reach into {first.isoformat()} through "
            f"{last.isoformat()}, or that have no readable date"
        ),
        query_url=None,
        rows=rows,
        returned_row_count=len(rows),
        completeness=(
            Completeness.LIMITED
            if agency.meetings_complete and not _late_range(last, [agency])
            else Completeness.TRUNCATED
        ),
        source_updated_at=agency.copied_at.isoformat(),
        schema_sha256=_schema_hash("meetings", MEETING_FIELDS),
    )
    warnings = []
    if not agency.meetings_complete:
        warnings.append(
            f"PARTIAL: the copy holds {len(meetings)} of the {agency.meetings_total} meeting "
            f"pages SF.gov listed for {agency.title}, so a meeting in these dates can be "
            "missing."
        )
    if late := _late_range(last, [agency]):
        warnings.append(late)
    warnings += [
        NO_COPY_MEETINGS_WARNING if warning == NO_MEETINGS_WARNING else warning
        for warning in _meeting_warnings(dated, undated, unread)
    ]
    return _lookup(copy, [agency], result, MEETING_GRAIN, warnings)


def _words(text: str) -> set[str]:
    return set(_search_words(text).casefold().split())


def search_from_copy(copy: SfGovCopy, *, words: str, kind: str) -> Lookup:
    wanted = _words(words)
    incomplete = _incomplete(copy.agencies, news=kind == "pages")
    if kind == "pages":
        found: list[tuple[date, int, dict[str, Any]]] = []
        for news in copy.news:
            if wanted <= _words(news.title):
                found.append((news.date, news.page_id, _page_row(news.title, "sf.News", news)))
        for meeting in copy.meetings:
            if wanted <= _words(meeting.title):
                day = min((span.start for span in meeting.spans), default=date.min)
                row = _page_row(meeting.title, "sf.Meeting", meeting)
                found.append((day, meeting.page_id, row))
        found.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        fields, grain, extra = PAGE_SEARCH_FIELDS, PAGE_SEARCH_GRAIN, COPY_SEARCH_PAGES_WARNING
    else:
        found = [
            (
                date.min,
                document.document_id,
                {
                    "title": document.title,
                    "published_date": document.published_date,
                    "uploaded_at": document.uploaded_at,
                    "file_url": document.file_url,
                    "document_id": document.document_id,
                },
            )
            for document in copy.documents
            if wanted <= _words(f"{document.title} {document.description or ''}")
        ]
        found.sort(key=lambda entry: (entry[2]["published_date"] or "", entry[1]), reverse=True)
        fields, grain = DOCUMENT_SEARCH_FIELDS, DOCUMENT_SEARCH_GRAIN
        extra = COPY_SEARCH_DOCUMENTS_WARNING
    rows = [row for _, _, row in found[:SEARCH_LIMIT]]
    result = SourceQueryResult(
        dataset_id=f"sfgov-copy/{kind}",
        dataset_name=(
            f"SF.gov {'page title' if kind == 'pages' else 'document'} search in the daily "
            f"copy for {words}: {len(found):,} match"
        ),
        domain=SFGOV_HOST,
        source_url=PUBLIC_SITE,
        query=(
            f"daily copy of SF.gov taken {_as_of(copy.agencies).isoformat()}; {kind} "
            f"matching {words}"
        ),
        query_url=None,
        rows=rows,
        returned_row_count=len(rows),
        completeness=(
            Completeness.TRUNCATED if len(found) > len(rows) or incomplete else Completeness.LIMITED
        ),
        source_updated_at=_as_of(copy.agencies).isoformat(),
        schema_sha256=_schema_hash(f"search-{kind}", fields),
    )
    warnings = [] if rows else [NO_COPY_MATCH_WARNING]
    if len(found) > len(rows):
        warnings.append(
            f"PARTIAL: {len(found):,} {kind} in the copy match and this result shows the "
            f"newest {len(rows)}. Add a word to narrow the search."
        )
    if incomplete:
        warnings.append(incomplete)
    warnings.append(extra)
    if kind == "documents":
        warnings.append(DOCUMENT_DATE_WARNING)
    warnings.append(UNDOCUMENTED_WARNING)
    return _lookup(copy, copy.agencies, result, grain, warnings)


def page_from_copy(copy: SfGovCopy, *, page_id: int) -> Lookup:
    agencies = {agency.page_id: agency for agency in copy.agencies}
    found: list[CopyNews | CopyMeeting] = [
        item for item in (*copy.news, *copy.meetings) if item.page_id == page_id
    ]
    if not found:
        covered = "; ".join(agency.title for agency in copy.agencies)
        raise SourceQueryError(
            f"Page {page_id} not in the SF.gov daily copy",
            code="NOT_FOUND",
            argument="page_id",
            public_message=(
                "This server could not get a live answer from SF.gov, so this lookup used the "
                f"daily copy, which holds only the English news and meeting pages of: "
                f"{covered}. Page "
                f"{page_id} is not among them. For any page, use the OpenCivicQuery SF.gov "
                f"extension for Claude Desktop: {EXTENSION_URL}"
            ),
        )
    if any(item.agency_id not in agencies for item in found):
        raise SourceError("The SF.gov daily copy names an agency it does not hold")
    # A page that moved to another agency while the old agency's read failed sits in the
    # copy twice; the agency read last holds the current one.
    page = max(found, key=lambda item: agencies[item.agency_id].copied_at)
    agency = agencies[page.agency_id]
    warnings = []
    # The copy is checked again here: a changed copy must not carry control characters out.
    text, cut = _capped(clean_text(page.text))
    read_at = agency.copied_at
    if isinstance(page, CopyNews):
        day: date | None = page.date
        page_type, cancelled = "sf.News", None
        unread = page.text is None or page.text_read_at is None
        if unread:
            warnings.append(
                "PARTIAL: the copy could not read this page's text. Open page_url, or use "
                "the SF.gov extension for Claude Desktop."
            )
        else:
            assert page.text_read_at is not None
            read_at = page.text_read_at
            if read_at.date() != agency.copied_at.date():
                warnings.append(
                    f"The copy read this page's text on {_day(read_at)}. It reads a news "
                    "page's text again only when the page's date, title, type label, or "
                    "summary changes, so a later edit to the text alone is not in the copy."
                )
            if text is None:
                warnings.append(no_text_warning(page_type, copied=True))
    else:
        day = min((span.start for span in page.spans), default=None)
        page_type, cancelled = "sf.Meeting", page.cancelled
        unread = bool(page.agenda_unread)
        if unread:
            warnings.append(_unread_warning(page.agenda_unread))
        elif text is None:
            warnings.append(no_text_warning(page_type, copied=True))
        warnings += meeting_page_notes(page.spans, page.cancelled)
    partial = cut or unread
    if cut:
        warnings.insert(0, _cut_warning())
    row = {
        "page_id": page_id,
        "page_type": page_type,
        "title": page.title,
        "date": day.isoformat() if day is not None else None,
        "cancelled": cancelled,
        "agency": agency.title,
        "page_url": page.page_url,
        "text": text,
    }
    result = SourceQueryResult(
        dataset_id="sfgov-copy/page",
        dataset_name=f"SF.gov page {page_id} from the daily copy: {page.title}",
        domain=SFGOV_HOST,
        source_url=page.page_url,
        query=(
            f"daily copy of SF.gov taken {agency.copied_at.isoformat()}; page {page_id}, text "
            f"read {read_at.isoformat()}"
        ),
        query_url=None,
        rows=[row],
        returned_row_count=1,
        completeness=Completeness.TRUNCATED if partial else Completeness.LIMITED,
        source_updated_at=agency.copied_at.isoformat(),
        schema_sha256=_schema_hash("page-text", PAGE_TEXT_FIELDS),
    )
    warnings += [PAGE_TEXT_WARNING, UNDOCUMENTED_WARNING]
    return _lookup(copy, [agency], result, PAGE_TEXT_GRAIN, warnings, [PAGE_TEXT_RULE])


def _page_row(title: str, page_type: str, page: CopyNews | CopyMeeting) -> dict[str, Any]:
    return {
        "title": title,
        "page_type": page_type,
        "page_url": page.page_url,
        "first_published_at": None,
        "page_id": page.page_id,
    }


# The daily read


@dataclass
class AgencyOutcome:
    """What one agency's read did, for the run's log."""

    name: str
    status: str
    detail: str = ""
    news: int = 0
    news_total: int = 0
    text_requests: int = 0
    text_failures: int = 0
    meetings: int = 0
    meetings_total: int = 0


@dataclass
class _AgencyPart:
    agency: CopyAgency
    news: list[CopyNews] = field(default_factory=list)
    meetings: list[CopyMeeting] = field(default_factory=list)
    documents: dict[int, CopyDocument] = field(default_factory=dict)


def _clean_date(value: Any) -> str | None:
    return value if isinstance(value, str) and _DATE.fullmatch(value) else None


def _documents_of(item: dict[str, Any], page_id: int) -> list[CopyDocument]:
    """The files attached to a meeting page and to its agenda items, as document entries."""
    entries: list[Any] = []
    for group in item.get("related_documents") or []:
        value = group.get("value") if isinstance(group, dict) else None
        if isinstance(value, dict) and isinstance(value.get("documents"), list):
            entries += value["documents"]
    agenda = item.get("agenda")
    for block in agenda if isinstance(agenda, list) else []:
        value = block.get("value") if isinstance(block, dict) else None
        if isinstance(value, dict) and isinstance(value.get("documents"), list):
            entries += value["documents"]
    documents = []
    for entry in entries:
        document = entry.get("value") if isinstance(entry, dict) else None
        if not isinstance(document, dict):
            continue
        document_id = _integer(document.get("id"))
        title = _text(document.get("title"))
        link = _document_link(document)
        if document_id is None or title is None or link is None:
            continue
        created = document.get("created_at")
        documents.append(
            CopyDocument(
                document_id=document_id,
                title=title,
                description=html_to_text(document.get("description")),
                file_url=link,
                published_date=_clean_date(document.get("published_date")),
                uploaded_at=created if isinstance(created, str) else None,
                page_ids=[page_id],
            )
        )
    return documents


async def _news_text(source: SfGov, page_id: int) -> str | None:
    """The text of one news page, read from its own page in the interface: "" when it has
    none, None when the site has no such page."""
    try:
        page = await source._get(f"{PAGES_URL}{page_id}/", {})
    except SourceQueryError as exc:
        if exc.http_status == 404:
            return None
        raise
    meta = page.get("meta") if isinstance(page, dict) else None
    if (
        not isinstance(meta, dict)
        or _integer(page.get("id")) != page_id
        or meta.get("type") != "sf.News"
        or "body" not in page
        or not isinstance(page["body"], str | None)
    ):
        raise SourceError(f"The {SOURCE_NAME} returned a malformed news page")
    # "" records a page read that holds no text, apart from None, a page not read.
    return html_to_text(page.get("body")) or ""


async def _read_agency(
    source: SfGov,
    name: str,
    previous: SfGovCopy | None,
    *,
    sample: int | None,
    now: Callable[[], datetime],
    outcome: AgencyOutcome,
) -> _AgencyPart:
    found = await source._agency(name)
    page_id = str(found.page_id)

    async def read(params: dict[str, str], what: str) -> tuple[list[dict[str, Any]], int]:
        if sample is not None:
            return _listing(await source._get(PAGES_URL, params), what)
        return await source._read_all(params, what, max_pages=SYNC_MAX_PAGES)

    news_params = {
        "type": "sf.News",
        "locale": "en",
        "primary_agency": page_id,
        "order": "-id",
        "limit": str(sample or NEWS_LIST_PAGE_SIZE),
    }
    news_items, news_total = await read(news_params, "list of news pages")
    earlier = {item.page_id: item for item in previous.news} if previous else {}
    part_news = []
    failures_in_a_row = 0
    for item in news_items:
        day, row = _news_row(item)
        old = earlier.get(row["page_id"])
        listing = (day, row["title"], row["news_type"], row["abstract"])
        if (
            old is not None
            and old.text is not None
            and old.text_read_at is not None
            and listing
            == (
                old.date,
                old.title,
                old.news_type,
                old.abstract,
            )
        ):
            text, read_at = old.text, old.text_read_at
        else:
            outcome.text_requests += 1
            try:
                text = await _news_text(source, row["page_id"])
                read_at = now() if text is not None else None
                failures_in_a_row = 0
            except SourceError as exc:
                # A slow-down answer stops the run; one unreadable page does not drop the
                # agency, but several in a row mean the site stopped answering.
                if getattr(exc, "code", None) == "SOURCE_RATE_LIMITED":
                    raise
                failures_in_a_row += 1
                outcome.text_failures += 1
                if failures_in_a_row >= MAX_TEXT_FAILURES_IN_A_ROW:
                    raise
                text, read_at = None, None
        part_news.append(
            CopyNews(
                page_id=row["page_id"],
                agency_id=found.page_id,
                date=day,
                title=row["title"],
                news_type=row["news_type"],
                abstract=row["abstract"],
                page_url=row["page_url"],
                text=text,
                text_read_at=read_at,
            )
        )

    meeting_params = {
        "type": "sf.Meeting",
        "locale": "en",
        "primary_agency": page_id,
        "order": "-id",
        "fields": f"{MEETING_FIELDS_PARAM},agenda,overview",
        "limit": str(sample or MEETING_LIST_PAGE_SIZE),
    }
    meeting_items, meetings_total = await read(meeting_params, "list of meeting pages")
    part_meetings = []
    documents: dict[int, CopyDocument] = {}
    for item in meeting_items:
        parsed = _meeting(item)
        text, agenda_unread = _agenda_text(item)
        part_meetings.append(
            CopyMeeting(
                page_id=parsed.row["page_id"],
                agency_id=found.page_id,
                title=parsed.row["title"],
                cancelled=parsed.row["cancelled"],
                page_url=parsed.row["page_url"],
                spans=[
                    CopySpan(start=span.start, end=span.end, start_time=span.start_time)
                    for span in parsed.dates
                ],
                documents=parsed.row["documents"],
                video_links=parsed.row["video_links"],
                unread=parsed.unread,
                text=text,
                agenda_unread=agenda_unread,
            )
        )
        if agenda_unread:
            logger.info("%s: %d agenda blocks not read", parsed.row["page_id"], agenda_unread)
        for document in _documents_of(item, parsed.row["page_id"]):
            _merge_document(documents, document)

    outcome.news, outcome.news_total = len(part_news), news_total
    outcome.meetings, outcome.meetings_total = len(part_meetings), meetings_total
    agency = CopyAgency(
        page_id=found.page_id,
        title=found.title,
        page_url=found.page_url,
        copied_at=now(),
        news_total=news_total,
        meetings_total=meetings_total,
        news_complete=len(part_news) == news_total,
        meetings_complete=len(part_meetings) == meetings_total,
    )
    return _AgencyPart(agency, part_news, part_meetings, documents)


def _merge_document(documents: dict[int, CopyDocument], document: CopyDocument) -> None:
    known = documents.get(document.document_id)
    if known is None:
        documents[document.document_id] = document
    else:
        known.page_ids = sorted(set(known.page_ids) | set(document.page_ids))


def _previous_part(previous: SfGovCopy | None, name: str) -> _AgencyPart | None:
    if previous is None:
        return None
    agency = next((item for item in previous.agencies if _same_name(item.title, name)), None)
    if agency is None:
        return None
    meetings = [item for item in previous.meetings if item.agency_id == agency.page_id]
    meeting_ids = {item.page_id for item in meetings}
    documents = {
        document.document_id: document.model_copy(
            update={"page_ids": [pid for pid in document.page_ids if pid in meeting_ids]}
        )
        for document in previous.documents
        if meeting_ids.intersection(document.page_ids)
    }
    return _AgencyPart(
        agency=agency,
        news=[item for item in previous.news if item.agency_id == agency.page_id],
        meetings=meetings,
        documents=documents,
    )


async def sync_copy(
    source: SfGov,
    names: Sequence[str],
    previous: SfGovCopy | None,
    *,
    sample: int | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[SfGovCopy | None, list[AgencyOutcome]]:
    """Read each agency and return the new copy, or None when no agency has any data.

    An agency whose read fails keeps its pages from the previous copy, with their older date.
    A slow-down answer from the site stops the run; the agencies not yet read keep theirs.
    """
    parts: list[_AgencyPart] = []
    outcomes: list[AgencyOutcome] = []
    stopped: str | None = None
    unique: list[str] = []
    for name in names:
        if not any(_same_name(name, seen) for seen in unique):
            unique.append(name)
    for name in unique:
        outcome = AgencyOutcome(name=name, status="read")
        outcomes.append(outcome)
        if stopped is None:
            try:
                part = await _read_agency(
                    source, name, previous, sample=sample, now=now, outcome=outcome
                )
                if any(known.agency.page_id == part.agency.page_id for known in parts):
                    outcome.status = "skipped: the same agency page as an earlier name"
                else:
                    parts.append(part)
                continue
            except SourceError as exc:
                message = getattr(exc, "public_message", None) or str(exc)
                outcome.detail = message
                if getattr(exc, "code", None) == "SOURCE_RATE_LIMITED":
                    stopped = message
        else:
            outcome.detail = f"not read: the run stopped earlier ({stopped})"
        kept = _previous_part(previous, name)
        if kept is not None:
            parts.append(kept)
            outcome.status = f"kept the copy of {_day(kept.agency.copied_at)}"
        else:
            outcome.status = "failed, not in the copy"
    if not parts:
        return None, outcomes
    documents: dict[int, CopyDocument] = {}
    for part in parts:
        for document in part.documents.values():
            _merge_document(documents, document.model_copy(deep=True))
    return (
        SfGovCopy(
            copied_at=now(),
            agencies=[part.agency for part in parts],
            news=[item for part in parts for item in part.news],
            meetings=[item for part in parts for item in part.meetings],
            documents=sorted(documents.values(), key=lambda item: item.document_id),
        ),
        outcomes,
    )


async def plan_copy(
    source: SfGov, names: Sequence[str], previous: SfGovCopy | None
) -> list[dict[str, Any]]:
    """Count what a full read would fetch, with four light requests per agency."""
    rows = []
    for name in names:
        found = await source._agency(name)
        base = {"locale": "en", "primary_agency": str(found.page_id), "limit": "1"}
        newest, news_total = _listing(
            await source._get(PAGES_URL, base | {"type": "sf.News", "order": "-date"}), "news"
        )
        oldest, _ = _listing(
            await source._get(PAGES_URL, base | {"type": "sf.News", "order": "date"}), "news"
        )
        first_meeting, meetings_total = _listing(
            await source._get(
                PAGES_URL,
                base | {"type": "sf.Meeting", "order": "id", "fields": "date_time"},
            ),
            "meetings",
        )
        known = (
            sum(
                1
                for item in previous.news
                if item.agency_id == found.page_id and item.text is not None
            )
            if previous
            else 0
        )
        # A lower bound after the first run: pages whose listing changed are read again.
        texts = max(news_total - known, 0)
        requests = (
            1
            + _list_requests(news_total, NEWS_LIST_PAGE_SIZE)
            + texts
            + _list_requests(meetings_total, MEETING_LIST_PAGE_SIZE)
        )
        rows.append(
            {
                "agency": found.title,
                "news_pages": news_total,
                "oldest_news": _news_row(oldest[0])[0].isoformat() if oldest else None,
                "newest_news": _news_row(newest[0])[0].isoformat() if newest else None,
                "meeting_pages": meetings_total,
                "oldest_meeting_page": _first_date(first_meeting),
                "text_requests": texts,
                "requests": requests,
            }
        )
    return rows


def _list_requests(total: int, page_size: int) -> int:
    """Requests to read a list of this many items, each request after the first repeating
    the last item already read."""
    return 1 + max(0, -(-(total - page_size) // (page_size - 1)))


def _first_date(items: list[dict[str, Any]]) -> str | None:
    """The first date on the oldest meeting page by id, as a rough start of the record."""
    spans = _meeting_dates(items[0].get("date_time")) if items else []
    return min(span.start for span in spans).isoformat() if spans else None


def plan_summary(rows: list[dict[str, Any]]) -> str:
    requests = sum(row["requests"] for row in rows)
    minutes = requests * SECONDS_PER_REQUEST / 60
    return (
        f"{requests:,} requests in all, about {minutes:.0f} minutes at one request a second "
        f"plus answer time ({SECONDS_PER_REQUEST} seconds a request)."
    )


def as_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)

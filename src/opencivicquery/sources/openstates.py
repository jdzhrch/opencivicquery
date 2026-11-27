"""Open States API v3: bills, sponsors, actions, and votes of the California Legislature.

Open States compiles state legislative records from each legislature's own data and serves
them at v3.openstates.org. The API needs a key. The key travels only in the X-API-KEY
header, never in a URL, and nothing this module returns or raises carries it. The host and
the California jurisdiction are fixed; the caller supplies only search words, a session, a
sponsor's name, a date, and a bill number, each checked before any request.

A free key allows 250 requests a day (counted in UTC) and 10 a minute for the whole server,
so a lookup sends at most two: the jurisdiction's session list and update time, kept for six
hours, and the bill search or the bill itself.

API documentation: https://docs.openstates.org/api-v3/
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx

from opencivicquery.models import Completeness, SourceQueryResult
from opencivicquery.sources.base import Lookup, PreparedLookup, SourceError, SourceQueryError
from opencivicquery.sources.http import FixedHostClient, source_reason

OPENSTATES_HOST = "v3.openstates.org"
SOURCE_NAME = "Open States API"
_API = f"https://{OPENSTATES_HOST}"
DOCS_URL = "https://docs.openstates.org/api-v3/"
JURISDICTION_ID = "ocd-jurisdiction/country:us/state:ca/government"
JURISDICTION_CACHE_SECONDS = 6 * 3600.0
SEARCH_PAGE_SIZE = 20
# checked 2026-11-27: per_page above 20 on /bills is refused by the API's own check; 20
# returned 20 rows with an exact total_items.
MAX_MEMBER_VOTES = 800
# Our own bound on member-by-member votes in one answer, so a bill with many roll calls does
# not outgrow what an assistant reads. checked 2026-11-27: SB 79 (2025-2026) had 10 roll
# calls with 229 member votes in all.
REQUESTS_PER_MINUTE = 10
# Open States' limit for a free key, for the whole server; this process keeps to it alone.
MAX_TEXT_LENGTH = 100
MAX_SPONSOR_LENGTH = 60

_API_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SESSION = re.compile(r"[0-9]{8}( Special Session [0-9]{1,2})?")
_BILL = re.compile(r"(AB|SB|ACA|SCA|AJR|SJR|ACR|SCR|HR|SR)\s*0*([1-9][0-9]{0,4})", re.IGNORECASE)
# The measure types the Legislature numbers: Assembly and Senate bills, constitutional
# amendments, joint, concurrent, and house resolutions. checked 2026-11-27: "SB 79" and
# "AB 1" returned bills; Open States stores the identifier with one space.
_STORED_BILL = re.compile(r"(AB|SB|ACA|SCA|AJR|SJR|ACR|SCR|HR|SR) [1-9][0-9]{0,4}")
# checked 2026-11-27: regular and special session bills alike, such as AB 906 and, in
# 20232024 Special Session 1, AB 1.
_PERSON_ID = re.compile(r"ocd-person/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_BILL_ID = re.compile(r"ocd-bill/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SPONSOR = re.compile(r"[^\W\d_][^\W\d_ .'’-]*(?:[ .'’-]+[^\W\d_]+)*\.?")
_LEGINFO = "leginfo.legislature.ca.gov"
_OPENSTATES_PAGES = frozenset({"openstates.org", "open.pluralpolicy.com"})
_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_JOINERS = "\u200c\u200d"
_LINK_BREAKERS = frozenset("()[]<>\"'`\\ ")

SEARCH_FIELDS = (
    "identifier",
    "session",
    "title",
    "chamber",
    "first_action_date",
    "latest_action_date",
    "latest_action_description",
    "latest_passage_date",
    "leginfo_url",
    "openstates_url",
    "openstates_id",
)
BILL_FIELDS = (
    "identifier",
    "session",
    "title",
    "chamber",
    "first_action_date",
    "latest_action_date",
    "latest_action_description",
    "latest_passage_date",
    "sponsors",
    "actions",
    "votes",
    "leginfo_url",
    "openstates_url",
    "openstates_id",
    "openstates_updated_at",
)

SEARCH_GRAIN = (
    "One California bill or resolution in one legislative session. The same number recurs in "
    "other sessions as a different measure."
)
BILL_GRAIN = (
    "One California bill or resolution in one legislative session, with its authors, every "
    "recorded action, and each roll call with how each member voted."
)

COMPILER_WARNING = (
    "Open States, a project of Plural, compiles these records from the California "
    "Legislature's own data; the Legislature's site, leginfo.legislature.ca.gov, is the record. "
    "Open States can lag it, so confirm a bill's current status and any vote you report on "
    "leginfo_url."
)
# official: docs.openstates.org/api-v3 and open.pluralpolicy.com/data; checked 2026-11-27:
# every bill's sources named a leginfo.legislature.ca.gov page.
SEARCH_WORDS_WARNING = (
    "text is matched against Open States' own search index of each bill, whose fields Open "
    "States does not document, so a match does not show what a bill does; read the bill "
    "before you count it. Text that looks like a bill number, such as SB 79, is matched as "
    "that number instead."
)
# checked 2026-11-27: q "SB 79" returned SB 79 of three sessions first, 15 matches in all.
SPONSOR_WARNING = (
    "sponsor matches the name the Legislature prints on a bill, compared exactly, such as "
    "Wiener, and two members can share one."
)
ROLE_WARNING = (
    "With role any, a match includes bills the member coauthored; with role author, only "
    "bills the member authored."
)
# checked 2026-11-27: sponsor Wiener matched 196 bills of 2025-2026, and 59 with
# sponsor_classification author; SB 79's sponsorships were Wiener (author), Wicks (principal
# coauthor), Haney and Lee (coauthor), each named by surname.
ACTION_SINCE_WARNING = (
    "action_since keeps bills whose latest action falls on or after that date. It has no end "
    "date, so a bill matches when any action came later than the period asked about; read "
    "the bill's actions before counting it in a window."
)
ORDER_WARNING = "Rows come with the most recent latest action first."
VOTE_OPTION_WARNING = (
    "In each roll call, option other counts members who did not vote yes or no, which the "
    "Legislature records as absent, abstaining, or not voting. Report the tally from counts "
    "and check the Legislature's vote page, the vote's source_url, before quoting it."
)
# checked 2026-11-27: SB 79's roll calls used only the options yes, no, and other.
DATE_WARNING = (
    "A roll call's date is the day it took place in California. Check it against the bill's "
    "actions, which record each floor vote with its tally."
)
# checked 2026-11-27: SB 79's committee votes carried midnight Pacific time written in UTC
# (2025-04-22T07:00:00+00:00), and its Assembly floor vote carried 2025-09-12T01:01:05+00:00,
# 6:01 p.m. on 2025-09-11 in California, the date of the action "Read third time. Passed.
# (Ayes 43. Noes 19.)".

OPENSTATES_RULE = (
    "Name Open States as the compiler and the California Legislature as the source of record, "
    "give the session with every bill number, and link each bill's leginfo_url when it has one."
)
NO_MATCH_RULE = (
    "NO BILLS MATCHED. Say that Open States returned no California bill for this search. Do "
    "not say that no such bill exists."
)


def _invalid(argument: str, message: str) -> SourceQueryError:
    return SourceQueryError(
        message, code="INVALID_QUERY_ARGUMENT", argument=argument, public_message=message
    )


def _plain(value: Any, argument: str, limit: int) -> str | None:
    """Normalized text with single spaces, or None for an empty value; raise on bad text."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(argument, f"Give {argument} as text.")
    text = " ".join(unicodedata.normalize("NFKC", value[: limit * 4]).split())
    if not text:
        return None
    if len(text) > limit or any(unicodedata.category(c) in _UNSAFE for c in text):
        raise _invalid(argument, f"Give {argument} as at most {limit} characters of words.")
    return text


def validate_bill(value: Any) -> str:
    """The bill number as Open States stores it, such as SB 79, or raise."""
    text = _plain(value, "bill", 12)
    match = _BILL.fullmatch(text) if text else None
    if match is None:
        raise _invalid(
            "bill",
            "Give bill as a California measure number, such as SB 79, AB 1, or ACA 13.",
        )
    return f"{match.group(1).upper()} {int(match.group(2))}"


def validate_session(value: Any) -> str | None:
    text = _plain(value, "session", 30)
    if text is not None and not _SESSION.fullmatch(text):
        raise _invalid(
            "session",
            "Give session as Open States names it, such as 20252026 for the 2025-2026 "
            "Regular Session or 20252026 Special Session 1.",
        )
    return text


def validate_sponsor(value: Any) -> str | None:
    text = _plain(value, "sponsor", MAX_SPONSOR_LENGTH)
    if text is None or _PERSON_ID.fullmatch(text):
        return text
    if not _SPONSOR.fullmatch(text):
        raise _invalid(
            "sponsor",
            "Give sponsor as the member's name as the Legislature prints it on bills, such as "
            "Wiener, or as an Open States person id (ocd-person/...).",
        )
    return text


def validate_date(argument: str, value: Any) -> date | None:
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else ""
    if not _DATE.fullmatch(text):
        raise _invalid(argument, f"Give {argument} as YYYY-MM-DD.")
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise _invalid(argument, f"{argument} is not a real calendar date.") from exc
    if not date(1990, 1, 1) <= day <= date(2100, 12, 31):
        raise _invalid(argument, f"Give {argument} between 1990-01-01 and 2100-12-31.")
    return day


def _schema_hash(kind: str, fields: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps({"kind": kind, "fields": list(fields)}, separators=(",", ":")).encode()
    ).hexdigest()


def _text(value: Any) -> str | None:
    """Source text with control and format characters removed, or None."""
    if not isinstance(value, str):
        return None
    kept = "".join(c for c in value if unicodedata.category(c) not in _UNSAFE or c in _JOINERS)
    return " ".join(kept.split()) or None


def _nth_sunday(year: int, month: int, nth: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))


def _california_date(moment: datetime) -> date:
    """The date in California of a moment, by the United States daylight saving rules.

    Pacific daylight time runs from 2 a.m. on the second Sunday of March to 2 a.m. on the
    first Sunday of November since 2007, and from the first Sunday of April to the last
    Sunday of October before then. Written out here so the server needs no time zone
    database, which some of the systems it runs on lack.
    """
    year = moment.astimezone(UTC).year
    if year >= 2007:
        begins, ends = _nth_sunday(year, 3, 2), _nth_sunday(year, 11, 1)
    else:
        begins = _nth_sunday(year, 4, 1)
        ends = _nth_sunday(year, 10, 5)
        if ends.month != 10:
            ends -= timedelta(days=7)
    start = datetime(begins.year, begins.month, begins.day, 10, tzinfo=UTC)  # 2 a.m. PST
    stop = datetime(ends.year, ends.month, ends.day, 9, tzinfo=UTC)  # 2 a.m. PDT
    offset = -7 if start <= moment < stop else -8
    return (moment.astimezone(UTC) + timedelta(hours=offset)).date()


def _moment(value: Any) -> datetime | None:
    """An ISO time with its offset, or None."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _timestamp(value: Any) -> str | None:
    """An ISO time rewritten in a standard form, or None; source text never passes through."""
    moment = _moment(value)
    return moment.isoformat() if moment is not None else None


def _day(value: Any) -> str | None:
    """The date part of an ISO date or time, or None."""
    if not isinstance(value, str) or not _DATE.fullmatch(value[:10]):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _link(value: Any, hosts: frozenset[str] | set[str]) -> str | None:
    """A plain web address on one of the named hosts, or None."""
    if (
        not isinstance(value, str)
        or len(value) > 500
        or any(c in _LINK_BREAKERS for c in value)
        or any(unicodedata.category(c) in _UNSAFE for c in value)
    ):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme not in {"http", "https"}
        or (parts.hostname or "") not in hosts
        or port is not None
        or parts.username is not None
    ):
        return None
    return value


def _leginfo(sources: Any) -> str | None:
    if not isinstance(sources, list):
        return None
    for source in sources:
        link = _link(source.get("url") if isinstance(source, dict) else None, {_LEGINFO})
        if link is not None:
            return link
    return None


def _chamber(organization: Any) -> str | None:
    return _text(organization.get("name")) if isinstance(organization, dict) else None


def _person_id(person: Any) -> str | None:
    value = person.get("id") if isinstance(person, dict) else None
    return value if isinstance(value, str) and _PERSON_ID.fullmatch(value) else None


def _bill_core(record: Any) -> dict[str, Any]:
    """The fields a search row and a bill share, or raise SourceError."""
    if not isinstance(record, dict):
        raise SourceError(f"The {SOURCE_NAME} returned a bill that is not an object")
    identifier = record.get("identifier")
    bill_id = record.get("id")
    session = record.get("session")
    jurisdiction = record.get("jurisdiction")
    if (
        not isinstance(identifier, str)
        or _STORED_BILL.fullmatch(identifier) is None
        or not isinstance(bill_id, str)
        or _BILL_ID.fullmatch(bill_id) is None
        or not isinstance(session, str)
        or _SESSION.fullmatch(session) is None
        or (
            jurisdiction is not None
            and (not isinstance(jurisdiction, dict) or jurisdiction.get("id") != JURISDICTION_ID)
        )
    ):
        raise SourceError(f"The {SOURCE_NAME} returned a bill without valid identifiers")
    return {
        "identifier": identifier,
        "session": session,
        "title": _text(record.get("title")),
        "chamber": _chamber(record.get("from_organization")),
        "first_action_date": _day(record.get("first_action_date")),
        "latest_action_date": _day(record.get("latest_action_date")),
        "latest_action_description": _text(record.get("latest_action_description")),
        "latest_passage_date": _day(record.get("latest_passage_date")),
        "leginfo_url": _leginfo(record.get("sources")),
        "openstates_url": _link(record.get("openstates_url"), _OPENSTATES_PAGES),
        "openstates_id": bill_id,
    }


def _sponsors(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise SourceError(f"The {SOURCE_NAME} returned a bill without its sponsors")
    sponsors = []
    for item in items:
        if not isinstance(item, dict) or _text(item.get("name")) is None:
            raise SourceError(f"The {SOURCE_NAME} returned a malformed sponsor")
        person = item.get("person")
        sponsors.append(
            {
                "name": _text(item.get("name")),
                "full_name": _text(person.get("name")) if isinstance(person, dict) else None,
                "party": _text(person.get("party")) if isinstance(person, dict) else None,
                "role": _text(item.get("classification")),
                "primary": item.get("primary") is True,
                "person_id": _person_id(person),
            }
        )
    return sponsors


def _actions(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise SourceError(f"The {SOURCE_NAME} returned a bill without its actions")
    actions = []
    for item in items:
        day = _day(item.get("date")) if isinstance(item, dict) else None
        if day is None or not isinstance(item.get("order"), int):
            raise SourceError(f"The {SOURCE_NAME} returned a malformed action")
        kinds = item.get("classification")
        actions.append(
            {
                "order": item["order"],
                "date": day,
                "chamber": _chamber(item.get("organization")),
                "description": _text(item.get("description")),
                "classification": [k for k in kinds if isinstance(k, str)]
                if isinstance(kinds, list)
                else [],
            }
        )
    return sorted(actions, key=lambda action: (action["date"], action["order"]))


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SourceError(f"The {SOURCE_NAME} returned a malformed vote count")
    return value


def _votes(items: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Each roll call, earliest first, and the roll calls whose member list disagrees with
    the tally."""
    if not isinstance(items, list):
        raise SourceError(f"The {SOURCE_NAME} returned a bill without its votes")
    votes = []
    disagree = []
    for item in items:
        moment = _moment(item.get("start_date")) if isinstance(item, dict) else None
        counts = item.get("counts") if isinstance(item, dict) else None
        members = item.get("votes") if isinstance(item, dict) else None
        if moment is None or not isinstance(counts, list) or not isinstance(members, list):
            raise SourceError(f"The {SOURCE_NAME} returned a malformed vote")
        day = _california_date(moment).isoformat()
        tally: dict[str, int] = {}
        for count in counts:
            option = _text(count.get("option")) if isinstance(count, dict) else None
            if option is None:
                raise SourceError(f"The {SOURCE_NAME} returned a malformed vote count")
            tally[option] = tally.get(option, 0) + _count(count.get("value"))
        listed: dict[str, int] = {}
        member_rows = []
        for member in members:
            option = _text(member.get("option")) if isinstance(member, dict) else None
            if option is None:
                raise SourceError(f"The {SOURCE_NAME} returned a malformed member vote")
            listed[option] = listed.get(option, 0) + 1
            person = _person_id(member.get("voter"))
            member_rows.append(
                {"name": _text(member.get("voter_name")), "option": option, "person_id": person}
            )
        motion = _text(item.get("motion_text"))
        chamber = _chamber(item.get("organization"))
        if {k: v for k, v in tally.items() if v} != listed:
            disagree.append(f"{day} {chamber or ''} {(motion or '')[:80]}".strip())
        votes.append(
            {
                "moment": moment,
                "date": day,
                "chamber": chamber,
                "motion": motion,
                "result": _text(item.get("result")),
                "counts": tally,
                "members": member_rows,
                "source_url": _leginfo(item.get("sources")),
            }
        )
    votes.sort(key=lambda vote: vote.pop("moment"))
    return votes, disagree


def _cap_members(votes: list[dict[str, Any]]) -> int:
    """Keep member lists for the latest roll calls up to MAX_MEMBER_VOTES; return how many
    roll calls lost theirs."""
    room = MAX_MEMBER_VOTES
    dropped = 0
    for vote in reversed(votes):
        if len(vote["members"]) <= room:
            room -= len(vote["members"])
        else:
            vote["members"] = None
            dropped += 1
            room = 0
    return dropped


def _slow_down(reason: str) -> SourceQueryError:
    return SourceQueryError(
        f"Open States per-minute limit: {reason}",
        code="SOURCE_RATE_LIMITED",
        http_status=429,
        public_message=(
            "This server has sent Open States too many requests this minute. Wait a minute "
            "and try again."
        ),
    )


class _KeyedClient(FixedHostClient):
    """FixedHostClient that also sends the Open States key header on every request."""

    def __init__(self, *, api_key: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if api_key is not None:
            # official: docs.openstates.org/api-v3, the key goes in the X-API-KEY header or
            # the apikey parameter. The header keeps it out of every URL.
            self._headers = {**self._headers, "X-API-KEY": api_key}


class OpenStates:
    """Search and read California bills through the Open States API."""

    host = OPENSTATES_HOST

    def __init__(
        self,
        *,
        api_key: str | None,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        key = api_key.strip() if api_key else ""
        if key and not _API_KEY.fullmatch(key):
            raise ValueError(
                "The Open States API key must be letters, digits, hyphens, or underscores"
            )
        if client is not None and client.follow_redirects:
            # httpx keeps custom headers on a redirect to another host, so the key would
            # follow it there.
            raise ValueError("The Open States client must not follow redirects")
        self._api_key = key or None
        self._http = _KeyedClient(api_key=self._api_key, source_name=SOURCE_NAME, client=client)
        self._clock = clock
        self._today = today
        self._jurisdiction: tuple[float, list[dict[str, Any]], str | None] | None = None
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _redact(self, value: Any) -> Any:
        """Remove the key from every string in a JSON value."""
        if isinstance(value, str):
            return value.replace(self._api_key, "[redacted]") if self._api_key else value
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        return value

    def _require_key(self) -> None:
        if self._api_key is None:
            raise SourceQueryError(
                "No Open States API key configured",
                code="SOURCE_NOT_CONFIGURED",
                public_message="This server has no Open States API key configured.",
            )

    async def _get(self, path: str, params: list[tuple[str, str]], *, what: str) -> Any:
        now = self._clock()
        while self._sent and now - self._sent[0] >= 60:
            self._sent.popleft()
        if len(self._sent) >= REQUESTS_PER_MINUTE:
            # Open States would refuse it, and a refused request may still count toward the
            # day's allowance, so it is never sent.
            raise _slow_down(f"local limit of {REQUESTS_PER_MINUTE} requests a minute")
        self._sent.append(now)
        status, content = await self._http.send("GET", f"{_API}{path}", params=params)
        try:
            payload = json.loads(content) if content else None
        except (ValueError, RecursionError):
            payload = None
        if status == 200:
            if not isinstance(payload, dict):
                raise SourceError(f"The {SOURCE_NAME} returned a body that is not a JSON object")
            return payload
        # Redact before source_reason shortens the text, so no part of the key survives.
        reason = source_reason(self._redact(payload)) or f"HTTP {status}"
        if status == 429:
            # The API's own limiter answers 429 with "exceeded limit of 250/day" or
            # "... 10/min" (source: openstates/api-v3, api/auth.py); its day is UTC.
            if "/min" in reason:
                raise _slow_down(reason)
            raise SourceQueryError(
                f"Open States HTTP 429: {reason}",
                code="SOURCE_RATE_LIMITED",
                http_status=429,
                public_message=(
                    "This server has used up its daily allowance of Open States requests. It "
                    "resets at midnight UTC; try again after that, tomorrow."
                    if "/day" in reason
                    else "Open States is refusing more requests from this server for now. "
                    "Try again in a minute; if it still refuses, the day's allowance is used "
                    "up, and it resets at midnight UTC."
                ),
            )
        if status in {401, 403}:
            code, public = (
                "SOURCE_ACCESS_DENIED",
                (f"Open States refused this server's API key (HTTP {status})."),
            )
        elif status == 404:
            code, public = "NOT_FOUND", f"Open States has no {what}."
        elif status in {400, 422}:
            code, public = "QUERY_REJECTED", f"Open States rejected the request: {reason}"
        else:
            code, public = (
                "SOURCE_UNAVAILABLE",
                (f"The Open States API could not complete the request (HTTP {status})."),
            )
        raise SourceQueryError(
            f"Open States HTTP {status}: {reason}",
            code=code,
            http_status=status,
            public_message=public,
        )

    async def _sessions(self) -> tuple[list[dict[str, Any]], str | None]:
        """California's sessions and the time Open States last updated a California bill,
        read at most once every six hours per process."""
        async with self._lock:
            now = self._clock()
            if self._jurisdiction is not None and now - self._jurisdiction[0] < (
                JURISDICTION_CACHE_SECONDS
            ):
                return self._jurisdiction[1], self._jurisdiction[2]
            payload = await self._get(
                f"/jurisdictions/{JURISDICTION_ID}",
                [("include", "legislative_sessions")],
                what="record for California",
            )
            items = payload.get("legislative_sessions")
            if payload.get("id") != JURISDICTION_ID or not isinstance(items, list):
                raise SourceError(f"The {SOURCE_NAME} returned a malformed jurisdiction")
            sessions = []
            for item in items:
                identifier = item.get("identifier") if isinstance(item, dict) else None
                if not isinstance(identifier, str) or not _SESSION.fullmatch(identifier):
                    continue
                sessions.append(
                    {
                        "identifier": identifier,
                        "name": _text(item.get("name")),
                        "primary": item.get("classification") == "primary",
                        "start_date": _day(item.get("start_date")),
                        "end_date": _day(item.get("end_date")),
                    }
                )
            if not sessions:
                raise SourceError(f"The {SOURCE_NAME} listed no California sessions")
            updated_at = _timestamp(payload.get("latest_bill_update"))
            self._jurisdiction = (now, sessions, updated_at)
            return sessions, updated_at

    def _pick_session(self, sessions: list[dict[str, Any]], asked: str | None) -> dict[str, Any]:
        if asked is not None:
            for session in sessions:
                if session["identifier"] == asked:
                    return session
            newest = sorted(sessions, key=lambda s: s["start_date"] or "", reverse=True)[:5]
            raise _invalid(
                "session",
                f"Open States lists no California session {asked}. Recent sessions: "
                + "; ".join(s["identifier"] for s in newest)
                + ".",
            )
        today = self._today().isoformat()
        started = [s for s in sessions if s["start_date"] and s["start_date"] <= today]
        if not started:
            raise SourceError(f"The {SOURCE_NAME} listed no California session under way")
        regular = [s for s in started if s["primary"]] or started
        return max(regular, key=lambda s: s["start_date"])

    def search_bills(
        self,
        *,
        text: str | None = None,
        session: str | None = None,
        sponsor: str | None = None,
        role: str = "any",
        action_since: str | None = None,
    ) -> PreparedLookup:
        """Validate one bill search without touching the network and return it."""
        self._require_key()
        words = _plain(text, "text", MAX_TEXT_LENGTH)
        asked = validate_session(session)
        name = validate_sponsor(sponsor)
        if role not in {"any", "author"}:
            raise _invalid("role", "Give role as any or author.")
        if role == "author" and name is None:
            raise _invalid("role", "role author needs a sponsor.")
        since = validate_date("action_since", action_since)
        arguments = {
            "text": words,
            "session": asked,
            "sponsor": name,
            "role": role,
            "action_since": since.isoformat() if since else None,
        }

        async def run() -> Lookup:
            sessions, updated_at = await self._sessions()
            chosen = self._pick_session(sessions, asked)
            params = [("jurisdiction", JURISDICTION_ID), ("session", chosen["identifier"])]
            if words:
                params.append(("q", words))
            if name:
                params.append(("sponsor", name))
                if role == "author":
                    params.append(("sponsor_classification", "author"))
            if since:
                params.append(("action_since", since.isoformat()))
            params += [
                ("sort", "latest_action_desc"),
                ("include", "sources"),
                ("per_page", str(SEARCH_PAGE_SIZE)),
                ("page", "1"),
            ]
            payload = await self._get("/bills", params, what="such search")
            results = payload.get("results")
            pagination = payload.get("pagination")
            total = pagination.get("total_items") if isinstance(pagination, dict) else None
            if (
                not isinstance(results, list)
                or isinstance(total, bool)
                or not isinstance(total, int)
                or len(results) > SEARCH_PAGE_SIZE
                or len(results) > total
                or (total > 0 and not results)
            ):
                raise SourceError(f"The {SOURCE_NAME} returned a search that does not add up")
            rows = []
            for record in results:
                row = _bill_core(record)
                if row["session"] != chosen["identifier"]:
                    raise SourceError(f"The {SOURCE_NAME} returned a bill of another session")
                rows.append({field: row[field] for field in SEARCH_FIELDS})
            if len({row["openstates_id"] for row in rows}) < len(rows):
                raise SourceError(f"The {SOURCE_NAME} returned the same bill twice")
            warnings = []
            if total > len(rows):
                warnings.append(
                    f"PARTIAL: Open States counts {total} matching bills; this answer holds "
                    f"the first {len(rows)}, most recent latest action first. Narrow the search "
                    "to see the rest."
                )
            if asked is None:
                warnings.append(
                    f"No session was given, so this searched {chosen['identifier']} "
                    f"({chosen['name'] or 'its current session'}), the newest regular "
                    "session that Open States lists as begun."
                )
            if words:
                warnings.append(SEARCH_WORDS_WARNING)
            if name and not _PERSON_ID.fullmatch(name):
                warnings.append(SPONSOR_WARNING)
            if name:
                warnings.append(ROLE_WARNING)
            if since:
                warnings.append(ACTION_SINCE_WARNING)
            warnings += [ORDER_WARNING, COMPILER_WARNING]
            result = SourceQueryResult(
                dataset_id=f"openstates/ca/bills/{chosen['identifier']}",
                dataset_name=(
                    f"California bills, {chosen['name'] or chosen['identifier']}, from Open "
                    f"States: {total} match"
                ),
                domain=OPENSTATES_HOST,
                source_url=DOCS_URL,
                query=f"{_API}/bills?{urlencode(params)}",
                query_url=None,
                rows=rows,
                returned_row_count=len(rows),
                completeness=(
                    Completeness.TRUNCATED if total > len(rows) else Completeness.COMPLETE
                ),
                source_updated_at=updated_at,
                schema_sha256=_schema_hash("bill-search", SEARCH_FIELDS),
            )
            return Lookup(
                result=result,
                row_grain=SEARCH_GRAIN,
                warnings=warnings,
                rules=([NO_MATCH_RULE] if total == 0 else []) + [OPENSTATES_RULE],
                escalations=["NO_BILLS_MATCHED"] if total == 0 else [],
            )

        return PreparedLookup(host=OPENSTATES_HOST, arguments=arguments, run=run)

    def get_bill(self, *, bill: str, session: str | None = None) -> PreparedLookup:
        """Validate one bill read without touching the network and return it."""
        self._require_key()
        number = validate_bill(bill)
        asked = validate_session(session)
        arguments = {"bill": number, "session": asked}

        async def run() -> Lookup:
            sessions, updated_at = await self._sessions()
            chosen = self._pick_session(sessions, asked)
            path = f"/bills/ca/{quote(chosen['identifier'], safe='')}/{quote(number, safe='')}"
            params = [
                ("include", "sponsorships"),
                ("include", "actions"),
                ("include", "votes"),
                ("include", "sources"),
            ]
            try:
                record = await self._get(path, params, what="bill")
            except SourceQueryError as exc:
                if exc.code != "NOT_FOUND":
                    raise
                raise SourceQueryError(
                    f"No bill {number} in {chosen['identifier']}",
                    code="NOT_FOUND",
                    argument="bill",
                    http_status=404,
                    public_message=(
                        f"Open States has no California bill {number} in session "
                        f"{chosen['identifier']}."
                    ),
                ) from exc
            row = _bill_core(record)
            if row["identifier"] != number or row["session"] != chosen["identifier"]:
                raise SourceError(f"The {SOURCE_NAME} returned a different bill")
            row["sponsors"] = _sponsors(record.get("sponsorships"))
            row["actions"] = _actions(record.get("actions"))
            row["votes"], disagree = _votes(record.get("votes"))
            dropped = _cap_members(row["votes"])
            # Counted after the cap: only member lists this answer holds.
            unmatched = sum(
                member["person_id"] is None
                for vote in row["votes"]
                for member in vote["members"] or []
            )
            row["openstates_updated_at"] = _timestamp(record.get("updated_at"))
            row = {field: row[field] for field in BILL_FIELDS}
            warnings = []
            if dropped:
                warnings.append(
                    f"PARTIAL: this bill has more member votes than one answer holds, so "
                    f"{dropped} of its {len(row['votes'])} roll calls, the earliest, come with "
                    "their tallies but without how each member voted (members is null). The "
                    "vote's source_url lists them."
                )
            if asked is None:
                warnings.append(
                    f"No session was given, so this read {number} of {chosen['identifier']} "
                    f"({chosen['name'] or 'its current session'}), the newest regular "
                    "session that Open States lists as begun. The same number names a "
                    "different bill in other sessions."
                )
            if disagree:
                warnings.append(
                    "In these roll calls the member list does not add up to the tally: "
                    + "; ".join(disagree)
                    + ". Report the tally from counts and check the vote's source_url."
                )
            if unmatched:
                warnings.append(
                    f"{unmatched} member vote{'s are' if unmatched != 1 else ' is'} not tied "
                    "to an Open States person record; the name given is the one the "
                    "Legislature printed."
                )
            if not row["votes"]:
                warnings.append("Open States holds no roll call for this bill.")
            warnings += [VOTE_OPTION_WARNING, DATE_WARNING, COMPILER_WARNING]
            result = SourceQueryResult(
                dataset_id=f"openstates/ca/bills/{chosen['identifier']}/{number}",
                dataset_name=(
                    f"California {number} ({chosen['name'] or chosen['identifier']}) from "
                    "Open States"
                ),
                domain=OPENSTATES_HOST,
                source_url=row["leginfo_url"] or row["openstates_url"] or DOCS_URL,
                query=f"{_API}{path}?{urlencode(params)}",
                query_url=None,
                rows=[row],
                returned_row_count=1,
                completeness=Completeness.TRUNCATED if dropped else Completeness.COMPLETE,
                source_updated_at=updated_at,
                schema_sha256=_schema_hash("bill", BILL_FIELDS),
            )
            return Lookup(
                result=result,
                row_grain=BILL_GRAIN,
                warnings=warnings,
                rules=[OPENSTATES_RULE],
            )

        return PreparedLookup(host=OPENSTATES_HOST, arguments=arguments, run=run)

    async def close(self) -> None:
        await self._http.close()

"""Granicus Legistar Web API: city and county meetings, agenda items, votes, legislation.

Most Legistar clients answer without a key. Every request goes to one fixed host; the
caller names only a client, which is checked against a strict pattern before it becomes
a path segment.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

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


class Legistar:
    """Read meetings, agenda items, votes, and legislation from the Legistar Web API."""

    host = LEGISTAR_HOST

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        tokens: Mapping[str, str] | None = None,
    ) -> None:
        self._http = FixedHostClient(source_name=SOURCE_NAME, client=client)
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
            records, public_url = await self._list(name, "events", params)
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
            records, public_url = await self._list(name, "matters", params)
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
            return Lookup(
                result=result, row_grain=MATTER_GRAIN, warnings=warnings, rules=[LEGISTAR_RULE]
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

    async def close(self) -> None:
        await self._http.close()

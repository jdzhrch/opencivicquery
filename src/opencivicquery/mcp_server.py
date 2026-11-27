from __future__ import annotations

import hashlib
import json
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, SecretStr, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from opencivicquery import __version__
from opencivicquery.audit import AuditLogger
from opencivicquery.client import ClientIdentity, client_address, identify
from opencivicquery.config import ConfigurationError, RuntimeSettings, load_deployment
from opencivicquery.limits import UsageLimiter, UsageLimitExceeded
from opencivicquery.logging_redaction import install as install_log_redaction
from opencivicquery.models import QueryRequest
from opencivicquery.service import OPEN_ACTOR, CallContext, OpenCivicQueryService
from opencivicquery.snapshots import FileSnapshotStore, GcsSnapshotStore, SnapshotError
from opencivicquery.sources.base import PreparedLookup, SourceError, SourceQueryError
from opencivicquery.sources.census_data import CENSUS_DATA_HOST, MAX_VARIABLES, CensusData
from opencivicquery.sources.census_geocoder import MAX_ADDRESS_LENGTH, CensusGeocoder
from opencivicquery.sources.govinfo import DEFAULT_LIMIT as OPINIONS_DEFAULT_LIMIT
from opencivicquery.sources.govinfo import GOVINFO_HOST, GovInfo
from opencivicquery.sources.govinfo import MAX_LIMIT as OPINIONS_MAX_LIMIT
from opencivicquery.sources.govinfo import MAX_TEXT_LENGTH as OPINIONS_MAX_TEXT
from opencivicquery.sources.legistar import LEGISTAR_HOST, Legistar
from opencivicquery.sources.legistar import MAX_LIMIT as LEGISTAR_MAX_LIMIT
from opencivicquery.sources.legistar import MAX_TEXT_LENGTH as LEGISTAR_MAX_TEXT
from opencivicquery.sources.nonprofits import MAX_NAME_LENGTH as NONPROFIT_MAX_NAME
from opencivicquery.sources.nonprofits import NONPROFITS_HOST, NonprofitExplorer
from opencivicquery.sources.openstates import MAX_SPONSOR_LENGTH as STATE_MAX_SPONSOR
from opencivicquery.sources.openstates import MAX_TEXT_LENGTH as STATE_MAX_TEXT
from opencivicquery.sources.openstates import OPENSTATES_HOST, OpenStates
from opencivicquery.sources.sec_edgar import DEFAULT_LIMIT as SEC_DEFAULT_LIMIT
from opencivicquery.sources.sec_edgar import MAX_COMPANY_LENGTH, MAX_FORM_TYPES, SecEdgar
from opencivicquery.sources.sec_edgar import MAX_LIMIT as SEC_MAX_LIMIT
from opencivicquery.sources.sec_edgar import SUBMISSIONS_HOST as SEC_HOST
from opencivicquery.sources.sfgov import MAX_AGENCY_LENGTH as SFGOV_MAX_AGENCY
from opencivicquery.sources.sfgov import MAX_PAGE_ID as SFGOV_MAX_PAGE_ID
from opencivicquery.sources.sfgov import MAX_TEXT_LENGTH as SFGOV_MAX_TEXT
from opencivicquery.sources.sfgov import SFGOV_HOST, SfGov
from opencivicquery.sources.sfgov_copy import CopyReader, copy_store
from opencivicquery.sources.socrata import SocrataNetwork
from opencivicquery.sources.usaspending import DEFAULT_LIMIT as AWARDS_DEFAULT_LIMIT
from opencivicquery.sources.usaspending import (
    MAX_KEYWORD_LENGTH,
    MAX_KEYWORDS,
    MAX_RECIPIENT_LENGTH,
    USASPENDING_HOST,
    USAspending,
)
from opencivicquery.sources.usaspending import MAX_LIMIT as AWARDS_MAX_LIMIT

SERVER_INSTRUCTIONS = (
    "OpenCivicQuery answers questions from government open data on every portal the Socrata "
    "platform hosts: cities, counties, states, and federal agencies. Call list_source_packs "
    "first. A source pack documents what each row of a dataset means and how to count it, "
    "and answers from documented datasets are safer. Then search and describe a dataset "
    "before querying it. Ask the user to clarify any ambiguous place, date range, metric, or "
    "counting unit. Never present a truncated result as complete. Judge whether the records "
    "support the user's specific claim. If the row grain, time field, geography, or counting "
    "unit is unclear, say exactly what remains unknown and do not expand the result into a "
    "broader claim. Never attach a dataset-wide quality, review, approval, certification, or "
    "publication label. Publication decisions belong to the newsroom's editorial process. "
    "Every successful query returns a citable snapshot. To place a U.S. street address in "
    "census geographies such as its county, tract, and block, call geocode_address. For "
    "American Community Survey estimates for those geographies, call get_census_data. For "
    "city and county meetings, agenda items, member votes, and legislation in places that "
    "publish through Legistar, call find_meetings, get_meeting_items, get_item_votes, and "
    "search_legislation. For a company's SEC filings, call find_sec_filings. For federal "
    "contracts, grants, and other awards, call search_federal_awards. For the text of federal "
    "court opinions, call search_court_opinions. For a tax-exempt organization's IRS Form "
    "990 filings, call search_nonprofits and then get_nonprofit_filings. For news, meeting "
    "pages, and documents on San Francisco's city website, SF.gov, call find_sfgov_news, "
    "find_sfgov_meetings, and search_sfgov; get_sfgov_page returns the text of one news or "
    "meeting page. For California state bills, their authors, actions, and how each "
    "legislator voted, call search_california_bills and get_california_bill. These tools "
    "follow the same evidence rules.\n\n"
    "The people reading your answer are reporters who will not open the tool output. A "
    "caveat reaches them only if you write it into your reply. Every successful query "
    "response carries a presentation_rules list; follow every rule in it. At minimum, name "
    "the counting unit in the same sentence as any figure you state, give the as-of date as "
    "an update time that the source reports when source_updated_at is available, state the "
    "completeness value for each query, state when the source omitted that date, repeat each "
    "warning that bears on a figure you quote, copy each response query verbatim beside its "
    "public query_url when one is present, state when no live-result URL is available, and "
    "supply the snapshot URI for any figure the user may publish. Describe a truncated result "
    "as partial at the point where you state the number. A trailing caveat does not satisfy "
    "this.\n\n"
    "When a question cannot be answered from the records, for example because a field's "
    "meaning is undocumented, draft a short, specific question the reporter can send to the "
    "agency that publishes the dataset."
)


def _instructions(tools: tuple[str, ...]) -> str:
    """The server instructions, introduced by the tools offered when only some are."""
    if not tools:
        return SERVER_INSTRUCTIONS
    rules = SERVER_INSTRUCTIONS.split("\n\n", 1)[1]
    return (
        f"This server offers these OpenCivicQuery tools: {', '.join(tools)}. They follow "
        "OpenCivicQuery's evidence rules: every successful answer returns a citable snapshot."
        f"\n\n{rules}"
    )


@dataclass
class AppState:
    service: OpenCivicQueryService
    legistar: Legistar | None = None
    sec: SecEdgar | None = None
    usaspending: USAspending | None = None
    govinfo: GovInfo | None = None
    census: CensusData | None = None
    nonprofits: NonprofitExplorer | None = None
    sfgov: SfGov | None = None
    openstates: OpenStates | None = None
    """Record sources that answer through OpenCivicQueryService.lookup. The lifespan
    builds and closes them; None only in tests that never call their tools."""


class QueryOnlyMCPServer(MCPServer[AppState]):
    """Keep HTTP queries without the SDK's independent legacy GET event stream."""

    def streamable_http_app(
        self, *, streamable_http_path: str = "/mcp", **kwargs: Any
    ) -> Starlette:
        app = super().streamable_http_app(streamable_http_path=streamable_http_path, **kwargs)

        async def reject_idle_stream(_: Request) -> Response:
            return Response(status_code=405, headers={"Allow": "POST"})

        # SDK custom routes come after its MCP route and cannot intercept its GET stream.
        # Put this route first; POST still reaches the SDK's original authenticated route.
        app.router.routes.insert(
            0, Route(streamable_http_path, reject_idle_stream, methods=["GET"])
        )
        return app


def _build_service(settings: RuntimeSettings) -> OpenCivicQueryService:
    bundle = load_deployment(settings.config_path, auth_mode=settings.auth_mode)
    portals = bundle.config.socrata.portals
    source = SocrataNetwork(
        app_token=(
            bundle.socrata_app_token.get_secret_value()
            if bundle.socrata_app_token is not None
            else None
        ),
        portals=None if portals == "any" else list(portals),
    )
    if settings.snapshot_backend == "local":
        assert settings.snapshot_path is not None
        snapshot_store = FileSnapshotStore(settings.snapshot_path, bundle.config.deployment_id)
    else:
        assert settings.snapshot_bucket is not None
        snapshot_store = GcsSnapshotStore(settings.snapshot_bucket, bundle.config.deployment_id)
    return OpenCivicQueryService(
        bundle=bundle,
        source=source,
        geocoder=CensusGeocoder(),
        snapshot_store=snapshot_store,
        audit_logger=AuditLogger(stream=sys.stderr if settings.transport == "stdio" else None),
        limiter=UsageLimiter(bundle.config.limits),
        auth_mode=settings.auth_mode,
    )


REPORTER_QUESTION_FIELD = Field(
    description=(
        "The reporter's own question, quoted as they asked it, not a rewrite and not a "
        "restatement of the query you built. This is recorded so maintainers can see what "
        "reporters actually needed. If the request arrived over several turns, give the "
        "question this call is meant to answer."
    ),
    min_length=1,
    max_length=500,
)

DATASET_ID_FIELD = Field(
    description="The Socrata dataset ID, four characters, a hyphen, and four characters.",
    min_length=9,
    max_length=9,
)

CLAUSE_FIELD = Field(min_length=1, max_length=8000)

SNAPSHOT_URI_FIELD = Field(
    description="The snapshot URI that a query or lookup tool returned.",
    min_length=1,
    max_length=512,
)

DOMAIN_FIELD = Field(
    description=(
        "The portal's hostname, for example data.cityofnewyork.us. Use the domain that "
        "search_datasets or list_source_packs returned."
    ),
    min_length=1,
    max_length=253,
)


LEGISTAR_CLIENT_FIELD = Field(
    description=(
        "The jurisdiction's Legistar client name: the first part of its Legistar web "
        "address, such as oakland for oakland.legistar.com or seattle for "
        "seattle.legistar.com."
    ),
    min_length=1,
    max_length=40,
)

DATE_FIELD = Field(description="A date as YYYY-MM-DD.", min_length=10, max_length=10)
SFGOV_AGENCY_FIELD = Field(
    description=(
        "The department or public body exactly as SF.gov names it, such as Police "
        "Commission or Office of the Mayor."
    ),
    max_length=SFGOV_MAX_AGENCY,
)
SFGOV_OPTIONAL_AGENCY_FIELD = Field(
    description=(
        "Optional. The department or public body exactly as SF.gov names it, such as "
        "Office of the Mayor. Leave it out for every agency."
    ),
    max_length=SFGOV_MAX_AGENCY,
)

STATE_SESSION_FIELD = Field(
    description=(
        "The legislative session as Open States names it, such as 20252026 for the 2025-2026 "
        "Regular Session. Omit it for the most recent session."
    ),
    max_length=30,
)
EVENT_GUID_FIELD = Field(
    description=(
        "The meeting's GUID: the EventGuid that find_meetings or a matter's History "
        "returned with the EventId. Needed only for a jurisdiction whose meetings are read "
        "from its Legistar pages, such as San Francisco (sfgov)."
    ),
    min_length=36,
    max_length=36,
)


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def _connection_id(settings: RuntimeSettings) -> str | None:
    """A stable id for one authenticated connection, so one line of enquiry reads as one."""
    if settings.auth_mode != "oauth":
        return None
    access_token = get_access_token()
    if access_token is None:
        return None
    return hashlib.sha256(access_token.token.encode()).hexdigest()[:12]


_warned_peer_behind_proxy = False


def _client_identity(
    settings: RuntimeSettings, ctx: Context[AppState] | None
) -> ClientIdentity | None:
    """Usage identity for the caller. None under stdio, where the only user is local."""
    global _warned_peer_behind_proxy
    if settings.transport == "stdio" or ctx is None:
        return None
    service = ctx.request_context.lifespan_context.service
    limits = service.bundle.config.limits
    source = limits.client_address or "peer"
    request = ctx.request_context.request
    headers = getattr(request, "headers", None)
    peer = getattr(getattr(request, "client", None), "host", None)
    if (
        source == "peer"
        and limits.per_client_daily is not None
        and not _warned_peer_behind_proxy
        and headers is not None
        and headers.get("x-forwarded-for")
        and identify(peer).capped is False
    ):
        _warned_peer_behind_proxy = True
        logging.getLogger(__name__).warning(
            "The per-client cap reads the connection address, but requests arrive through "
            "a proxy. Set limits.client_address to x-forwarded-for-last if the proxy "
            "appends the client address."
        )
    return identify(client_address(headers, peer, source), service.bundle.shared_networks)


def _call_context(
    settings: RuntimeSettings,
    reporter_question: str | None,
    ctx: Context[AppState] | None = None,
) -> CallContext:
    return CallContext(
        reporter_question=(reporter_question or "").strip() or None,
        connection_id=_connection_id(settings),
        client=_client_identity(settings, ctx),
    )


def _actor_id(settings: RuntimeSettings) -> str:
    if settings.auth_mode == "open":
        return OPEN_ACTOR
    access_token = get_access_token()
    if access_token is None or not isinstance(access_token.claims, dict):
        raise PermissionError("A verified OAuth identity is required")
    email = access_token.claims.get("email")
    if not isinstance(email, str) or not email.strip():
        raise PermissionError("The OAuth identity does not include a verified email")
    return email


def _error_result(error: dict[str, Any]) -> CallToolResult:
    # Return an intentional tool error, so the SDK does not mask it as an unexpected
    # exception. Text carries the same payload for clients that ignore structuredContent.
    payload = {"error": {"argument": None, "http_status": None, **error}}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
        is_error=True,
    )


def _ok_result(payload: Any) -> CallToolResult:
    wrapped = payload if isinstance(payload, dict) else {"results": payload}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(wrapped))],
        structured_content=wrapped,
    )


def _known_error(exc: Exception) -> CallToolResult | None:
    """Translate expected failures into tool errors. Unexpected ones return a generic error."""
    if isinstance(exc, TimeoutError):
        return _error_result(
            {
                "code": "SOURCE_TIMEOUT",
                "message": "The data source did not answer in time. Narrow the query or retry.",
            }
        )
    if isinstance(exc, UsageLimitExceeded):
        return _error_result({"code": exc.code, "message": str(exc)})
    if isinstance(exc, SourceQueryError):
        return _error_result(exc.tool_error())
    if isinstance(exc, SourceError):
        return _error_result(
            {
                "code": "SOURCE_ERROR",
                "message": "OpenCivicQuery could not obtain a usable response from the source.",
            }
        )
    if isinstance(exc, SnapshotError):
        return _error_result(
            {
                "code": "SNAPSHOT_ERROR",
                "message": "OpenCivicQuery could not read or store the evidence snapshot.",
            }
        )
    if isinstance(exc, ValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        location = first.get("loc") or ()
        return _error_result(
            {
                "code": "INVALID_QUERY_ARGUMENT",
                "message": str(first.get("msg") or "Invalid argument"),
                "argument": str(location[0]) if location else None,
            }
        )
    return None


def _internal_error() -> CallToolResult:
    # Unexpected exception text may contain credentials or private storage paths.
    # The service records its diagnostic in the private audit log.
    return _error_result(
        {"code": "INTERNAL_ERROR", "message": "OpenCivicQuery encountered an unexpected failure."}
    )


def create_server(settings: RuntimeSettings) -> MCPServer[AppState]:
    verifier = None
    proxy = None
    auth: AuthSettings | None = None
    required_scopes = ["openid", "email"]
    if settings.auth_mode == "oauth":
        # Imported here so a deployment without login never loads the OAuth code path.
        from opencivicquery.oauth import GoogleWorkspaceTokenVerifier
        from opencivicquery.oauth_proxy import GoogleOAuthProxy

        assert settings.oauth_client_id is not None
        assert settings.oauth_issuer_url is not None
        assert settings.oauth_resource_url is not None
        # One parsed access list feeds both the token verifier and the per-call check,
        # so the two can never drift apart.
        access = load_deployment(settings.config_path, auth_mode="oauth").config.access
        if access is None:
            raise ConfigurationError("Login is on but the configuration has no access list")
        allowed_domains = access.allowed_email_domains
        allowed_users = access.allowed_users
        verifier = GoogleWorkspaceTokenVerifier(
            client_ids=settings.oauth_client_ids,
            required_scopes=required_scopes,
            allowed_email_domains=allowed_domains,
            allowed_users=allowed_users,
        )
        proxy = GoogleOAuthProxy(
            issuer_url=settings.oauth_issuer_url,
            client_ids=settings.oauth_client_ids,
            allowed_redirect_uris=settings.oauth_redirect_uris,
            # A hosted-domain hint would hide the account chooser from an exact
            # collaborator outside that domain. It is safe only for domain-only access.
            hosted_domain=(
                allowed_domains[0] if len(allowed_domains) == 1 and not allowed_users else None
            ),
        )
        auth = AuthSettings(
            issuer_url=settings.oauth_issuer_url,
            resource_server_url=settings.oauth_resource_url,
            required_scopes=required_scopes,
            # Google tokens carry no RFC 8707 resource claim. The verifier checks the
            # token audience against this server's own OAuth client IDs instead.
            validate_token_resource=False,
        )

    @asynccontextmanager
    async def lifespan(_: MCPServer[AppState]) -> AsyncIterator[AppState]:
        async with AsyncExitStack() as stack:
            service = _build_service(settings)
            # Callbacks run in reverse order, and each one runs even if another fails.
            stack.push_async_callback(service.close)
            state = AppState(
                service=service,
                legistar=Legistar(
                    tokens={
                        name: token.get_secret_value()
                        for name, token in settings.legistar_tokens.items()
                    }
                ),
            )
            stack.push_async_callback(state.legistar.close)
            state.sec = SecEdgar(contact=settings.sec_contact)
            stack.push_async_callback(state.sec.close)
            state.usaspending = USAspending()
            stack.push_async_callback(state.usaspending.close)
            state.govinfo = GovInfo(api_key=_secret(settings.govinfo_api_key))
            stack.push_async_callback(state.govinfo.close)
            state.census = CensusData(api_key=_secret(settings.census_api_key))
            stack.push_async_callback(state.census.close)
            state.nonprofits = NonprofitExplorer()
            stack.push_async_callback(state.nonprofits.close)
            state.openstates = OpenStates(api_key=_secret(settings.openstates_api_key))
            stack.push_async_callback(state.openstates.close)
            # The daily copy sits beside the snapshots. Without one, the tools answer live only.
            state.sfgov = SfGov(
                copy=CopyReader(
                    copy_store(
                        backend=settings.snapshot_backend,
                        path=settings.snapshot_path,
                        bucket=settings.snapshot_bucket,
                    )
                )
            )
            stack.push_async_callback(state.sfgov.close)
            if verifier is not None:
                stack.push_async_callback(verifier.close)
            if proxy is not None:
                stack.push_async_callback(proxy.close)
            yield state

    server = QueryOnlyMCPServer(
        name="opencivicquery",
        title="OpenCivicQuery",
        description="Open, auditable access to government open data for reporters.",
        instructions=_instructions(settings.tools),
        version=__version__,
        lifespan=lifespan,
        auth=auth,
        token_verifier=verifier,
        log_level=settings.log_level.upper(),
    )
    offered: list[str] = []

    def tool(*, name: str, description: str, read_only: bool = True) -> Callable[[Any], Any]:
        """Register a tool, or skip it when settings.tools names others.

        Every tool but pin_snapshot only reads records. The evidence snapshot a lookup saves is
        this server's own record of what it read, so it does not count as a change. Clients
        such as Claude's Cowork ask the user before each call of a tool that does not declare
        itself read-only, so the declaration is what lets a lookup run without a prompt.
        pin_snapshot keeps a snapshot past its normal expiry at the caller's request, so it
        declares itself not read-only and stays behind the client's approval step.
        """
        offered.append(name)
        if settings.tools and name not in settings.tools:
            return lambda function: function
        return server.tool(
            name=name,
            description=description,
            annotations=ToolAnnotations(readOnlyHint=read_only, destructiveHint=False),
        )

    @tool(
        name="list_source_packs",
        description=(
            "List the portals and datasets that a source pack documents, with the date each "
            "pack was last checked against live data. Datasets outside these packs are still "
            "queryable, but their responses carry a ROW GRAIN UNKNOWN warning."
        ),
    )
    async def list_source_packs(ctx: Context[AppState]) -> Annotated[CallToolResult, dict]:
        service = ctx.request_context.lifespan_context.service
        packs = service.list_packs(actor_id=_actor_id(settings))
        return _ok_result({"packs": [pack.model_dump(mode="json") for pack in packs]})

    @tool(
        name="search_datasets",
        description=(
            "Search the Socrata catalog for datasets. Leave domain empty to search every "
            "portal, or give a portal hostname to search one. Each result says whether a "
            "source pack documents it. Search results do not carry a quality, review, "
            "reliability, or publication status."
        ),
    )
    async def search_datasets(
        search_term: Annotated[str, Field(min_length=1, max_length=200)],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        domain: Annotated[str | None, DOMAIN_FIELD] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> Annotated[CallToolResult, dict]:
        service = ctx.request_context.lifespan_context.service
        try:
            entries = await service.search_datasets(
                actor_id=_actor_id(settings),
                context=_call_context(settings, reporter_question, ctx),
                search_term=search_term,
                limit=limit,
                domain=domain,
            )
        except Exception as exc:
            return _known_error(exc) or _internal_error()
        return _ok_result({"results": [entry.model_dump(mode="json") for entry in entries]})

    @tool(
        name="describe_dataset",
        description=(
            "Return a dataset schema, source update time, and, when a source pack documents "
            "it, the row grain, counting guidance, and caveats. Call this before writing a "
            "query."
        ),
    )
    async def describe_dataset(
        domain: Annotated[str, DOMAIN_FIELD],
        dataset_id: Annotated[str, DATASET_ID_FIELD],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
    ) -> Annotated[CallToolResult, dict]:
        service = ctx.request_context.lifespan_context.service
        try:
            description = await service.describe_dataset(
                actor_id=_actor_id(settings),
                context=_call_context(settings, reporter_question, ctx),
                domain=domain,
                dataset_id=dataset_id,
            )
        except Exception as exc:
            return _known_error(exc) or _internal_error()
        return _ok_result(description.model_dump(mode="json"))

    @tool(
        name="query_dataset",
        description=(
            "Run a read-only SoQL query with a mandatory row ceiling. The response on success "
            "states completeness, warnings, methodology, source update time, the public "
            "dataset page, the exact query, a public live-result query_url when the source "
            "supports one, and the URI of an immutable evidence snapshot. If completeness is "
            "truncated, revise the query or report the limitation. The response also carries "
            "presentation_rules, which govern how you must state its figures to the user; "
            "obey every rule listed there before you write your reply. Failures return "
            "isError=true and an error object with the failure code, message, argument, and "
            "http_status. A failed query has no data result or snapshot."
        ),
    )
    async def query_dataset(
        domain: Annotated[str, DOMAIN_FIELD],
        dataset_id: Annotated[str, DATASET_ID_FIELD],
        select: Annotated[str, CLAUSE_FIELD],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        where: Annotated[str | None, CLAUSE_FIELD] = None,
        group_by: Annotated[str | None, CLAUSE_FIELD] = None,
        order_by: Annotated[str | None, CLAUSE_FIELD] = None,
        max_rows: Annotated[int, Field(ge=1, le=5000)] = 1000,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        service = ctx.request_context.lifespan_context.service
        try:
            request = QueryRequest(
                domain=domain,
                dataset_id=dataset_id,
                select=select,
                where=where,
                group_by=group_by,
                order_by=order_by,
                max_rows=max_rows,
            )
            response = await service.query_dataset(
                actor_id=_actor_id(settings),
                context=_call_context(settings, reporter_question, ctx),
                request=request,
            )
        except Exception as exc:
            return _known_error(exc) or _internal_error()
        return _ok_result(response.model_dump(mode="json"))

    @tool(
        name="geocode_address",
        description=(
            "Look up one U.S. street address in the U.S. Census Bureau's Census Geocoder. "
            "Returns each matched address with approximate coordinates and the census "
            "geographies that contain it: state, county, county subdivision, place, census "
            "tract, 2020 census block, congressional and state legislative districts. Use it "
            "to join an address in a records query to census geography. The response has the "
            "same shape as query_dataset, including warnings, presentation_rules, a public "
            "query_url, and an evidence snapshot; obey every presentation rule. Use it only "
            "for places in public records or places the user names. Never use it to find "
            "where a private person lives, or to turn an address that a portal generalized "
            "on purpose, such as a block or an intersection, into a point. Failures return "
            "isError=true with an error object."
        ),
    )
    async def geocode_address(
        address: Annotated[
            str,
            Field(
                description=(
                    "One street address on a single line, with city and state or ZIP code, "
                    f"at most {MAX_ADDRESS_LENGTH} characters."
                ),
                min_length=1,
                max_length=2 * MAX_ADDRESS_LENGTH,
            ),
        ],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        service = ctx.request_context.lifespan_context.service
        try:
            response = await service.geocode_address(
                actor_id=_actor_id(settings),
                context=_call_context(settings, reporter_question, ctx),
                address=address,
            )
        except Exception as exc:
            return _known_error(exc) or _internal_error()
        return _ok_result(response.model_dump(mode="json"))

    async def run_lookup(
        ctx: Context[AppState],
        *,
        action: str,
        reporter_question: str,
        arguments: dict[str, Any],
        prepare: Callable[[AppState], PreparedLookup],
        host: str,
    ) -> CallToolResult:
        state = ctx.request_context.lifespan_context
        try:
            response = await state.service.lookup(
                action=action,
                actor_id=_actor_id(settings),
                context=_call_context(settings, reporter_question, ctx),
                arguments=arguments,
                prepare=lambda: prepare(state),
                host=host,
            )
        except Exception as exc:
            return _known_error(exc) or _internal_error()
        return _ok_result(response.model_dump(mode="json"))

    def legistar(state: AppState) -> Legistar:
        if state.legistar is None:
            raise RuntimeError("The Legistar source was not built")
        return state.legistar

    @tool(
        name="find_meetings",
        description=(
            "Find public meetings of a city or county that publishes its agendas in Granicus "
            "Legistar: councils, committees, boards, and commissions. Returns each meeting "
            "with its agenda and minutes PDF links, the minutes status, and the meeting page "
            "to cite. The response has the same shape as query_dataset, including warnings, "
            "presentation_rules, a public query_url, and an evidence snapshot; obey every "
            "presentation rule. Some jurisdictions, such as New York City, require their own "
            "token, and a tool error says when this server lacks one. When a jurisdiction's "
            "API cannot list meetings because of a setting on its side, as San Francisco's "
            "cannot, the tool reads its public Legistar calendar page instead, at most two "
            "calendar years per call, and says so; pass such a meeting's EventId and "
            "EventGuid to get_meeting_items and get_item_votes. Failures return isError=true "
            "with an error object."
        ),
    )
    async def find_meetings(
        client: Annotated[str, LEGISTAR_CLIENT_FIELD],
        start_date: Annotated[str, DATE_FIELD],
        end_date: Annotated[str, DATE_FIELD],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        body: Annotated[
            str | None,
            Field(
                description=(
                    "Optional words from the body's name, such as City Council or Planning "
                    "Commission. Matches anywhere in the name, ignoring case."
                ),
                min_length=1,
                max_length=LEGISTAR_MAX_TEXT,
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=LEGISTAR_MAX_LIMIT)] = 50,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {
            "client": client,
            "start_date": start_date,
            "end_date": end_date,
            "body": body,
            "limit": limit,
        }
        return await run_lookup(
            ctx,
            action="find_meetings",
            reporter_question=reporter_question,
            arguments=arguments,
            host=LEGISTAR_HOST,
            prepare=lambda state: legistar(state).find_meetings(**arguments),
        )

    @tool(
        name="get_meeting_items",
        description=(
            "List the agenda items of one Legistar meeting, with the action, result, tally, "
            "mover, and seconder the clerk recorded for each. Use an EventId that "
            "find_meetings returned for the same client. When the jurisdiction's API cannot "
            "answer or has stopped updating, as San Francisco's has, pass event_guid too: "
            "the tool then reads the meeting's public Legistar page and says so. That page "
            "has one row per action recorded on a legislative file, with file number, "
            "action, and result. Same response shape and rules as query_dataset."
        ),
    )
    async def get_meeting_items(
        client: Annotated[str, LEGISTAR_CLIENT_FIELD],
        event_id: Annotated[int, Field(description="EventId from find_meetings.", ge=1)],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        event_guid: Annotated[str | None, EVENT_GUID_FIELD] = None,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {"client": client, "event_id": event_id, "event_guid": event_guid}
        return await run_lookup(
            ctx,
            action="get_meeting_items",
            reporter_question=reporter_question,
            arguments=arguments,
            host=LEGISTAR_HOST,
            prepare=lambda state: legistar(state).get_meeting_items(**arguments),
        )

    @tool(
        name="get_item_votes",
        description=(
            "List each member's vote on one agenda item of a Legistar meeting, as the clerk "
            "recorded it. Use the EventId from find_meetings and the EventItemId from "
            "get_meeting_items. When the jurisdiction's API cannot answer or has stopped "
            "updating, as San Francisco's has, pass event_guid too: the tool then reads the "
            "roll call from the action's public Legistar page and says so. A matter's "
            "History from search_legislation gives the EventId, EventGuid, and EventItemId "
            "of each action. Same response shape and rules as query_dataset."
        ),
    )
    async def get_item_votes(
        client: Annotated[str, LEGISTAR_CLIENT_FIELD],
        event_id: Annotated[int, Field(description="EventId from find_meetings.", ge=1)],
        event_item_id: Annotated[
            int, Field(description="EventItemId from get_meeting_items.", ge=1)
        ],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        event_guid: Annotated[str | None, EVENT_GUID_FIELD] = None,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {
            "client": client,
            "event_id": event_id,
            "event_item_id": event_item_id,
            "event_guid": event_guid,
        }
        return await run_lookup(
            ctx,
            action="get_item_votes",
            reporter_question=reporter_question,
            arguments=arguments,
            host=LEGISTAR_HOST,
            prepare=lambda state: legistar(state).get_item_votes(**arguments),
        )

    @tool(
        name="search_legislation",
        description=(
            "Search a jurisdiction's Legistar legislation, such as ordinances, resolutions, "
            "and reports, by words in the title or name or by exact file number, optionally "
            "within an introduction date range. Returns each matter's type, status, body, "
            "dates, and page link, newest first. When the jurisdiction's API copy has "
            "stopped updating or cannot answer, as San Francisco's has, the tool reads the "
            "public Legistar search page and matter pages instead and says so: it returns "
            "current matters, a file-number match first and the rest in the page's own "
            "order, with sponsors for the first ten listed, and, for a search by one file "
            "number, that matter's action history with the IDs get_item_votes needs. Same "
            "response shape and rules as query_dataset."
        ),
    )
    async def search_legislation(
        client: Annotated[str, LEGISTAR_CLIENT_FIELD],
        text: Annotated[
            str,
            Field(
                description=(
                    "Words to find in a matter's title or name, or an exact file number "
                    "such as 24-0624. Matches inside longer words too."
                ),
                min_length=2,
                max_length=LEGISTAR_MAX_TEXT,
            ),
        ],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        introduced_from: Annotated[str | None, DATE_FIELD] = None,
        introduced_to: Annotated[str | None, DATE_FIELD] = None,
        limit: Annotated[int, Field(ge=1, le=LEGISTAR_MAX_LIMIT)] = 50,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {
            "client": client,
            "text": text,
            "introduced_from": introduced_from,
            "introduced_to": introduced_to,
            "limit": limit,
        }
        return await run_lookup(
            ctx,
            action="search_legislation",
            reporter_question=reporter_question,
            arguments=arguments,
            host=LEGISTAR_HOST,
            prepare=lambda state: legistar(state).search_legislation(**arguments),
        )

    def sec(state: AppState) -> SecEdgar:
        if state.sec is None:
            raise RuntimeError("The SEC source was not built")
        return state.sec

    @tool(
        name="find_sec_filings",
        description=(
            "List one company's filings in the SEC's EDGAR system, newest first, with links "
            "to each filing index and main document. Give a ticker, a CIK, or the exact EDGAR "
            "company name; a name that is not exact returns candidates and is never guessed. "
            "A row shows that a filing was submitted and says nothing about its content. Same "
            "response shape and rules as query_dataset. Failures return isError=true."
        ),
    )
    async def find_sec_filings(
        company: Annotated[
            str,
            Field(
                description=(
                    "A ticker such as AAPL, a CIK of up to 10 digits, or the exact company "
                    "name as EDGAR shows it."
                ),
                min_length=1,
                max_length=MAX_COMPANY_LENGTH,
            ),
        ],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        form_types: Annotated[
            list[Annotated[str, Field(min_length=1, max_length=25)]] | None,
            Field(
                description=(
                    "EDGAR form types, matched exactly, such as 10-K, 10-K/A, 8-K, 4, or D."
                ),
                max_length=MAX_FORM_TYPES,
            ),
        ] = None,
        filed_after: Annotated[
            str | None,
            Field(
                description="Earliest filing date to include, YYYY-MM-DD, inclusive.",
                min_length=10,
                max_length=10,
            ),
        ] = None,
        filed_before: Annotated[
            str | None,
            Field(
                description="Latest filing date to include, YYYY-MM-DD, inclusive.",
                min_length=10,
                max_length=10,
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=SEC_MAX_LIMIT)] = SEC_DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {
            "company": company,
            "form_types": form_types,
            "filed_after": filed_after,
            "filed_before": filed_before,
            "limit": limit,
        }
        return await run_lookup(
            ctx,
            action="find_sec_filings",
            reporter_question=reporter_question,
            arguments=arguments,
            host=SEC_HOST,
            prepare=lambda state: sec(state).find_filings(**arguments),
        )

    def usaspending(state: AppState) -> USAspending:
        if state.usaspending is None:
            raise RuntimeError("The USAspending source was not built")
        return state.usaspending

    @tool(
        name="search_federal_awards",
        description=(
            "Find federal prime awards on USAspending.gov, such as contracts, grants, loans, "
            "direct payments, or other assistance, by keyword, recipient name, or the state "
            "and county where the work is done. Rows are sorted by award amount, largest "
            "first, and each links to its public award page. The date range returns awards "
            "with activity in that period, so check each award's start date before calling it "
            "new. Same response shape and rules as query_dataset; this source offers no "
            "rerunnable link, so the response carries the exact request body instead."
        ),
    )
    async def search_federal_awards(
        award_type: Annotated[
            Literal["contracts", "grants", "loans", "direct_payments", "other"],
            Field(description="One award group; grants include cooperative agreements."),
        ],
        start_date: Annotated[str, DATE_FIELD],
        end_date: Annotated[str, DATE_FIELD],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        keywords: Annotated[
            list[Annotated[str, Field(min_length=1, max_length=MAX_KEYWORD_LENGTH)]] | None,
            Field(
                description=(
                    "Plain words or phrases; an award matching any one is included. Give "
                    "keywords, recipient, or state."
                ),
                max_length=MAX_KEYWORDS,
            ),
        ] = None,
        recipient: Annotated[
            str | None,
            Field(
                description="Part of a recipient or parent company name, or a UEI.",
                min_length=1,
                max_length=MAX_RECIPIENT_LENGTH,
            ),
        ] = None,
        state: Annotated[
            str | None,
            Field(
                description="Two-letter postal code of the place of performance.",
                min_length=2,
                max_length=2,
            ),
        ] = None,
        county: Annotated[
            str | None,
            Field(
                description=(
                    "Three-digit county FIPS code of the place of performance; needs state."
                ),
                min_length=3,
                max_length=3,
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=AWARDS_MAX_LIMIT)] = AWARDS_DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {
            "award_type": award_type,
            "start_date": start_date,
            "end_date": end_date,
            "keywords": keywords,
            "recipient": recipient,
            "state": state,
            "county": county,
            "limit": limit,
        }
        return await run_lookup(
            ctx,
            action="search_federal_awards",
            reporter_question=reporter_question,
            arguments=arguments,
            host=USASPENDING_HOST,
            prepare=lambda state_: usaspending(state_).search_awards(**arguments),
        )

    def govinfo(state: AppState) -> GovInfo:
        if state.govinfo is None:
            raise RuntimeError("The GovInfo source was not built")
        return state.govinfo

    @tool(
        name="search_court_opinions",
        description=(
            "Search federal court opinions in GovInfo's United States Courts Opinions "
            "collection. Returns GovInfo's count of matching opinion documents and the "
            "top-ranked page, with a public link to each opinion. Use it for what opinions "
            "say. It covers only selected federal courts, mostly from 2004 on, and holds no "
            "dockets. Same response shape and rules as query_dataset."
        ),
    )
    async def search_court_opinions(
        text: Annotated[
            str,
            Field(
                description=(
                    "Words that must all appear in the opinion. Punctuation and search "
                    "operators count as plain words."
                ),
                min_length=1,
                max_length=OPINIONS_MAX_TEXT,
            ),
        ],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        court: Annotated[
            str | None,
            Field(
                description="Optional GovInfo court code, such as ca9 or cand.",
                min_length=2,
                max_length=10,
            ),
        ] = None,
        issued_from: Annotated[str | None, DATE_FIELD] = None,
        issued_to: Annotated[str | None, DATE_FIELD] = None,
        limit: Annotated[int, Field(ge=1, le=OPINIONS_MAX_LIMIT)] = OPINIONS_DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {
            "text": text,
            "court": court,
            "issued_from": issued_from,
            "issued_to": issued_to,
            "limit": limit,
        }
        return await run_lookup(
            ctx,
            action="search_court_opinions",
            reporter_question=reporter_question,
            arguments=arguments,
            host=GOVINFO_HOST,
            prepare=lambda state_: govinfo(state_).search_opinions(**arguments),
        )

    def nonprofits(state: AppState) -> NonprofitExplorer:
        if state.nonprofits is None:
            raise RuntimeError("The Nonprofit Explorer source was not built")
        return state.nonprofits

    @tool(
        name="search_nonprofits",
        description=(
            "Find tax-exempt organizations by name in ProPublica's Nonprofit Explorer, which "
            "republishes the IRS list of exempt organizations. Returns each match's EIN, "
            "name, city, and state, in ProPublica's relevance order, at most 25 per call. The "
            "words also match city names, so check each row. Use an EIN with "
            "get_nonprofit_filings. Same response shape and rules as query_dataset."
        ),
    )
    async def search_nonprofits(
        name: Annotated[
            str,
            Field(
                description="Words from the organization's name, in plain words.",
                min_length=1,
                max_length=NONPROFIT_MAX_NAME,
            ),
        ],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        state: Annotated[
            str | None,
            Field(
                description="Optional two-letter postal code where the organization is based.",
                min_length=2,
                max_length=2,
            ),
        ] = None,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {"name": name, "state": state}
        return await run_lookup(
            ctx,
            action="search_nonprofits",
            reporter_question=reporter_question,
            arguments=arguments,
            host=NONPROFITS_HOST,
            prepare=lambda state_: nonprofits(state_).search_nonprofits(**arguments),
        )

    @tool(
        name="get_nonprofit_filings",
        description=(
            "List one tax-exempt organization's IRS Form 990, 990-EZ, and 990-PF filings "
            "from ProPublica's Nonprofit Explorer, newest tax period first, with total "
            "revenue, expenses, assets, and liabilities where the IRS extracted them, and a "
            "link to each filed document. Give the organization's EIN from search_nonprofits. "
            "Same response shape and rules as query_dataset."
        ),
    )
    async def get_nonprofit_filings(
        ein: Annotated[
            str,
            Field(
                description="The organization's nine-digit EIN, such as 94-6068564.",
                min_length=9,
                max_length=10,
            ),
        ],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {"ein": ein}
        return await run_lookup(
            ctx,
            action="get_nonprofit_filings",
            reporter_question=reporter_question,
            arguments=arguments,
            host=NONPROFITS_HOST,
            prepare=lambda state_: nonprofits(state_).get_filings(**arguments),
        )

    def sfgov(state: AppState) -> SfGov:
        if state.sfgov is None:
            raise RuntimeError("The SF.gov source was not built")
        return state.sfgov

    @tool(
        name="find_sfgov_news",
        description=(
            "List news pages on SF.gov, San Francisco's city website, dated in a range, "
            "newest first, for one agency or for all. Returns each page's date, title, "
            "type label, summary, and public link. Give agency as the exact name SF.gov "
            "uses, such as Office of the Mayor; an inexact name returns the names that "
            "match instead of a guess. The lookup reads the 500 newest items and reports a "
            "range it could not reach as partial. The server runs one SF.gov lookup at a "
            "time, so call the SF.gov tools one after another. When SF.gov cannot be reached, "
            "the answer comes from a daily copy and says so. The response carries warnings, "
            "presentation_rules to obey, and an evidence snapshot."
        ),
    )
    async def find_sfgov_news(
        start_date: Annotated[str, DATE_FIELD],
        end_date: Annotated[str, DATE_FIELD],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        agency: Annotated[str | None, SFGOV_OPTIONAL_AGENCY_FIELD] = None,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {"agency": agency, "start_date": start_date, "end_date": end_date}
        return await run_lookup(
            ctx,
            action="find_sfgov_news",
            reporter_question=reporter_question,
            arguments=arguments,
            host=SFGOV_HOST,
            prepare=lambda state_: sfgov(state_).find_news(**arguments),
        )

    @tool(
        name="find_sfgov_meetings",
        description=(
            "List the meeting pages one San Francisco board, commission, or department "
            "published on SF.gov for a date range, newest first. Returns each page's "
            "meeting date, start time, title, cancelled flag, and link (the page carries "
            "the agenda), the files staff attached with their headings, such as minutes, "
            "and video links. Give agency as the exact name SF.gov uses, such as Police "
            "Commission; an inexact name returns the names that match. SF.gov has no "
            "minutes field, so read a file's heading and title before you call it the "
            "minutes. A page with no readable date is listed last whatever the dates. For "
            "the Board of Supervisors and its committees, use find_meetings with client "
            "sfgov. The server runs one SF.gov lookup at a time, so call the SF.gov tools "
            "one after another. When SF.gov cannot be reached, the answer comes from a daily "
            "copy and says so. The response carries warnings, presentation_rules to obey, "
            "and an evidence snapshot."
        ),
    )
    async def find_sfgov_meetings(
        agency: Annotated[str, SFGOV_AGENCY_FIELD],
        start_date: Annotated[str, DATE_FIELD],
        end_date: Annotated[str, DATE_FIELD],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {"agency": agency, "start_date": start_date, "end_date": end_date}
        return await run_lookup(
            ctx,
            action="find_sfgov_meetings",
            reporter_question=reporter_question,
            arguments=arguments,
            host=SFGOV_HOST,
            prepare=lambda state_: sfgov(state_).find_meetings(**arguments),
        )

    @tool(
        name="search_sfgov",
        description=(
            "Find pages on SF.gov, San Francisco's city website, by words in the title, or "
            "files in SF.gov's document library by words in the title or description. A "
            "page search covers every kind of page: news, meetings, reports, agency pages, "
            "and events. Returns at most 50 rows in the site's own order, each with its "
            "public link. It cannot be limited to one agency; for one agency's news or "
            "meetings use find_sfgov_news or find_sfgov_meetings. The server runs one "
            "SF.gov lookup at a time, so call the SF.gov tools one after another. When SF.gov "
            "cannot be reached, the answer comes from a daily copy and says so. The response "
            "carries warnings, presentation_rules to obey, and an evidence snapshot."
        ),
    )
    async def search_sfgov(
        text: Annotated[
            str,
            Field(
                description="Plain words from the title you are looking for.",
                max_length=SFGOV_MAX_TEXT,
            ),
        ],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        kind: Annotated[
            Literal["pages", "documents"],
            Field(
                description=(
                    "pages searches page titles; documents searches files in the document "
                    "library, such as reports and minutes."
                )
            ),
        ] = "pages",
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {"text": text, "kind": kind}
        return await run_lookup(
            ctx,
            action="search_sfgov",
            reporter_question=reporter_question,
            arguments=arguments,
            host=SFGOV_HOST,
            prepare=lambda state_: sfgov(state_).search(**arguments),
        )

    @tool(
        name="get_sfgov_page",
        description=(
            "Return the text of one SF.gov news or meeting page: for news, the body; for a "
            "meeting page, the overview and each agenda item's title and text. Give the "
            "page_id of a row from find_sfgov_news, find_sfgov_meetings, or search_sfgov. The "
            "server runs one SF.gov lookup at a time, so call the SF.gov tools one after "
            "another. When SF.gov cannot be reached, the answer comes from a daily copy and "
            "says so. The response carries warnings, presentation_rules to obey, and an "
            "evidence snapshot."
        ),
    )
    async def get_sfgov_page(
        page_id: Annotated[
            int,
            Field(
                description="The page_id of a news or meeting page row.",
                ge=1,
                le=SFGOV_MAX_PAGE_ID,
            ),
        ],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {"page_id": page_id}
        return await run_lookup(
            ctx,
            action="get_sfgov_page",
            reporter_question=reporter_question,
            arguments=arguments,
            host=SFGOV_HOST,
            prepare=lambda state_: sfgov(state_).get_page(**arguments),
        )

    def openstates(state: AppState) -> OpenStates:
        if state.openstates is None:
            raise RuntimeError("The Open States source was not built")
        return state.openstates

    @tool(
        name="search_california_bills",
        description=(
            "Search bills and resolutions of the California Legislature, through Open States, "
            "by words, by the member who sponsored them, or by recent action, in one session. "
            "Returns Open States' count of matches and the first 20, most recent action "
            "first, each with its status and a link to the Legislature's page. Same response "
            "shape and rules as query_dataset."
        ),
    )
    async def search_california_bills(
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        text: Annotated[
            str | None,
            Field(
                description=(
                    "Optional words to look for, or a bill number such as SB 79, which is "
                    "matched as that number."
                ),
                max_length=STATE_MAX_TEXT,
            ),
        ] = None,
        session: Annotated[str | None, STATE_SESSION_FIELD] = None,
        sponsor: Annotated[
            str | None,
            Field(
                description=(
                    "Optional member's name as the Legislature prints it on bills, usually "
                    "the surname, such as Wiener, or an Open States person id."
                ),
                max_length=STATE_MAX_SPONSOR,
            ),
        ] = None,
        role: Annotated[
            Literal["any", "author"],
            Field(description="With a sponsor: any includes coauthored bills; author does not."),
        ] = "any",
        action_since: Annotated[
            str | None,
            Field(
                description=(
                    "Optional YYYY-MM-DD: keep bills whose latest action is on or after it."
                ),
                max_length=10,
            ),
        ] = None,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {
            "text": text,
            "session": session,
            "sponsor": sponsor,
            "role": role,
            "action_since": action_since,
        }
        return await run_lookup(
            ctx,
            action="search_california_bills",
            reporter_question=reporter_question,
            arguments=arguments,
            host=OPENSTATES_HOST,
            prepare=lambda state_: openstates(state_).search_bills(**arguments),
        )

    @tool(
        name="get_california_bill",
        description=(
            "Read one California bill or resolution through Open States: its authors and "
            "coauthors, every recorded action, and each roll call with its tally and how each "
            "member voted. Same response shape and rules as query_dataset."
        ),
    )
    async def get_california_bill(
        bill: Annotated[
            str,
            Field(description="The measure number, such as SB 79 or AB 1.", max_length=12),
        ],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        session: Annotated[str | None, STATE_SESSION_FIELD] = None,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {"bill": bill, "session": session}
        return await run_lookup(
            ctx,
            action="get_california_bill",
            reporter_question=reporter_question,
            arguments=arguments,
            host=OPENSTATES_HOST,
            prepare=lambda state_: openstates(state_).get_bill(**arguments),
        )

    def census(state: AppState) -> CensusData:
        if state.census is None:
            raise RuntimeError("The Census data source was not built")
        return state.census

    @tool(
        name="get_census_data",
        description=(
            "Get American Community Survey 5-year estimates from the U.S. Census Bureau's "
            "Census Data API for one or more geographies, such as every tract in a county. "
            "Each row is one geography, with the requested variables exactly as the API "
            "returns them, and the dataset name carries their official labels. Request each "
            "estimate's margin-of-error variable (E becomes M) and include NAME. At most "
            f"{MAX_VARIABLES} variables and 5,000 geographies per call. Same response shape "
            "and rules as query_dataset."
        ),
    )
    async def get_census_data(
        year: Annotated[
            int,
            Field(description="Final year of the 5-year period; 2024 means 2020 to 2024."),
        ],
        variables: Annotated[
            list[Annotated[str, Field(min_length=1, max_length=50)]],
            Field(
                description="Variable names, such as NAME, B19013_001E, B19013_001M.",
                min_length=1,
                max_length=MAX_VARIABLES,
            ),
        ],
        geography_for: Annotated[
            str,
            Field(
                description="One level:codes clause, such as tract:*, county:075, place:67000.",
                min_length=3,
                max_length=500,
            ),
        ],
        reporter_question: Annotated[str, REPORTER_QUESTION_FIELD],
        ctx: Context[AppState],
        geography_in: Annotated[
            str | None,
            Field(
                description='Optional parent clauses, such as "state:06 county:075".',
                min_length=3,
                max_length=500,
            ),
        ] = None,
        dataset: Annotated[
            Literal["acs/acs5", "acs/acs5/subject", "acs/acs5/profile"],
            Field(description="Detailed tables, subject tables, or data profiles."),
        ] = "acs/acs5",
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        arguments = {
            "year": year,
            "variables": variables,
            "geography_for": geography_for,
            "geography_in": geography_in,
            "dataset": dataset,
        }
        return await run_lookup(
            ctx,
            action="get_census_data",
            reporter_question=reporter_question,
            arguments=arguments,
            host=CENSUS_DATA_HOST,
            prepare=lambda state_: census(state_).get_data(**arguments),
        )

    @tool(
        name="pin_snapshot",
        description=(
            "Preserve a temporary query snapshot indefinitely for a published story. Use the "
            "snapshot URI that a query or lookup tool returned."
        ),
        read_only=False,
    )
    async def pin_snapshot(
        snapshot_uri: Annotated[str, SNAPSHOT_URI_FIELD], ctx: Context[AppState]
    ) -> Annotated[CallToolResult, dict]:
        service = ctx.request_context.lifespan_context.service
        try:
            reference = await service.pin_snapshot(
                actor_id=_actor_id(settings),
                context=_call_context(settings, None, ctx),
                snapshot_uri=snapshot_uri,
            )
        except Exception as exc:
            return _known_error(exc) or _internal_error()
        return _ok_result(reference.model_dump(mode="json"))

    @tool(
        name="get_snapshot",
        description=(
            "Read a temporary or pinned evidence snapshot for verification. The snapshot "
            "contains the exact query, frozen rows, source update time, completeness, "
            "methodology, and warnings."
        ),
    )
    async def get_snapshot(
        snapshot_uri: Annotated[str, SNAPSHOT_URI_FIELD], ctx: Context[AppState]
    ) -> Annotated[CallToolResult, dict]:
        service = ctx.request_context.lifespan_context.service
        try:
            snapshot = await service.get_snapshot(
                actor_id=_actor_id(settings),
                context=_call_context(settings, None, ctx),
                snapshot_uri=snapshot_uri,
            )
        except Exception as exc:
            return _known_error(exc) or _internal_error()
        return _ok_result(snapshot.model_dump(mode="json"))

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "opencivicquery", "version": __version__})

    if proxy is not None:
        # Custom routes sit outside RequireAuthMiddleware, which only wraps the MCP
        # path. That is required here: a client cannot hold a token before it has
        # walked through these endpoints to obtain one.
        server.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])(
            proxy.authorization_server_metadata
        )
        server.custom_route("/oauth/authorize", methods=["GET"])(proxy.authorize)
        server.custom_route("/oauth/callback", methods=["POST"])(proxy.callback)
        server.custom_route("/oauth/token", methods=["POST"])(proxy.token)

    # This catalog is static. MCP 2.2.0 otherwise advertises subscriptions and
    # holds a listen POST open even with json_response/stateless_http enabled,
    # which keeps an instance billed while idle. The SDK has no public opt-out;
    # protocol tests guard this pinned API seam.
    server._lowlevel_server._request_handlers.pop("subscriptions/listen", None)
    unknown = sorted(set(settings.tools) - set(offered))
    if unknown:
        raise ConfigurationError(f"OPENCIVICQUERY_TOOLS names tools this server lacks: {unknown}")
    return server


def main() -> None:
    settings = RuntimeSettings.from_environment()
    logging.basicConfig(level=settings.log_level.upper())
    # Must run before the first outbound request: the HTTP client logs URLs, and the
    # OAuth tokeninfo URL carries a live access token.
    install_log_redaction()
    server = create_server(settings)
    if settings.transport == "stdio":
        server.run(transport="stdio")
        return
    server.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()

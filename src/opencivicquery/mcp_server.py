from __future__ import annotations

import hashlib
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent
from pydantic import Field, ValidationError
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
from opencivicquery.sources.socrata import SocrataError, SocrataNetwork, SocrataQueryError

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
    "Every successful query returns a citable snapshot.\n\n"
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


@dataclass
class AppState:
    service: OpenCivicQueryService


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
    description="The snapshot URI that query_dataset returned.",
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
    if isinstance(exc, SocrataQueryError):
        return _error_result(exc.tool_error())
    if isinstance(exc, SocrataError):
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
        service = _build_service(settings)
        try:
            yield AppState(service=service)
        finally:
            await service.close()
            if verifier is not None:
                await verifier.close()
            if proxy is not None:
                await proxy.close()

    server = QueryOnlyMCPServer(
        name="opencivicquery",
        title="OpenCivicQuery",
        description="Open, auditable access to government open data for reporters.",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
        auth=auth,
        token_verifier=verifier,
        log_level=settings.log_level.upper(),
    )

    @server.tool(
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

    @server.tool(
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

    @server.tool(
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

    @server.tool(
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

    @server.tool(
        name="pin_snapshot",
        description=(
            "Preserve a temporary query snapshot indefinitely for a published story. Use the "
            "snapshot URI returned by query_dataset."
        ),
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

    @server.tool(
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

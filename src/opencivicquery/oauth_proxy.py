from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUER = "https://accounts.google.com"
OAUTH_CALLBACK_PATH = "/oauth/callback"

# Google grants a refresh token through the non-standard `access_type=offline`
# parameter and never advertises `offline_access` in its own metadata, so an MCP
# client reading Google's discovery document never asks for one and the reporter is
# forced through a fresh consent screen every hour. This proxy advertises
# `offline_access` on its own metadata, forces the two Google-specific parameters on
# the authorization request, and reports the scope back as granted. Nothing else is
# rewritten: authorization codes, tokens, PKCE verifiers and client credentials all
# belong to Google and pass through untouched.
OFFLINE_ACCESS_SCOPE = "offline_access"

# Only scopes Google recognises may reach it. An unknown scope, `offline_access`
# included, is rejected upstream as `invalid_scope`.
UPSTREAM_SCOPES = ("openid", "email")

AUTHORIZE_PASSTHROUGH_PARAMS = (
    "client_id",
    "redirect_uri",
    "response_type",
    "state",
    "code_challenge",
    "code_challenge_method",
    "nonce",
    "login_hint",
)

TOKEN_PASSTHROUGH_FIELDS = (
    "grant_type",
    "code",
    "redirect_uri",
    "client_id",
    "client_secret",
    "code_verifier",
    "refresh_token",
)

UPSTREAM_TIMEOUT_SECONDS = 8.0
MAX_STATE_LENGTH = 2048
MAX_UPSTREAM_STATE_LENGTH = 4096
MAX_AUTHORIZATION_RESPONSE_VALUE_LENGTH = 4096
OAUTH_ERROR_PATTERN = re.compile(r"[A-Za-z0-9._~-]{1,128}\Z")
RESERVED_REDIRECT_QUERY_FIELDS = frozenset({"code", "error", "iss", "state"})


def _append_query(url: str, params: dict[str, str]) -> str:
    parsed = urlsplit(url)
    query = [*parse_qsl(parsed.query, keep_blank_values=True), *params.items()]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _encode_state(redirect_uri: str, state: str) -> str:
    payload = json.dumps(
        {"version": 1, "redirect_uri": redirect_uri, "state": state},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_state(value: str) -> tuple[str, str]:
    if not value or len(value) > MAX_UPSTREAM_STATE_LENGTH:
        raise ValueError("The OAuth state is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("The OAuth state is invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("The OAuth state is invalid")
    redirect_uri = payload.get("redirect_uri")
    state = payload.get("state")
    if (
        not isinstance(redirect_uri, str)
        or not isinstance(state, str)
        or not state
        or len(state) > MAX_STATE_LENGTH
    ):
        raise ValueError("The OAuth state is invalid")
    return redirect_uri, state


def _oauth_error(error: str, description: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
    )


def _basic_client_id(authorization: str) -> str:
    scheme, separator, encoded_credentials = authorization.partition(" ")
    if scheme.lower() != "basic" or not separator or not encoded_credentials:
        raise ValueError("The client authentication method is invalid")
    try:
        decoded = base64.b64decode(encoded_credentials, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("The client authentication method is invalid") from exc
    encoded_client_id, separator, _ = decoded.partition(":")
    if not separator or not encoded_client_id:
        raise ValueError("The client authentication method is invalid")
    return unquote_plus(encoded_client_id)


class GoogleOAuthProxy:
    """Present Google Sign-In to an MCP client as a refresh-capable authorization server.

    The proxy holds no state and issues no tokens of its own. Every credential in the
    flow is minted and validated by Google.
    """

    def __init__(
        self,
        *,
        issuer_url: str,
        client_ids: tuple[str, ...],
        allowed_redirect_uris: tuple[str, ...],
        hosted_domain: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        issuer = issuer_url.strip().rstrip("/")
        parsed_issuer = urlsplit(issuer)
        if (
            parsed_issuer.scheme != "https"
            or not parsed_issuer.netloc
            or parsed_issuer.query
            or parsed_issuer.fragment
            or parsed_issuer.username
            or parsed_issuer.password
        ):
            raise ValueError("An issuer URL is required")
        expected_client_ids = {
            client_id.strip() for client_id in client_ids if client_id.strip()
        }
        if not expected_client_ids:
            raise ValueError("At least one OAuth client ID is required")
        redirect_uris = tuple(
            dict.fromkeys(uri.strip() for uri in allowed_redirect_uris if uri.strip())
        )
        if not redirect_uris:
            raise ValueError("At least one OAuth redirect URI is required")
        for redirect_uri in redirect_uris:
            parsed = urlsplit(redirect_uri)
            query_fields = {name for name, _ in parse_qsl(parsed.query, keep_blank_values=True)}
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.fragment
                or parsed.username
                or parsed.password
                or query_fields & RESERVED_REDIRECT_QUERY_FIELDS
            ):
                raise ValueError(f"Invalid OAuth redirect URI: {redirect_uri}")
        self._issuer = issuer
        self._client_ids = frozenset(expected_client_ids)
        self._allowed_redirect_uris = frozenset(redirect_uris)
        self._upstream_callback_uri = f"{issuer}{OAUTH_CALLBACK_PATH}"
        self._hosted_domain = hosted_domain
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS))
        self._owns_client = client is None

    async def authorization_server_metadata(self, _: Request) -> Response:
        return JSONResponse(
            {
                "issuer": self._issuer,
                "authorization_endpoint": f"{self._issuer}/oauth/authorize",
                "token_endpoint": f"{self._issuer}/oauth/token",
                "scopes_supported": [*UPSTREAM_SCOPES, OFFLINE_ACCESS_SCOPE],
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "authorization_response_iss_parameter_supported": True,
                "token_endpoint_auth_methods_supported": [
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "code_challenge_methods_supported": ["S256"],
            }
        )

    async def authorize(self, request: Request) -> Response:
        downstream_redirect_uri = request.query_params.get("redirect_uri", "")
        downstream_state = request.query_params.get("state", "")
        requested_client_id = request.query_params.get("client_id", "")
        if requested_client_id not in self._client_ids:
            return _oauth_error("invalid_request", "The OAuth client ID is not allowed", 400)
        if downstream_redirect_uri not in self._allowed_redirect_uris:
            return _oauth_error("invalid_request", "The OAuth redirect URI is not allowed", 400)
        if not downstream_state or len(downstream_state) > MAX_STATE_LENGTH:
            return _oauth_error("invalid_request", "A valid OAuth state is required", 400)
        params = {
            name: value
            for name in AUTHORIZE_PASSTHROUGH_PARAMS
            if (value := request.query_params.get(name))
        }
        params["response_type"] = params.get("response_type") or "code"
        if params["response_type"] != "code":
            return _oauth_error("unsupported_response_type", "Only code flow is supported", 400)
        params["redirect_uri"] = self._upstream_callback_uri
        upstream_state = _encode_state(downstream_redirect_uri, downstream_state)
        if len(upstream_state) > MAX_UPSTREAM_STATE_LENGTH:
            return _oauth_error("invalid_request", "The OAuth state is too long", 400)
        params["state"] = upstream_state
        params["response_mode"] = "form_post"
        params["scope"] = " ".join(UPSTREAM_SCOPES)
        params["access_type"] = "offline"
        # Google re-issues a refresh token only when consent is forced. Authorization
        # runs once per connection and every later call rides the refresh token, so the
        # extra consent screen costs a single click.
        params["prompt"] = "consent"
        if self._hosted_domain:
            params["hd"] = self._hosted_domain
        return RedirectResponse(
            f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(params)}",
            status_code=302,
        )

    async def callback(self, request: Request) -> Response:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            return _oauth_error(
                "invalid_request",
                "The request body must be application/x-www-form-urlencoded",
                400,
            )
        try:
            form = await request.form()
        except Exception:
            return _oauth_error("invalid_request", "A form-encoded OAuth response is required", 400)
        if form.get("iss") != GOOGLE_ISSUER:
            return _oauth_error("invalid_request", "The upstream OAuth issuer is invalid", 400)
        encoded_state = str(form.get("state", ""))
        try:
            downstream_redirect_uri, downstream_state = _decode_state(encoded_state)
        except ValueError:
            return _oauth_error("invalid_request", "The OAuth state is invalid", 400)
        if downstream_redirect_uri not in self._allowed_redirect_uris:
            return _oauth_error("invalid_request", "The OAuth redirect URI is not allowed", 400)

        code_value = form.get("code")
        error_value = form.get("error")
        code = str(code_value) if code_value is not None else None
        error = str(error_value) if error_value is not None else None
        if bool(code) == bool(error):
            return _oauth_error("invalid_request", "The OAuth response is invalid", 400)
        if code and len(code) > MAX_AUTHORIZATION_RESPONSE_VALUE_LENGTH:
            return _oauth_error("invalid_request", "The OAuth code is too long", 400)
        if error and not OAUTH_ERROR_PATTERN.fullmatch(error):
            return _oauth_error("invalid_request", "The OAuth error is invalid", 400)
        response_params = {"state": downstream_state, "iss": self._issuer}
        if code:
            response_params["code"] = code
        else:
            response_params["error"] = error or "server_error"
        return RedirectResponse(
            _append_query(downstream_redirect_uri, response_params),
            status_code=302,
        )

    async def token(self, request: Request) -> Response:
        # RFC 6749 section 4.1.3 requires this content type, and refusing anything else
        # keeps multipart parsing out of an unauthenticated endpoint.
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            return _oauth_error(
                "invalid_request",
                "The request body must be application/x-www-form-urlencoded",
                400,
            )
        try:
            form = await request.form()
        except Exception:
            return _oauth_error(
                "invalid_request",
                "A form-encoded request body is required",
                400,
            )
        payload = {name: str(form[name]) for name in TOKEN_PASSTHROUGH_FIELDS if name in form}
        grant_type = payload.get("grant_type")
        if not grant_type:
            return _oauth_error("invalid_request", "grant_type is required", 400)
        form_client_id = payload.get("client_id")
        if form_client_id and form_client_id not in self._client_ids:
            return _oauth_error("invalid_client", "The OAuth client ID is not allowed", 401)
        if grant_type == "authorization_code":
            downstream_redirect_uri = payload.get("redirect_uri", "")
            if downstream_redirect_uri not in self._allowed_redirect_uris:
                return _oauth_error("invalid_request", "The OAuth redirect URI is not allowed", 400)
            payload["redirect_uri"] = self._upstream_callback_uri

        headers = {}
        # A client authenticating with client_secret_basic carries its credentials in
        # the Authorization header rather than the form body.
        authorization = request.headers.get("authorization")
        if authorization:
            try:
                basic_client_id = _basic_client_id(authorization)
            except ValueError:
                return _oauth_error(
                    "invalid_client", "The OAuth client authentication is invalid", 401
                )
            if basic_client_id not in self._client_ids:
                return _oauth_error("invalid_client", "The OAuth client ID is not allowed", 401)
            if form_client_id and form_client_id != basic_client_id:
                return _oauth_error(
                    "invalid_client", "The OAuth client credentials do not match", 401
                )
            headers["Authorization"] = authorization

        try:
            upstream = await self._client.post(
                GOOGLE_TOKEN_ENDPOINT,
                data=payload,
                headers=headers,
            )
        except httpx.RequestError:
            logger.warning("The Google token endpoint could not be reached")
            return _oauth_error(
                "temporarily_unavailable",
                "The upstream token endpoint could not be reached",
                502,
            )
        return self._relay_token_response(upstream)

    @staticmethod
    def _relay_token_response(upstream: httpx.Response) -> Response:
        try:
            body = upstream.json()
        except ValueError:
            logger.warning("The Google token endpoint returned a non-JSON response")
            return _oauth_error(
                "server_error",
                "The upstream token endpoint returned a malformed response",
                502,
            )
        if not isinstance(body, dict):
            logger.warning("The Google token endpoint returned an unexpected payload")
            return _oauth_error(
                "server_error",
                "The upstream token endpoint returned an unexpected payload",
                502,
            )
        # Report offline_access as granted on every successful exchange, refreshes
        # included. Google omits the refresh token from a refresh response, so keying
        # this on its presence would narrow the scope halfway through the connection
        # and a client that re-checks granted scope would drop back to a consent screen.
        if upstream.status_code == 200 and (body.get("scope") or body.get("refresh_token")):
            granted = str(body.get("scope") or "").split()
            if OFFLINE_ACCESS_SCOPE not in granted:
                granted.append(OFFLINE_ACCESS_SCOPE)
            body["scope"] = " ".join(granted)
        return JSONResponse(body, status_code=upstream.status_code)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

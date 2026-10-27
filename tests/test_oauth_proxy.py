from __future__ import annotations

import base64
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from opencivicquery.oauth_proxy import (
    GOOGLE_ISSUER,
    GOOGLE_TOKEN_ENDPOINT,
    GoogleOAuthProxy,
)

ISSUER = "https://opencivicquery.example.org"
CLIENT_ID = "client-123.apps.googleusercontent.com"
ADDITIONAL_CLIENT_ID = "client-456.apps.googleusercontent.com"
REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


UpstreamHandler = Callable[[httpx.Request], httpx.Response]


def build_proxy(
    handler: UpstreamHandler | None = None,
    *,
    hosted_domain: str | None = "example.org",
) -> GoogleOAuthProxy:
    client = None
    if handler is not None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GoogleOAuthProxy(
        issuer_url=ISSUER,
        client_ids=(CLIENT_ID, ADDITIONAL_CLIENT_ID),
        allowed_redirect_uris=(REDIRECT_URI,),
        hosted_domain=hosted_domain,
        client=client,
    )


def build_client(proxy: GoogleOAuthProxy) -> TestClient:
    app = Starlette(
        routes=[
            Route(
                "/.well-known/oauth-authorization-server",
                proxy.authorization_server_metadata,
                methods=["GET"],
            ),
            Route("/oauth/authorize", proxy.authorize, methods=["GET"]),
            Route("/oauth/callback", proxy.callback, methods=["POST"]),
            Route("/oauth/token", proxy.token, methods=["POST"]),
        ]
    )
    return TestClient(app)


def authorize_query(response: httpx.Response) -> dict[str, list[str]]:
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    return parse_qs(urlparse(location).query)


def test_metadata_advertises_offline_access_and_pkce() -> None:
    with build_client(build_proxy()) as client:
        payload = client.get("/.well-known/oauth-authorization-server").json()

    assert payload["issuer"] == ISSUER
    assert payload["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
    assert payload["token_endpoint"] == f"{ISSUER}/oauth/token"
    # Advertising offline_access is the entire point of the proxy: Claude requests a
    # refresh token only when the authorization server metadata lists this scope.
    assert "offline_access" in payload["scopes_supported"]
    assert payload["code_challenge_methods_supported"] == ["S256"]
    assert "refresh_token" in payload["grant_types_supported"]
    assert payload["authorization_response_iss_parameter_supported"] is True


def test_issuer_url_is_normalized() -> None:
    proxy = GoogleOAuthProxy(
        issuer_url=f"{ISSUER}/",
        client_ids=(CLIENT_ID,),
        allowed_redirect_uris=(REDIRECT_URI,),
        hosted_domain=None,
    )
    with build_client(proxy) as client:
        payload = client.get("/.well-known/oauth-authorization-server").json()

    assert payload["issuer"] == ISSUER
    assert payload["token_endpoint"] == f"{ISSUER}/oauth/token"


def test_authorize_forces_offline_access_and_consent() -> None:
    with build_client(build_proxy()) as client:
        response = client.get(
            "/oauth/authorize",
            params={
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "state": "state-abc",
                "code_challenge": "challenge-xyz",
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )

    assert response.status_code == 302
    query = authorize_query(response)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["hd"] == ["example.org"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"{ISSUER}/oauth/callback"]
    assert query["state"] != ["state-abc"]
    assert query["response_mode"] == ["form_post"]
    assert query["code_challenge"] == ["challenge-xyz"]
    assert query["code_challenge_method"] == ["S256"]


def test_authorize_accepts_an_additional_client_id() -> None:
    with build_client(build_proxy()) as client:
        response = client.get(
            "/oauth/authorize",
            params={
                "client_id": ADDITIONAL_CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "state": "state-abc",
            },
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert authorize_query(response)["client_id"] == [ADDITIONAL_CLIENT_ID]


def test_authorize_sends_only_scopes_google_recognises() -> None:
    with build_client(build_proxy()) as client:
        response = client.get(
            "/oauth/authorize",
            params={
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "state": "state-abc",
                "scope": "openid email offline_access opencivicquery:read",
            },
            follow_redirects=False,
        )

    # offline_access and any product scope would come back from Google as
    # invalid_scope, so neither reaches it.
    assert authorize_query(response)["scope"] == ["openid email"]


def test_authorize_drops_unknown_parameters() -> None:
    with build_client(build_proxy()) as client:
        response = client.get(
            "/oauth/authorize",
            params={
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "state": "state-abc",
                "access_type": "online",
                "hd": "attacker.example",
                "unexpected": "value",
            },
            follow_redirects=False,
        )

    query = authorize_query(response)
    assert "unexpected" not in query
    # A caller cannot downgrade the offline grant or widen the hosted domain.
    assert query["access_type"] == ["offline"]
    assert query["hd"] == ["example.org"]


def test_authorize_omits_hosted_domain_when_several_are_allowed() -> None:
    with build_client(build_proxy(hosted_domain=None)) as client:
        response = client.get(
            "/oauth/authorize",
            params={
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "state": "state-abc",
            },
            follow_redirects=False,
        )

    assert "hd" not in authorize_query(response)


@pytest.mark.parametrize("params", [{}, {"client_id": CLIENT_ID}, {"redirect_uri": REDIRECT_URI}])
def test_authorize_rejects_an_incomplete_request(params: dict[str, str]) -> None:
    with build_client(build_proxy()) as client:
        response = client.get("/oauth/authorize", params=params, follow_redirects=False)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_authorize_rejects_an_unregistered_redirect_uri() -> None:
    with build_client(build_proxy()) as client:
        response = client.get(
            "/oauth/authorize",
            params={
                "client_id": CLIENT_ID,
                "redirect_uri": "https://attacker.example/callback",
                "state": "state-abc",
            },
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_authorize_rejects_an_unexpected_client_id() -> None:
    with build_client(build_proxy()) as client:
        response = client.get(
            "/oauth/authorize",
            params={
                "client_id": "other-client.apps.googleusercontent.com",
                "redirect_uri": REDIRECT_URI,
                "state": "state-abc",
            },
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_callback_returns_the_service_issuer_to_the_mcp_client() -> None:
    with build_client(build_proxy()) as client:
        authorize = client.get(
            "/oauth/authorize",
            params={
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "state": "state-abc",
            },
            follow_redirects=False,
        )
        upstream_state = authorize_query(authorize)["state"][0]
        callback = client.post(
            "/oauth/callback",
            data={"code": "google-code", "state": upstream_state, "iss": GOOGLE_ISSUER},
            follow_redirects=False,
        )

    assert callback.status_code == 302
    parsed = urlparse(callback.headers["location"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == REDIRECT_URI
    query = parse_qs(parsed.query)
    assert query == {"code": ["google-code"], "state": ["state-abc"], "iss": [ISSUER]}


def test_callback_rejects_an_unexpected_upstream_issuer() -> None:
    with build_client(build_proxy()) as client:
        authorize = client.get(
            "/oauth/authorize",
            params={
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "state": "state-abc",
            },
            follow_redirects=False,
        )
        upstream_state = authorize_query(authorize)["state"][0]
        callback = client.post(
            "/oauth/callback",
            data={"code": "google-code", "state": upstream_state, "iss": "https://evil.example"},
            follow_redirects=False,
        )

    assert callback.status_code == 400
    assert callback.json()["error"] == "invalid_request"


def test_callback_rejects_a_malformed_state() -> None:
    with build_client(build_proxy()) as client:
        callback = client.post(
            "/oauth/callback",
            data={"code": "google-code", "state": "not-base64!", "iss": GOOGLE_ISSUER},
            follow_redirects=False,
        )

    assert callback.status_code == 400
    assert callback.json()["error"] == "invalid_request"


def test_callback_rejects_a_non_form_body() -> None:
    with build_client(build_proxy()) as client:
        callback = client.post(
            "/oauth/callback",
            json={"code": "google-code", "state": "state", "iss": GOOGLE_ISSUER},
            follow_redirects=False,
        )

    assert callback.status_code == 400
    assert callback.json()["error"] == "invalid_request"


def test_proxy_rejects_an_insecure_redirect_uri() -> None:
    with pytest.raises(ValueError, match="Invalid OAuth redirect URI"):
        GoogleOAuthProxy(
            issuer_url=ISSUER,
            client_ids=(CLIENT_ID,),
            allowed_redirect_uris=("http://attacker.example/callback",),
        )


def test_token_relays_the_refresh_token_and_reports_offline_access() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "google-access-token",
                "refresh_token": "google-refresh-token",
                "expires_in": 3599,
                "scope": "openid https://www.googleapis.com/auth/userinfo.email",
                "token_type": "Bearer",
            },
        )

    with build_client(build_proxy(handler)) as client:
        response = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": "auth-code",
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "client_secret": "client-secret",
                "code_verifier": "verifier-xyz",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["refresh_token"] == "google-refresh-token"
    # The client asked for offline_access, so the granted scope has to name it or the
    # requested and granted scopes disagree.
    assert "offline_access" in body["scope"].split()

    assert len(seen) == 1
    assert str(seen[0].url) == GOOGLE_TOKEN_ENDPOINT
    forwarded = parse_qs(seen[0].content.decode())
    assert forwarded["code_verifier"] == ["verifier-xyz"]
    assert forwarded["client_secret"] == ["client-secret"]
    assert forwarded["redirect_uri"] == [f"{ISSUER}/oauth/callback"]


def test_token_accepts_an_additional_client_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "google-access-token"})

    with build_client(build_proxy(handler)) as client:
        response = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": "stored-refresh",
                "client_id": ADDITIONAL_CLIENT_ID,
                "client_secret": "client-secret",
            },
        )

    assert response.status_code == 200
    forwarded = parse_qs(seen[0].content.decode())
    assert forwarded["client_id"] == [ADDITIONAL_CLIENT_ID]


def test_token_reports_offline_access_on_a_refresh_too() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        # Google returns no refresh token on a refresh exchange.
        return httpx.Response(
            200,
            json={"access_token": "google-access-token", "scope": "openid email"},
        )

    with build_client(build_proxy(handler)) as client:
        response = client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": "stored-refresh"},
        )

    # Narrowing the scope halfway through a connection would read as a downgrade and
    # could send the reporter back to a consent screen.
    assert response.json()["scope"].split() == ["openid", "email", "offline_access"]


def test_token_leaves_a_scopeless_response_alone() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "google-access-token"})

    with build_client(build_proxy(handler)) as client:
        response = client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": "stored-refresh"},
        )

    # Inventing a scope field where the upstream sent none would claim less than was
    # actually granted.
    assert "scope" not in response.json()


def test_token_forwards_basic_client_authentication() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "google-access-token"})

    authorization = base64.b64encode(f"{CLIENT_ID}:secret".encode()).decode()
    with build_client(build_proxy(handler)) as client:
        client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": "auth-code",
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Authorization": f"Basic {authorization}"},
        )

    assert seen[0].headers["authorization"] == f"Basic {authorization}"


def test_token_rejects_an_unexpected_basic_client_id() -> None:
    authorization = base64.b64encode(b"other-client:secret").decode()
    with build_client(build_proxy()) as client:
        response = client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": "stored-refresh"},
            headers={"Authorization": f"Basic {authorization}"},
        )

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


def test_token_rejects_mismatched_form_and_basic_client_ids() -> None:
    authorization = base64.b64encode(f"{CLIENT_ID}:secret".encode()).decode()
    with build_client(build_proxy()) as client:
        response = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": "stored-refresh",
                "client_id": ADDITIONAL_CLIENT_ID,
            },
            headers={"Authorization": f"Basic {authorization}"},
        )

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


def test_token_drops_fields_google_did_not_ask_for() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "google-access-token"})

    with build_client(build_proxy(handler)) as client:
        client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": "auth-code",
                "redirect_uri": REDIRECT_URI,
                "assertion": "injected",
            },
        )

    assert "assertion" not in parse_qs(seen[0].content.decode())


def test_token_relays_an_upstream_rejection() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "Bad Request"},
        )

    with build_client(build_proxy(handler)) as client:
        response = client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": "expired"},
        )

    # Claude retries a refresh only on an RFC 6749 error code, so the upstream status
    # and code have to survive the hop.
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_token_reports_an_unreachable_upstream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream down", request=request)

    with build_client(build_proxy(handler)) as client:
        response = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": "auth-code",
                "redirect_uri": REDIRECT_URI,
            },
        )

    assert response.status_code == 502
    assert response.json()["error"] == "temporarily_unavailable"


def test_token_reports_a_malformed_upstream_response() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with build_client(build_proxy(handler)) as client:
        response = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": "auth-code",
                "redirect_uri": REDIRECT_URI,
            },
        )

    assert response.status_code == 502
    assert response.json()["error"] == "server_error"


def test_token_rejects_a_request_without_a_grant_type() -> None:
    with build_client(build_proxy()) as client:
        response = client.post("/oauth/token", data={"code": "auth-code"})

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_token_rejects_a_body_that_is_not_form_encoded() -> None:
    with build_client(build_proxy()) as client:
        response = client.post(
            "/oauth/token",
            json={"grant_type": "authorization_code", "code": "auth-code"},
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_token_accepts_a_form_content_type_carrying_a_charset() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "google-access-token"})

    with build_client(build_proxy(handler)) as client:
        response = client.post(
            "/oauth/token",
            content=(
                "grant_type=authorization_code&code=auth-code&"
                "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_close_leaves_an_injected_client_open() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    proxy = GoogleOAuthProxy(
        issuer_url=ISSUER,
        client_ids=(CLIENT_ID,),
        allowed_redirect_uris=(REDIRECT_URI,),
        client=client,
    )

    await proxy.close()

    assert not client.is_closed
    await client.aclose()

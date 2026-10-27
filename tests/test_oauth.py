from __future__ import annotations

import httpx
import pytest

from opencivicquery.oauth import GoogleWorkspaceTokenVerifier


def verifier_with(
    payload: dict,
    status_code: int = 200,
    allowed_email_domains: list[str] | None = None,
    allowed_users: list[str] | None = None,
) -> GoogleWorkspaceTokenVerifier:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GoogleWorkspaceTokenVerifier(
        client_ids=(
            "claude-client.apps.googleusercontent.com",
            "chatgpt-client.apps.googleusercontent.com",
        ),
        required_scopes=["openid", "email"],
        allowed_email_domains=(
            allowed_email_domains if allowed_email_domains is not None else ["example.org"]
        ),
        allowed_users=allowed_users,
        client=client,
    )


@pytest.mark.asyncio
async def test_google_token_verifier_returns_verified_email() -> None:
    verifier = verifier_with(
        {
            "audience": "claude-client.apps.googleusercontent.com",
            "email": "Reporter@Example.org",
            "verified_email": "true",
            "user_id": "user-123",
            "scope": "openid https://www.googleapis.com/auth/userinfo.email profile",
            "expires_in": "300",
        }
    )

    access_token = await verifier.verify_token("opaque-token")

    assert access_token is not None
    assert access_token.claims["email"] == "reporter@example.org"


@pytest.mark.asyncio
async def test_google_token_verifier_accepts_each_configured_audience() -> None:
    verifier = verifier_with(
        {
            "audience": "chatgpt-client.apps.googleusercontent.com",
            "email": "reporter@example.org",
            "verified_email": "true",
            "user_id": "user-123",
            "scope": "openid email",
            "expires_in": "300",
        }
    )

    access_token = await verifier.verify_token("opaque-token")

    assert access_token is not None
    assert access_token.client_id == "chatgpt-client.apps.googleusercontent.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"audience": "different-client"},
        {"verified_email": "false"},
        {"scope": "openid"},
        {"expires_in": "0"},
        # An account outside the newsroom, however it reached a consent screen.
        {"email": "outsider@example.com"},
        {"email": "reporter@sub.example.org"},
        {"email": "reporter@example.org.attacker.example"},
        # A second at-sign would otherwise let a crafted local part carry the
        # allowed domain while the real domain is somewhere else.
        {"email": "reporter@example.org@attacker.example"},
        {"email": "@example.org"},
    ],
)
async def test_google_token_verifier_fails_closed(change: dict) -> None:
    payload = {
        "audience": "claude-client.apps.googleusercontent.com",
        "email": "reporter@example.org",
        "verified_email": "true",
        "user_id": "user-123",
        "scope": "openid email",
        "expires_in": "300",
    }
    verifier = verifier_with(payload | change)

    assert await verifier.verify_token("opaque-token") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("verified_field", ["email_verified", "verified_email"])
async def test_google_token_verifier_accepts_either_verified_field_name(
    verified_field: str,
) -> None:
    # The live endpoint sends email_verified and sub; the retired one sent
    # verified_email and user_id. Reading only the old pair refused every real token.
    verifier = verifier_with(
        {
            "aud": "claude-client.apps.googleusercontent.com",
            "email": "reporter@example.org",
            verified_field: "true",
            "sub": "user-123",
            "scope": "https://www.googleapis.com/auth/userinfo.email",
            "expires_in": "3599",
        }
    )

    access_token = await verifier.verify_token("opaque-token")

    assert access_token is not None
    assert access_token.subject == "user-123"
    # openid is treated as granted because the subject claim it guarantees is present.
    assert {"openid", "email"}.issubset(access_token.scopes)


@pytest.mark.asyncio
async def test_google_token_verifier_accepts_any_configured_domain() -> None:
    verifier = verifier_with(
        {
            "audience": "claude-client.apps.googleusercontent.com",
            "email": "editor@example.net",
            "verified_email": "true",
            "user_id": "user-456",
            "scope": "openid email",
            "expires_in": "300",
        },
        allowed_email_domains=["example.org", "example.net"],
    )

    access_token = await verifier.verify_token("opaque-token")

    assert access_token is not None
    assert access_token.claims["email"] == "editor@example.net"


@pytest.mark.asyncio
async def test_google_token_verifier_accepts_only_the_named_external_user() -> None:
    payload = {
        "audience": "claude-client.apps.googleusercontent.com",
        "email": "Collaborator@Example.com",
        "verified_email": "true",
        "user_id": "user-789",
        "scope": "openid email",
        "expires_in": "300",
    }
    verifier = verifier_with(
        payload,
        allowed_email_domains=["example.org"],
        allowed_users=["collaborator@example.com"],
    )

    access_token = await verifier.verify_token("opaque-token")

    assert access_token is not None
    assert access_token.claims["email"] == "collaborator@example.com"

    payload["email"] = "someone-else@example.com"
    assert await verifier.verify_token("another-token") is None


def test_google_token_verifier_requires_an_allowed_identity() -> None:
    with pytest.raises(ValueError):
        GoogleWorkspaceTokenVerifier(
            client_ids=("claude-client.apps.googleusercontent.com",),
            required_scopes=["openid", "email"],
            allowed_email_domains=[],
            allowed_users=[],
        )


def test_google_token_verifier_requires_a_client_id() -> None:
    with pytest.raises(ValueError):
        GoogleWorkspaceTokenVerifier(
            client_ids=(),
            required_scopes=["openid", "email"],
            allowed_email_domains=["example.org"],
        )

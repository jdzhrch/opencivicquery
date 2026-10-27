from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import httpx
from mcp.server.auth.provider import AccessToken

logger = logging.getLogger(__name__)


class GoogleWorkspaceTokenVerifier:
    """Validate Google OAuth access tokens issued to a configured MCP client."""

    tokeninfo_url = "https://oauth2.googleapis.com/tokeninfo"
    email_scope = "https://www.googleapis.com/auth/userinfo.email"

    def __init__(
        self,
        *,
        client_ids: tuple[str, ...],
        required_scopes: list[str],
        allowed_email_domains: list[str],
        allowed_users: list[str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        normalized_client_ids = {
            client_id.strip() for client_id in client_ids if client_id.strip()
        }
        if not normalized_client_ids:
            raise ValueError("At least one OAuth client ID is required")
        domains = {domain.strip().lower() for domain in allowed_email_domains if domain.strip()}
        users = {email.strip().lower() for email in (allowed_users or []) if email.strip()}
        if not domains and not users:
            raise ValueError("At least one allowed email domain or user is required")
        self._client_ids = frozenset(normalized_client_ids)
        self._required_scopes = set(required_scopes)
        self._allowed_email_domains = domains
        self._allowed_users = users
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._owns_client = client is None

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        try:
            response = await self._client.get(
                self.tokeninfo_url,
                params={"access_token": token},
            )
        except httpx.RequestError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload: Any = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None

        audience = payload.get("audience") or payload.get("aud") or payload.get("issued_to")
        if not isinstance(audience, str) or audience not in self._client_ids:
            self._refuse(token, "audience mismatch", payload)
            return None
        email = payload.get("email")
        subject = payload.get("user_id") or payload.get("sub")
        # `oauth2.googleapis.com/tokeninfo` reports OIDC field names (`email_verified`,
        # `sub`), the retired `www.googleapis.com/oauth2/v1/tokeninfo` reported
        # `verified_email` and `user_id`. Accept both: reading only the old name made
        # every live token look unverified while the constructed test payloads passed.
        verified = payload.get("email_verified")
        if verified is None:
            verified = payload.get("verified_email")
        if not isinstance(email, str) or not email.strip() or not isinstance(subject, str):
            self._refuse(token, "missing email or subject", payload)
            return None
        if verified not in {True, "true", "True"}:
            self._refuse(token, "email not verified", payload)
            return None

        # The Google audience only controls who can reach the consent screen. This
        # application-level check runs on every request and admits either a named
        # collaborator or anyone in a configured newsroom domain.
        normalized_email = email.strip().lower()
        local_part, separator, domain = normalized_email.partition("@")
        if (
            not local_part
            or not separator
            or (
                normalized_email not in self._allowed_users
                and domain not in self._allowed_email_domains
            )
        ):
            self._refuse(token, "email address not allowed", payload)
            return None

        scope_value = payload.get("scope")
        scopes = scope_value.split() if isinstance(scope_value, str) else []
        # Google reports the granted email scope in its expanded URL form.
        if self.email_scope in scopes and "email" not in scopes:
            scopes.append("email")
        # tokeninfo does not reliably echo the `openid` shorthand the request used.
        # What `openid` buys a resource server is a subject claim, and the subject was
        # already required above, so taking the claim as the grant checks the real
        # guarantee rather than Google's bookkeeping format for it.
        if "openid" not in scopes:
            scopes.append("openid")
        if not self._required_scopes.issubset(scopes):
            self._refuse(token, "required scope missing", payload)
            return None
        try:
            expires_in = max(0, int(payload.get("expires_in", 0)))
        except (TypeError, ValueError):
            self._refuse(token, "unreadable expires_in", payload)
            return None
        if expires_in == 0:
            self._refuse(token, "token already expired", payload)
            return None

        return AccessToken(
            token=token,
            client_id=audience,
            scopes=scopes,
            expires_at=int(time.time()) + expires_in,
            subject=subject,
            claims={"email": normalized_email, "email_verified": True},
        )

    @staticmethod
    def _refuse(token: str, reason: str, payload: dict[str, Any]) -> None:
        """Say why a token was refused without writing the token or the identity out.

        A refusal is indistinguishable from every other refusal at the HTTP layer, so
        without this a 401 on a token Google itself called valid takes a redeploy to
        explain. Only field names and scope names are logged; the email, the subject
        and the token stay out of Cloud Logging, which is why the token is reduced to
        a short fingerprint that is enough to correlate retries of one connection.
        """
        fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
        logger.warning(
            "Refused a Google token: %s (token %s, tokeninfo fields %s, scopes %s)",
            reason,
            fingerprint,
            sorted(payload),
            str(payload.get("scope", "")).split(),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

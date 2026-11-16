"""Adapt Authlib request construction to the application's OIDC transport."""

from typing import TYPE_CHECKING, cast
from urllib.parse import quote_plus

from authlib.integrations.httpx_client import OAuth2ClientAuth  # type: ignore[import-untyped]
from authlib.oauth2 import OAuth2Client  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx2

    from auth.config import OidcSettings


class OidcRequestAdapter:
    """Construct provider requests without retaining any user's token state.

    Authlib owns authorization URL composition, S256 PKCE, and Basic header
    construction. The application retains discovery validation, strict response
    parsing, ID-token validation, and owner-scoped refresh/session lifecycle.
    """

    def __init__(self, settings: OidcSettings, client: httpx2.AsyncClient) -> None:
        """Reuse the provider's transport without taking ownership of its lifetime."""
        self._client = client
        self._timeout_seconds = settings.http_timeout_seconds
        # Use only Authlib's request builder; no shared provider-token cache or
        # automatic refresh may bypass the application's per-session locks.
        self._authorization_client = OAuth2Client(
            session=None,
            client_id=settings.client_id,
            redirect_uri=settings.redirect_uri,
            scope=settings.scopes,
            code_challenge_method="S256",
        )
        # Authlib 1.9.1 joins its Basic credentials verbatim. RFC 6749 section
        # 2.3.1 requires separate UTF-8 form encoding before that join. Keep the
        # original client ID above for authorization and ID-token audiences.
        self._client_auth = OAuth2ClientAuth(
            quote_plus(settings.client_id, safe=""),
            quote_plus(settings.client_secret, safe=""),
            auth_method="client_secret_basic",
        )

    def authorization_url(self, endpoint: str, state: str, nonce: str, verifier: str) -> str:
        """Preserve endpoint query parameters and the supplied login correlation."""
        url, _ = self._authorization_client.create_authorization_url(
            endpoint,
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            response_type="code",
        )
        return cast("str", url)

    async def post_form(self, endpoint: str, data: Mapping[str, str]) -> httpx2.Response:
        """Authenticate exchange, refresh, and revocation through one bounded path."""
        return await self._client.post(
            endpoint,
            data=data,
            auth=self._client_auth,
            headers={"Accept": "application/json"},
            timeout=self._timeout_seconds,
            follow_redirects=False,
        )

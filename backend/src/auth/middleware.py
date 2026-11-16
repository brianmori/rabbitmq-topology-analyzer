"""Provider-neutral authentication checks that run before request bodies are read."""

import hmac
import json
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from auth.activity import OwnerSessionClosingError
from auth.cookies import get_cookie_value
from core.security import AppRole, AuthMode, Principal
from core.session import AmbiguousBrowserSessionHeaderError

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from auth.activity import OwnerActivityRegistry
    from core.session import OwnerContext

_HTTP_BAD_REQUEST = 400
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_SERVICE_UNAVAILABLE = 503
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_UPLOAD_PATH = "/api/definitions/upload"
_AUTH_SESSION_PATH = "/api/auth/session"
_ANONYMOUS_GET_PATHS = frozenset(
    {
        "/api/health",
        "/api/health/live",
        "/api/health/ready",
    },
)
_OIDC_PUBLIC_GET_PATHS = frozenset(
    {
        "/api/auth/oidc/login",
        "/api/auth/oidc/callback",
    },
)


@dataclass(frozen=True, slots=True)
class AuthenticatedRequest:
    """Verified request identity and provider-owned application-session state."""

    principal: Principal
    owner_context: OwnerContext | None = None
    csrf_token: str | None = None
    session_lookup_token: str | None = None


class RequestAuthenticator(Protocol):
    """Authenticate one provider credential without reading an ASGI body."""

    def __call__(
        self,
        credential: str,
        scope: Scope,
    ) -> Awaitable[AuthenticatedRequest | None]:
        """Return verified request state, or ``None`` for an invalid credential."""
        ...


class AuthenticationRejected(Exception):
    """Signal a safe credential failure from an injected provider authenticator."""


class AuthenticationCapacityUnavailable(Exception):
    """Signal bounded verifier saturation without treating credentials as invalid."""


class PreBodyAuthMiddleware:
    """Establish identity and reject auth, CSRF, or upload-role failures early.

    The middleware deliberately inspects only the ASGI scope and its headers.
    No rejection path calls ``receive``, so Starlette cannot parse or spool a
    multipart body before authentication and the upload analyst preflight.
    Provider-specific token or password verification is injected through
    ``authenticator`` and remains outside this module.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        mode: AuthMode,
        authenticator: RequestAuthenticator | None = None,
        public_origin: str | None = None,
        oidc_cookie_name: str = "rabbitmq_topology_session",
        oidc_cookie_secure: bool = True,
        basic_realm: str = "RabbitMQ Topology Analyzer",
        activity_registry: OwnerActivityRegistry | None = None,
    ) -> None:
        """Configure the one selected authentication transport."""
        if mode is not AuthMode.NONE and authenticator is None:
            raise ValueError("authenticated mode requires an authenticator")
        if mode is AuthMode.OIDC and not public_origin:
            raise ValueError("OIDC mode requires the configured public origin")
        self._app = app
        self._mode = mode
        self._authenticator = authenticator
        self._public_origin = public_origin
        self._oidc_cookie_name = oidc_cookie_name
        self._oidc_cookie_secure = oidc_cookie_secure
        self._basic_challenge = f'Basic realm="{basic_realm}", charset="UTF-8"'
        self._activity_registry = activity_registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate one HTTP scope before delegating to FastAPI."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if self._is_passthrough(method, path):
            await self._call_downstream(scope, receive, send, path)
            return

        credential = self._credential(scope)
        if method == "GET" and path == _AUTH_SESSION_PATH and credential is None:
            await self._call_downstream(scope, receive, send, path)
            return
        if credential is None:
            await self._reject(send, status_code=_HTTP_UNAUTHORIZED)
            return

        authentication = await self._authenticate_or_reject(credential, scope, send)
        if authentication is None:
            return
        if authentication.principal.auth_mode is not self._mode:
            raise RuntimeError("authenticator returned a principal for another mode")

        self._establish_state(scope, authentication)
        if self._early_forbidden(scope, method, path, authentication):
            await self._reject(send, status_code=_HTTP_FORBIDDEN)
            return

        await self._call_authenticated_downstream(
            scope,
            receive,
            send,
            path,
            authentication,
        )

    def _is_passthrough(self, method: str, path: str) -> bool:
        """Return whether this scope is outside credential enforcement."""
        is_api_path = path == "/api" or path.startswith("/api/")
        return (
            not is_api_path
            or self._mode is AuthMode.NONE
            or (method == "GET" and path in _ANONYMOUS_GET_PATHS)
            or (self._mode is AuthMode.OIDC and method == "GET" and path in _OIDC_PUBLIC_GET_PATHS)
        )

    def _early_forbidden(
        self,
        scope: Scope,
        method: str,
        path: str,
        authentication: AuthenticatedRequest,
    ) -> bool:
        """Check OIDC CSRF and the multipart analyst preflight."""
        csrf_failed = (
            self._mode is AuthMode.OIDC
            and method in _UNSAFE_METHODS
            and not self._valid_oidc_csrf(scope, authentication)
        )
        viewer_upload = (
            method == "POST"
            and path == _UPLOAD_PATH
            and not authentication.principal.permits(AppRole.ANALYST)
        )
        return csrf_failed or viewer_upload

    async def _authenticate_or_reject(
        self,
        credential: str,
        scope: Scope,
        send: Send,
    ) -> AuthenticatedRequest | None:
        """Render credential failures or capacity backpressure before body parsing."""
        authenticator = self._authenticator
        if authenticator is None:
            raise RuntimeError("authenticated mode has no authenticator")
        try:
            authentication = await authenticator(credential, scope)
        except AuthenticationRejected:
            authentication = None
        except AuthenticationCapacityUnavailable:
            await self._reject(send, status_code=_HTTP_SERVICE_UNAVAILABLE)
            return None
        except AmbiguousBrowserSessionHeaderError:
            await self._reject(send, status_code=_HTTP_BAD_REQUEST)
            return None
        if authentication is None:
            await self._reject(send, status_code=_HTTP_UNAUTHORIZED)
        return authentication

    def _credential(self, scope: Scope) -> str | None:
        """Extract only the selected mode's opaque credential transport."""
        if self._mode is AuthMode.OIDC:
            return get_cookie_value(scope, self._oidc_cookie_name)
        if self._mode is AuthMode.BASIC:
            return _single_header(scope, b"authorization")
        return None

    def _valid_oidc_csrf(
        self,
        scope: Scope,
        authentication: AuthenticatedRequest,
    ) -> bool:
        """Require the exact configured origin and session-bound CSRF value."""
        origin = _single_header(scope, b"origin")
        csrf = _single_header(scope, b"x-csrf-token")
        expected_csrf = authentication.csrf_token
        return (
            origin is not None
            and origin.isascii()
            and origin == self._public_origin
            and csrf is not None
            and csrf.isascii()
            and expected_csrf is not None
            and expected_csrf.isascii()
            and hmac.compare_digest(csrf, expected_csrf)
        )

    @staticmethod
    def _establish_state(scope: Scope, authentication: AuthenticatedRequest) -> None:
        """Expose verified state to the existing FastAPI dependencies."""
        state = scope.setdefault("state", {})
        if not isinstance(state, MutableMapping):
            raise RuntimeError("ASGI scope state must be a mutable mapping")
        state["authentication"] = authentication
        state["principal"] = authentication.principal
        state["owner_context"] = authentication.owner_context

    async def _call_downstream(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        path: str,
    ) -> None:
        """Add no-store to every response from the authentication namespace."""
        if not path.startswith("/api/auth/"):
            await self._app(scope, receive, send)
            return

        async def no_store_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                headers = list(message.get("headers", ()))
                headers = [
                    (name, value) for name, value in headers if name.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self._app(scope, receive, no_store_send)

    async def _call_authenticated_downstream(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        path: str,
        authentication: AuthenticatedRequest,
    ) -> None:
        """Track owner work so logout can close upload-publication races."""
        owner_context = authentication.owner_context
        activity_registry = self._activity_registry
        if owner_context is None or activity_registry is None or path.startswith("/api/auth/"):
            await self._call_downstream(scope, receive, send, path)
            return

        try:
            async with activity_registry.request(owner_context):
                await self._call_downstream(scope, receive, send, path)
        except OwnerSessionClosingError:
            await self._reject(send, status_code=_HTTP_UNAUTHORIZED)

    async def _reject(self, send: Send, *, status_code: int) -> None:
        """Send one safe, body-independent authentication error envelope."""
        if status_code == _HTTP_BAD_REQUEST:
            code = "invalid_session_token"
            message = "The browser-session identifier must be a canonical UUIDv4."
        elif status_code == _HTTP_UNAUTHORIZED:
            code = "unauthorized"
            message = "Authentication is required."
        elif status_code == _HTTP_SERVICE_UNAVAILABLE:
            code = "authentication_capacity_unavailable"
            message = "Authentication capacity is temporarily unavailable."
        else:
            code = "forbidden"
            message = "The current principal cannot perform this action."
        body = json.dumps(
            {"error": {"code": code, "message": message, "issues": []}},
            separators=(",", ":"),
        ).encode()
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        if status_code == _HTTP_UNAUTHORIZED and self._mode is AuthMode.BASIC:
            headers.append((b"www-authenticate", self._basic_challenge.encode("ascii")))
        if status_code == _HTTP_SERVICE_UNAVAILABLE:
            headers.append((b"retry-after", b"1"))
        if status_code == _HTTP_UNAUTHORIZED and self._mode is AuthMode.OIDC:
            headers.append((b"set-cookie", self._expired_oidc_cookie().encode("ascii")))
        await send({"type": "http.response.start", "status": status_code, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    def _expired_oidc_cookie(self) -> str:
        """Build the host-only deletion cookie for failed OIDC sessions."""
        attributes = [
            f"{self._oidc_cookie_name}=",
            "Path=/",
            "Max-Age=0",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if self._oidc_cookie_secure:
            attributes.append("Secure")
        return "; ".join(attributes)


def _single_header(scope: Scope, expected_name: bytes) -> str | None:
    """Return one header value while rejecting ambiguous duplicates."""
    headers = cast("list[tuple[bytes, bytes]]", scope.get("headers", ()))
    values = [value.decode("latin-1") for name, value in headers if name.lower() == expected_name]
    if len(values) != 1:
        return None
    return values[0]

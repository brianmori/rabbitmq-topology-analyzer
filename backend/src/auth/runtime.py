"""Application-lifespan composition for the selected authentication mode."""

from dataclasses import dataclass, replace
from datetime import timedelta
from threading import RLock
from typing import TYPE_CHECKING

from starlette.datastructures import Headers

from auth.activity import owner_activity_registry
from auth.basic import BasicAuthenticator
from auth.config import AuthSettings, load_auth_settings
from auth.middleware import AuthenticatedRequest, PreBodyAuthMiddleware
from auth.oidc import OidcProvider
from auth.sessions import AuthSessionRepository
from core.security import AuthMode, configure_authentication_dependencies
from core.session import OwnerContext, get_browser_session_token, parse_browser_session_id

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(slots=True, repr=False)
class AuthRuntime:
    """One process-local provider, credential verifier, and session store."""

    settings: AuthSettings
    sessions: AuthSessionRepository
    oidc_provider: OidcProvider | None = None
    basic_authenticator: BasicAuthenticator | None = None

    async def authenticate(
        self,
        credential: str,
        scope: Scope,
    ) -> AuthenticatedRequest | None:
        """Authenticate the selected transport and bind stable ownership."""
        if self.settings.mode is AuthMode.OIDC:
            provider = self.oidc_provider
            if provider is None:
                raise RuntimeError("OIDC runtime has no provider")
            return await provider.authenticate_session(self.sessions, credential)
        if self.settings.mode is AuthMode.BASIC:
            authenticator = self.basic_authenticator
            if authenticator is None:
                raise RuntimeError("Basic runtime has no verifier")
            authentication = await authenticator(credential, scope)
            if authentication is None:
                return None
            session_token = get_browser_session_token(
                Headers(scope=scope).getlist("x-session-token"),
            )
            if session_token is None:
                return authentication
            try:
                session_id = parse_browser_session_id(session_token)
            except ValueError:
                # The provider credential is valid. The owner dependency will
                # render the existing 400 contract if this route needs the
                # malformed browser-session component.
                return authentication
            return replace(
                authentication,
                owner_context=OwnerContext(
                    principal_id=authentication.principal.principal_id,
                    session_id=session_id,
                ),
            )
        raise RuntimeError("disabled authentication mode has no credential verifier")


class RuntimeAuthMiddleware:
    """Resolve lifespan state and apply pre-body authentication to each request."""

    def __init__(self, app: ASGIApp) -> None:
        """Retain the downstream FastAPI/CORS stack."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Delegate through middleware configured from the current lifespan."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        runtime = get_auth_runtime()
        settings = runtime.settings
        oidc = settings.oidc
        basic = settings.basic
        delegate = PreBodyAuthMiddleware(
            self._app,
            mode=settings.mode,
            authenticator=None if settings.mode is AuthMode.NONE else runtime.authenticate,
            public_origin=settings.public_origin,
            oidc_cookie_name=(
                oidc.cookie_name if oidc is not None else "rabbitmq_topology_session"
            ),
            oidc_cookie_secure=oidc.cookie_secure if oidc is not None else True,
            basic_realm=(basic.realm if basic is not None else "RabbitMQ Topology Analyzer"),
            activity_registry=owner_activity_registry,
        )
        await delegate(scope, receive, send)


@dataclass(slots=True)
class _RuntimeState:
    """Mutable slot protected by the process-local runtime lock."""

    runtime: AuthRuntime | None = None


_runtime_lock = RLock()
_runtime_state = _RuntimeState()


async def configure_auth_runtime(
    settings: AuthSettings | None = None,
    *,
    sessions: AuthSessionRepository | None = None,
    oidc_provider: OidcProvider | None = None,
) -> AuthRuntime:
    """Fail fast and establish the sole process-local authentication runtime."""
    await shutdown_auth_runtime()
    effective_settings = settings or load_auth_settings()
    oidc = effective_settings.oidc
    repository = sessions or AuthSessionRepository(
        login_limit=oidc.login_limit if oidc is not None else 256,
        login_ttl=timedelta(seconds=oidc.login_ttl_seconds if oidc is not None else 600),
        session_limit=oidc.session_limit if oidc is not None else 1_024,
        session_lifetime=timedelta(
            seconds=oidc.session_lifetime_seconds if oidc is not None else 28_800
        ),
    )
    provider: OidcProvider | None = None
    basic_authenticator: BasicAuthenticator | None = None
    try:
        if effective_settings.mode is AuthMode.OIDC:
            if oidc is None:
                raise RuntimeError("OIDC settings are missing")
            provider = oidc_provider or OidcProvider(oidc)
            await provider.start()
        elif effective_settings.mode is AuthMode.BASIC:
            basic = effective_settings.basic
            if basic is None:
                raise RuntimeError("Basic settings are missing")
            basic_authenticator = BasicAuthenticator(basic)
    except Exception:
        if provider is not None:
            await provider.close()
        configure_authentication_dependencies(AuthMode.NONE)
        raise

    runtime = AuthRuntime(
        settings=effective_settings,
        sessions=repository,
        oidc_provider=provider,
        basic_authenticator=basic_authenticator,
    )
    with _runtime_lock:
        _runtime_state.runtime = runtime
    basic_realm = (
        effective_settings.basic.realm
        if effective_settings.basic is not None
        else "RabbitMQ Topology Analyzer"
    )
    configure_authentication_dependencies(
        effective_settings.mode,
        basic_realm=basic_realm,
    )
    return runtime


def get_auth_runtime() -> AuthRuntime:
    """Return the lifespan-configured authentication runtime."""
    with _runtime_lock:
        if _runtime_state.runtime is None:
            raise RuntimeError("authentication runtime is not configured")
        return _runtime_state.runtime


async def shutdown_auth_runtime() -> None:
    """Drop mounted verifier secrets and close provider network resources."""
    with _runtime_lock:
        runtime = _runtime_state.runtime
        _runtime_state.runtime = None
    configure_authentication_dependencies(AuthMode.NONE)
    if runtime is not None and runtime.oidc_provider is not None:
        await runtime.oidc_provider.close()
    if runtime is not None and runtime.basic_authenticator is not None:
        await runtime.basic_authenticator.close()

"""Authentication bootstrap, OIDC BFF, and local logout routes."""

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from auth.cleanup import cleanup_owner_session
from auth.cookies import get_cookie_value
from auth.middleware import AuthenticatedRequest
from auth.oidc import OidcProviderError, validate_return_to
from auth.runtime import AuthRuntime, get_auth_runtime
from auth.schemas import AuthSessionResponse
from auth.sessions import (
    AuthenticatedSessionCapacityError,
    LoginCorrelationCapacityError,
)
from core.query import NoQuery, reject_query_fields
from core.security import AuthMode
from core.session import OwnerContext, get_browser_session_token, parse_browser_session_id

if TYPE_CHECKING:
    from auth.config import OidcSettings

router = APIRouter(prefix="/auth", tags=["auth"])

_LOGIN_PATH = "/api/auth/oidc/login"
_LOGOUT_PATH = "/api/auth/logout"
_MAX_CALLBACK_VALUE_LENGTH = 4_096


@router.get("/session")
async def get_auth_session(
    request: Request,
    runtime: Annotated[AuthRuntime, Depends(get_auth_runtime)],
    _query: NoQuery,
) -> AuthSessionResponse:
    """Return only safe mode and application-session transport metadata."""
    mode = runtime.settings.mode
    authentication = _request_authentication(request)
    authenticated = mode is AuthMode.NONE or authentication is not None
    return AuthSessionResponse(
        auth_mode=mode,
        authenticated=authenticated,
        login_path=_LOGIN_PATH if mode is AuthMode.OIDC and not authenticated else None,
        logout_path=_LOGOUT_PATH if mode is not AuthMode.NONE and authenticated else None,
        csrf_token=(
            authentication.csrf_token
            if mode is AuthMode.OIDC and authentication is not None
            else None
        ),
    )


@router.get("/oidc/login")
async def oidc_login(
    request: Request,
    runtime: Annotated[AuthRuntime, Depends(get_auth_runtime)],
) -> RedirectResponse:
    """Start one bounded Authorization Code + PKCE login correlation."""
    provider = runtime.oidc_provider
    settings = runtime.settings.oidc
    if runtime.settings.mode is not AuthMode.OIDC or provider is None or settings is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    raw_return_to = _single_query_value(request, "return_to", required=False)
    try:
        return_to = validate_return_to(raw_return_to)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc
    try:
        correlation = runtime.sessions.create_login_correlation(return_to)
    except LoginCorrelationCapacityError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication_capacity_unavailable",
        ) from exc
    location = provider.authorization_url(
        correlation.state,
        correlation.nonce,
        correlation.pkce_verifier,
    )
    response = RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=settings.login_cookie_name,
        value=correlation.browser_binding,
        max_age=settings.login_ttl_seconds,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/oidc/callback")
async def oidc_callback(
    request: Request,
    runtime: Annotated[AuthRuntime, Depends(get_auth_runtime)],
) -> RedirectResponse:
    """Consume one correlation, validate provider tokens, and set an opaque cookie."""
    provider = runtime.oidc_provider
    settings = runtime.settings.oidc
    if runtime.settings.mode is not AuthMode.OIDC or provider is None or settings is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    state_value = _single_query_value(request, "state", required=True)
    if state_value is None:
        raise RuntimeError("required callback state was not returned")
    correlation = runtime.sessions.consume_login_correlation(
        state_value,
        browser_binding=get_cookie_value(request.scope, settings.login_cookie_name),
    )
    if correlation is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication_failed",
        )
    try:
        provider_error = _single_query_value(request, "error", required=False)
        if provider_error is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication_failed",
            )
        code = _single_query_value(request, "code", required=True)
        if code is None:
            raise RuntimeError("required callback code was not returned")
        token_result = await provider.exchange_code(
            code=code,
            verifier=correlation.pkce_verifier,
            nonce=correlation.nonce,
        )
        session = runtime.sessions.establish_session(
            principal=token_result.principal,
            issuer=token_result.issuer,
            subject=token_result.subject,
            nonce=correlation.nonce,
            provider_credentials=token_result.credentials,
            provider_absolute_expires_at=(token_result.credentials.refresh_token_expires_at),
        )
    except HTTPException as exc:
        raise _bound_callback_failure(
            settings,
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc
    except (OidcProviderError, ValueError) as exc:
        raise _bound_callback_failure(
            settings,
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication_failed",
        ) from exc
    except AuthenticatedSessionCapacityError as exc:
        raise _bound_callback_failure(
            settings,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication_capacity_unavailable",
        ) from exc

    response = RedirectResponse(correlation.return_to, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=settings.cookie_name,
        value=session.lookup_token,
        max_age=runtime.sessions.remaining_session_lifetime_seconds(session.lookup_token),
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
        path="/",
    )
    _expire_login_cookie(response, settings)
    return response


def _expire_login_cookie(response: Response, settings: OidcSettings) -> None:
    """Clear a completed login's browser binding with its original attributes."""
    response.delete_cookie(
        key=settings.login_cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _bound_callback_failure(
    settings: OidcSettings,
    *,
    status_code: int,
    detail: object,
) -> HTTPException:
    """Preserve the safe failure envelope while clearing an accepted binding."""
    response = Response()
    _expire_login_cookie(response, settings)
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers={"Set-Cookie": response.headers["set-cookie"]},
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    runtime: Annotated[AuthRuntime, Depends(get_auth_runtime)],
) -> Response:
    """Delete every resource owned by this logical application session."""
    authentication = _request_authentication(request)
    if authentication is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    owner_context = _logout_owner(request, authentication)
    reject_query_fields(request)

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    if runtime.settings.mode is AuthMode.OIDC:
        settings = runtime.settings.oidc
        provider = runtime.oidc_provider
        lookup_token = authentication.session_lookup_token
        if settings is None or provider is None or lookup_token is None:
            raise RuntimeError("OIDC logout is missing established session state")

        removed_session = None

        async def invalidate_session() -> None:
            nonlocal removed_session
            removed_session = runtime.sessions.delete_session(lookup_token)

        # Owner admission stays closed through repository invalidation, so a
        # still-present browser cookie cannot publish after the cleanup edge.
        await cleanup_owner_session(owner_context, finalizer=invalidate_session)
        response.delete_cookie(
            key=settings.cookie_name,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        if removed_session is not None:
            await provider.revoke(removed_session)
    elif runtime.settings.mode is AuthMode.BASIC:
        await cleanup_owner_session(owner_context)
        # Native browser credential caches cannot be revoked server-side. This
        # clears only application-owned cache/storage and makes no such claim.
        response.headers["Clear-Site-Data"] = '"cache", "cookies", "storage"'
    else:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return response


def _request_authentication(request: Request) -> AuthenticatedRequest | None:
    """Read middleware-established provider state without interpreting tokens."""
    value = getattr(request.state, "authentication", None)
    if value is None:
        return None
    if not isinstance(value, AuthenticatedRequest):
        raise RuntimeError("request authentication state has an invalid type")
    return value


def _logout_owner(
    request: Request,
    authentication: AuthenticatedRequest,
) -> OwnerContext:
    """Resolve the stable OIDC owner or Basic logical browser-session owner."""
    if authentication.owner_context is not None:
        return authentication.owner_context
    if authentication.principal.auth_mode is not AuthMode.BASIC:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        session_token = get_browser_session_token(request.headers.getlist("x-session-token"))
        session_id = parse_browser_session_id(session_token) if session_token is not None else None
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if session_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Session-Token header is required for session operations.",
        )
    return OwnerContext(
        principal_id=authentication.principal.principal_id,
        session_id=session_id,
    )


def _single_query_value(request: Request, name: str, *, required: bool) -> str | None:
    """Reject duplicate, empty, or unbounded provider callback parameters."""
    values = request.query_params.getlist(name)
    if not values:
        if required:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
        return None
    if len(values) != 1 or not values[0] or len(values[0]) > _MAX_CALLBACK_VALUE_LENGTH:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    return values[0]

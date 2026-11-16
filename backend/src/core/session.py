"""Application-session ownership dependencies.

The browser session identifier is intentionally separate from authentication.
In disabled and Basic modes it comes from ``X-Session-Token``. OIDC binds the
same owner abstraction to a server-owned application session without using a
provider bearer token as a storage key.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from core.security import AuthMode, Principal, get_principal

if TYPE_CHECKING:
    from collections.abc import Sequence

_BROWSER_SESSION_UUID_VERSION = 4


class AmbiguousBrowserSessionHeaderError(ValueError):
    """Reject repeated session headers instead of choosing an untracked owner."""


@dataclass(frozen=True, slots=True)
class OwnerContext:
    """Stable owner of session-scoped uploads and analysis runs.

    Attributes:
        principal_id: Identity established by the configured auth provider.
        session_id: Browser or authenticated-login session identifier that
            remains stable across access-token refresh.
    """

    principal_id: str
    session_id: str


def get_browser_session_token(values: Sequence[str]) -> str | None:
    """Return an optional singleton header, rejecting even identical duplicates."""
    if len(values) > 1:
        raise AmbiguousBrowserSessionHeaderError("X-Session-Token header must appear only once.")
    return values[0] if values else None


def parse_browser_session_id(raw_session_id: str) -> str:
    """Validate and canonicalize a browser-supplied application-session UUID.

    This is deliberately a pure public parser so pre-body HTTP Basic middleware
    can bind activity to the exact same canonical identifier before FastAPI
    dependencies run.

    Args:
        raw_session_id: Value supplied in ``X-Session-Token``.

    Returns:
        Canonical lowercase UUID text.

    Raises:
        ValueError: When the header is not a canonical UUIDv4.
    """
    try:
        parsed_session_id = UUID(raw_session_id)
    except (AttributeError, ValueError) as exc:
        raise ValueError("X-Session-Token must be a UUIDv4.") from exc

    if (
        parsed_session_id.version != _BROWSER_SESSION_UUID_VERSION
        or str(parsed_session_id) != raw_session_id.lower()
    ):
        raise ValueError("X-Session-Token must be a canonical UUIDv4.")
    return str(parsed_session_id)


def _browser_owner_context(principal: Principal, raw_session_id: str) -> OwnerContext:
    """Map one browser header into safe dependency errors and canonical ownership."""
    try:
        session_id = parse_browser_session_id(raw_session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return OwnerContext(principal_id=principal.principal_id, session_id=session_id)


def _oidc_owner_context(request: Request, principal: Principal) -> OwnerContext:
    """Require the server-owned application session established by OIDC middleware."""
    owner_context = getattr(request.state, "owner_context", None)
    if not isinstance(owner_context, OwnerContext) or (
        owner_context.principal_id != principal.principal_id
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )
    return owner_context


async def get_optional_owner_context(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    x_session_token: Annotated[list[str] | None, Header()] = None,
) -> OwnerContext | None:
    """Resolve session ownership when the browser supplied its session ID.

    Args:
        principal: Caller established by the configured auth provider.
        request: Current request containing provider-established state.
        x_session_token: All browser-session header values for ``none`` and
            ``basic`` modes. OIDC deliberately ignores these headers.

    Returns:
        The owner context, or ``None`` for a request that needs only public
        process-level resources such as the static catalogue.
    """
    if principal.auth_mode is AuthMode.OIDC:
        return _oidc_owner_context(request, principal)
    try:
        session_token = get_browser_session_token(x_session_token or ())
    except AmbiguousBrowserSessionHeaderError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if session_token is None:
        return None
    return _browser_owner_context(principal, session_token)


async def require_owner_context(
    owner_context: Annotated[OwnerContext | None, Depends(get_optional_owner_context)],
) -> OwnerContext:
    """Require an owner for a session-scoped operation.

    Args:
        owner_context: Optional owner resolved from identity and session ID.

    Returns:
        The resolved owner.

    Raises:
        HTTPException: ``400`` when the browser session ID is missing.
    """
    if owner_context is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Session-Token header is required for session operations.",
        )
    return owner_context

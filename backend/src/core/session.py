"""Application-session ownership dependencies.

The browser session identifier is intentionally separate from authentication.
In v1 it comes from ``X-Session-Token``. Future JWT or HTTP Basic providers can
bind the same owner abstraction to a verified principal without using a bearer
token itself as a storage key.
"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from core.security import Principal, get_principal

_BROWSER_SESSION_UUID_VERSION = 4


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


def _parse_browser_session_id(raw_session_id: str) -> str:
    """Validate and canonicalize the current v1 browser-session UUID.

    Args:
        raw_session_id: Value supplied in ``X-Session-Token``.

    Returns:
        Canonical lowercase UUID text.

    Raises:
        HTTPException: ``400`` when the header is not a canonical UUIDv4.
    """
    try:
        parsed_session_id = UUID(raw_session_id)
    except (AttributeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Session-Token must be a UUIDv4.",
        ) from exc

    if (
        parsed_session_id.version != _BROWSER_SESSION_UUID_VERSION
        or str(parsed_session_id) != raw_session_id.lower()
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Session-Token must be a canonical UUIDv4.",
        )
    return str(parsed_session_id)


async def get_optional_owner_context(
    principal: Annotated[Principal, Depends(get_principal)],
    x_session_token: Annotated[str | None, Header()] = None,
) -> OwnerContext | None:
    """Resolve session ownership when the browser supplied its session ID.

    Args:
        principal: Caller established by the configured auth provider.
        x_session_token: Optional v1 browser-session UUID header.

    Returns:
        The owner context, or ``None`` for a request that needs only public
        process-level resources such as the static catalogue.
    """
    if x_session_token is None:
        return None
    return OwnerContext(
        principal_id=principal.principal_id,
        session_id=_parse_browser_session_id(x_session_token),
    )


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

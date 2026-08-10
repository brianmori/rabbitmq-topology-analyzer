"""Authentication and application-role seams.

Authentication is disabled in v1, but routers depend on the authorization
functions in this module now. A later JWT or HTTP Basic implementation can
replace :func:`get_principal` without changing every business-domain router.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, status


class AuthMode(StrEnum):
    """Supported authentication modes.

    Only ``NONE`` is implemented in v1. ``JWT`` and ``BASIC`` reserve the
    stable vocabulary used by the future deployment-selectable providers.
    """

    NONE = "none"
    JWT = "jwt"
    BASIC = "basic"


class AppRole(StrEnum):
    """Application authorization roles."""

    VIEWER = "viewer"
    ANALYST = "analyst"


@dataclass(frozen=True, slots=True)
class Principal:
    """Verified application identity and its granted roles.

    Attributes:
        principal_id: Stable identity chosen by the authentication provider.
            A future JWT provider derives this from verified issuer and subject
            claims, never from bearer-token bytes or a per-token identifier.
        roles: Roles granted to this identity.
        auth_mode: Provider that established the identity.
    """

    principal_id: str
    roles: frozenset[AppRole]
    auth_mode: AuthMode

    def permits(self, required_role: AppRole) -> bool:
        """Return whether this principal satisfies an application role.

        ``analyst`` includes every ``viewer`` permission and adds upload
        management. Keeping that implication here prevents individual routers
        from implementing the role hierarchy differently.

        Args:
            required_role: Role required by the protected operation.

        Returns:
            ``True`` when the principal may perform the operation.
        """
        if required_role is AppRole.VIEWER:
            return AppRole.VIEWER in self.roles or AppRole.ANALYST in self.roles
        return required_role in self.roles


_ANONYMOUS_ANALYST = Principal(
    principal_id="anonymous",
    roles=frozenset({AppRole.ANALYST}),
    auth_mode=AuthMode.NONE,
)


async def get_principal() -> Principal:
    """Return the caller principal for the configured authentication mode.

    Returns:
        An anonymous analyst while authentication is disabled. This preserves
        current v1 behavior while all routers still exercise the same role
        dependencies that future authenticated modes will use.
    """
    return _ANONYMOUS_ANALYST


def _require_role(principal: Principal, required_role: AppRole) -> Principal:
    """Enforce one role after authentication has established a principal.

    Args:
        principal: Verified caller identity.
        required_role: Minimum role needed by the endpoint.

    Returns:
        The unchanged principal for downstream dependencies.

    Raises:
        HTTPException: ``403`` when the authenticated principal lacks the role.
    """
    if not principal.permits(required_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"The {required_role.value} role is required.",
        )
    return principal


async def require_viewer(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """Require permission to view topology and run graph operations.

    Args:
        principal: Caller identity supplied by the configured auth provider.

    Returns:
        The authorized principal.
    """
    return _require_role(principal, AppRole.VIEWER)


async def require_analyst(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """Require permission to create, replace, or delete session uploads.

    Args:
        principal: Caller identity supplied by the configured auth provider.

    Returns:
        The authorized principal.
    """
    return _require_role(principal, AppRole.ANALYST)

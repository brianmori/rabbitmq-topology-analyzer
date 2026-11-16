"""Authentication and application-role seams.

Authentication is disabled by default, but routers depend on the authorization
functions in this module. OIDC or HTTP Basic can establish the principal
without changing every business-domain router.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

_DEFAULT_BASIC_REALM = "RabbitMQ Topology Analyzer"


class AuthMode(StrEnum):
    """Supported authentication modes.

    A deployment selects exactly one provider. OIDC is a browser-facing BFF
    mode even when its provider happens to encode tokens as JWTs.
    """

    NONE = "none"
    OIDC = "oidc"
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
            The OIDC provider derives this from verified issuer and subject
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


@dataclass(frozen=True, slots=True)
class _AuthenticationDependencyConfiguration:
    """Small provider-neutral dependency state configured during lifespan."""

    mode: AuthMode = AuthMode.NONE
    basic_realm: str = _DEFAULT_BASIC_REALM


@dataclass(slots=True)
class _AuthenticationDependencyState:
    """Mutable holder whose configuration value is replaced atomically."""

    configuration: _AuthenticationDependencyConfiguration


_dependency_state = _AuthenticationDependencyState(
    configuration=_AuthenticationDependencyConfiguration(),
)


def configure_authentication_dependencies(
    mode: AuthMode,
    *,
    basic_realm: str = _DEFAULT_BASIC_REALM,
) -> None:
    """Select whether missing middleware state may use the disabled-mode fallback.

    Authentication middleware remains responsible for credential verification.
    This narrow seam prevents an accidentally unwrapped authenticated deployment
    from silently receiving the legacy anonymous analyst. HTTP Basic retains its
    configured challenge if a protected dependency is reached without verified
    request state.

    Args:
        mode: The sole deployment-selected authentication mode.
        basic_realm: Startup-validated realm used only for a Basic ``401``.
    """
    _dependency_state.configuration = _AuthenticationDependencyConfiguration(
        mode=mode,
        basic_realm=basic_realm,
    )


def _unauthorized() -> HTTPException:
    """Return the standardized dependency-level authentication failure."""
    headers = None
    configuration = _dependency_state.configuration
    if configuration.mode is AuthMode.BASIC:
        realm = configuration.basic_realm
        headers = {"WWW-Authenticate": f'Basic realm="{realm}", charset="UTF-8"'}
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication is required.",
        headers=headers,
    )


async def get_principal(request: Request) -> Principal:
    """Return middleware-verified identity or the explicit disabled-mode fallback.

    The pre-body middleware stores a verified :class:`Principal` in the ASGI
    request state. Authenticated modes never synthesize identity when that state
    is missing. The default dependency configuration remains ``none`` so legacy
    local/test application construction preserves the established behavior until
    lifespan explicitly selects another mode.

    Args:
        request: Current request whose state was established by auth middleware.

    Returns:
        The verified caller, or the synthetic analyst only in configured
        disabled mode.

    Raises:
        HTTPException: ``401`` when authenticated mode has no verified state.
    """
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal):
        return principal
    if _dependency_state.configuration.mode is AuthMode.NONE:
        return _ANONYMOUS_ANALYST
    raise _unauthorized()


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

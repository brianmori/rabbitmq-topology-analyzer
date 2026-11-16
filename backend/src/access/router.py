"""Caller capability-discovery endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends

from access.schemas import AccessCapabilities, AccessResponse
from core.query import NoQuery  # noqa: TC001 - FastAPI resolves at runtime.
from core.security import AppRole, Principal, get_principal

router = APIRouter(tags=["access"])


@router.get("/access")
async def get_access(
    principal: Annotated[Principal, Depends(get_principal)],
    _query: NoQuery,
) -> AccessResponse:
    """Return roles and effective capabilities for the current caller.

    The response deliberately omits the provider's subject identifier. The
    frontend needs capabilities to present allowed actions, while backend route
    dependencies remain authoritative.

    Args:
        principal: Caller established by the configured authentication provider.

    Returns:
        The safe role and capability view for this request.
    """
    can_view = principal.permits(AppRole.VIEWER)
    return AccessResponse(
        auth_mode=principal.auth_mode,
        roles=tuple(sorted(principal.roles, key=lambda role: role.value)),
        capabilities=AccessCapabilities(
            view_topology=can_view,
            run_graph_operations=can_view,
            manage_uploads=principal.permits(AppRole.ANALYST),
        ),
    )

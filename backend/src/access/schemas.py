"""Wire schemas for caller capability discovery."""

from core.schemas import CamelModel
from core.security import AppRole, AuthMode  # noqa: TC001 - Pydantic resolves fields at runtime.


class AccessCapabilities(CamelModel):
    """Effective application capabilities for the current principal."""

    view_topology: bool
    run_graph_operations: bool
    manage_uploads: bool


class AccessResponse(CamelModel):
    """Safe authorization context consumed by the frontend."""

    auth_mode: AuthMode
    roles: tuple[AppRole, ...]
    capabilities: AccessCapabilities

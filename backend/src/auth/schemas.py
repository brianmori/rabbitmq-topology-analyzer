"""Safe browser-facing authentication bootstrap schemas."""

from core.schemas import CamelModel
from core.security import AuthMode  # noqa: TC001 - Pydantic resolves fields at runtime.


class AuthSessionResponse(CamelModel):
    """Provider-neutral session metadata that contains no provider claims."""

    auth_mode: AuthMode
    authenticated: bool
    login_path: str | None
    logout_path: str | None
    csrf_token: str | None

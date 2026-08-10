import asyncio
from uuid import uuid1, uuid4

import pytest
from fastapi import HTTPException

from core.security import (
    AppRole,
    AuthMode,
    Principal,
    get_principal,
    require_analyst,
    require_viewer,
)
from core.session import (
    OwnerContext,
    get_optional_owner_context,
    require_owner_context,
)


def test_disabled_authentication_preserves_analyst_access() -> None:
    principal = asyncio.run(get_principal())

    assert principal == Principal(
        principal_id="anonymous",
        roles=frozenset({AppRole.ANALYST}),
        auth_mode=AuthMode.NONE,
    )
    assert principal.permits(AppRole.ANALYST)
    assert principal.permits(AppRole.VIEWER)


def test_analyst_role_satisfies_both_authorization_dependencies() -> None:
    principal = Principal(
        principal_id="engineer",
        roles=frozenset({AppRole.ANALYST}),
        auth_mode=AuthMode.JWT,
    )

    assert asyncio.run(require_viewer(principal)) is principal
    assert asyncio.run(require_analyst(principal)) is principal


def test_viewer_cannot_use_analyst_dependency() -> None:
    principal = Principal(
        principal_id="observer",
        roles=frozenset({AppRole.VIEWER}),
        auth_mode=AuthMode.BASIC,
    )

    assert asyncio.run(require_viewer(principal)) is principal
    with pytest.raises(HTTPException) as exception_info:
        asyncio.run(require_analyst(principal))

    assert exception_info.value.status_code == 403


def test_optional_owner_may_be_omitted() -> None:
    principal = Principal(
        principal_id="observer",
        roles=frozenset({AppRole.VIEWER}),
        auth_mode=AuthMode.JWT,
    )

    assert asyncio.run(get_optional_owner_context(principal, None)) is None


def test_owner_binds_principal_to_canonical_browser_session() -> None:
    principal = Principal(
        principal_id="engineer",
        roles=frozenset({AppRole.ANALYST}),
        auth_mode=AuthMode.JWT,
    )
    browser_session_id = str(uuid4())

    owner = asyncio.run(
        get_optional_owner_context(principal, browser_session_id.upper()),
    )

    assert owner == OwnerContext(
        principal_id="engineer",
        session_id=browser_session_id,
    )
    assert owner != OwnerContext(
        principal_id="another-engineer",
        session_id=browser_session_id,
    )


@pytest.mark.parametrize(
    "invalid_session_id",
    [
        "not-a-uuid",
        str(uuid1()),
        f"{{{uuid4()}}}",
    ],
)
def test_owner_rejects_invalid_browser_session_uuid(invalid_session_id: str) -> None:
    principal = Principal(
        principal_id="engineer",
        roles=frozenset({AppRole.ANALYST}),
        auth_mode=AuthMode.JWT,
    )

    with pytest.raises(HTTPException) as exception_info:
        asyncio.run(get_optional_owner_context(principal, invalid_session_id))

    assert exception_info.value.status_code == 400


def test_required_owner_rejects_missing_browser_session() -> None:
    with pytest.raises(HTTPException) as exception_info:
        asyncio.run(require_owner_context(None))

    assert exception_info.value.status_code == 400

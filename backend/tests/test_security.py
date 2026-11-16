import asyncio
from uuid import uuid1, uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from core.security import (
    AppRole,
    AuthMode,
    Principal,
    configure_authentication_dependencies,
    get_principal,
    require_analyst,
    require_viewer,
)
from core.session import (
    OwnerContext,
    get_optional_owner_context,
    parse_browser_session_id,
    require_owner_context,
)


@pytest.fixture(autouse=True)
def disabled_dependency_fallback() -> None:
    configure_authentication_dependencies(AuthMode.NONE)


def _request(*, principal: Principal | None = None, owner: OwnerContext | None = None) -> Request:
    state: dict[str, object] = {}
    if principal is not None:
        state["principal"] = principal
    if owner is not None:
        state["owner_context"] = owner
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/test",
            "headers": [],
            "state": state,
        }
    )


def test_disabled_authentication_preserves_analyst_access() -> None:
    principal = asyncio.run(get_principal(_request()))

    assert principal == Principal(
        principal_id="anonymous",
        roles=frozenset({AppRole.ANALYST}),
        auth_mode=AuthMode.NONE,
    )
    assert principal.permits(AppRole.ANALYST)
    assert principal.permits(AppRole.VIEWER)


def test_middleware_verified_principal_takes_precedence_over_none_fallback() -> None:
    verified = Principal(
        principal_id="verified",
        roles=frozenset({AppRole.VIEWER}),
        auth_mode=AuthMode.BASIC,
    )

    assert asyncio.run(get_principal(_request(principal=verified))) is verified


@pytest.mark.parametrize("mode", [AuthMode.OIDC, AuthMode.BASIC])
def test_authenticated_modes_never_fall_back_to_anonymous(mode: AuthMode) -> None:
    configure_authentication_dependencies(mode)

    with pytest.raises(HTTPException) as exception_info:
        asyncio.run(get_principal(_request()))

    assert exception_info.value.status_code == 401
    assert exception_info.value.detail == "Authentication is required."
    if mode is AuthMode.BASIC:
        assert exception_info.value.headers == {
            "WWW-Authenticate": ('Basic realm="RabbitMQ Topology Analyzer", charset="UTF-8"')
        }
    else:
        assert exception_info.value.headers is None


def test_basic_dependency_uses_the_configured_challenge_realm() -> None:
    configure_authentication_dependencies(AuthMode.BASIC, basic_realm="Topology Test")

    with pytest.raises(HTTPException) as exception_info:
        asyncio.run(get_principal(_request()))

    assert exception_info.value.headers == {
        "WWW-Authenticate": 'Basic realm="Topology Test", charset="UTF-8"'
    }


def test_analyst_role_satisfies_both_authorization_dependencies() -> None:
    principal = Principal(
        principal_id="engineer",
        roles=frozenset({AppRole.ANALYST}),
        auth_mode=AuthMode.OIDC,
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
        auth_mode=AuthMode.BASIC,
    )

    assert asyncio.run(get_optional_owner_context(_request(), principal, None)) is None


def test_owner_binds_principal_to_canonical_browser_session() -> None:
    principal = Principal(
        principal_id="engineer",
        roles=frozenset({AppRole.ANALYST}),
        auth_mode=AuthMode.BASIC,
    )
    browser_session_id = str(uuid4())

    owner = asyncio.run(
        get_optional_owner_context(_request(), principal, [browser_session_id.upper()]),
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
        auth_mode=AuthMode.BASIC,
    )

    with pytest.raises(HTTPException) as exception_info:
        asyncio.run(get_optional_owner_context(_request(), principal, [invalid_session_id]))

    assert exception_info.value.status_code == 400


def test_public_browser_session_parser_is_pure_and_canonical() -> None:
    browser_session_id = str(uuid4())

    assert parse_browser_session_id(browser_session_id.upper()) == browser_session_id
    with pytest.raises(ValueError, match="canonical UUIDv4"):
        parse_browser_session_id(f"{{{browser_session_id}}}")


def test_oidc_uses_only_middleware_owner_and_ignores_browser_session_header() -> None:
    principal = Principal(
        principal_id="oidc-principal",
        roles=frozenset({AppRole.ANALYST}),
        auth_mode=AuthMode.OIDC,
    )
    owner = OwnerContext(principal_id=principal.principal_id, session_id=str(uuid4()))
    request = _request(principal=principal, owner=owner)

    resolved = asyncio.run(
        get_optional_owner_context(request, principal, ["not-a-browser-session-token"])
    )

    assert resolved is owner


@pytest.mark.parametrize(
    "owner",
    [
        None,
        OwnerContext(principal_id="another-principal", session_id=str(uuid4())),
    ],
)
def test_oidc_rejects_missing_or_mismatched_middleware_owner(
    owner: OwnerContext | None,
) -> None:
    principal = Principal(
        principal_id="oidc-principal",
        roles=frozenset({AppRole.VIEWER}),
        auth_mode=AuthMode.OIDC,
    )

    with pytest.raises(HTTPException) as exception_info:
        asyncio.run(
            get_optional_owner_context(
                _request(principal=principal, owner=owner),
                principal,
            )
        )

    assert exception_info.value.status_code == 401


def test_required_owner_rejects_missing_browser_session() -> None:
    with pytest.raises(HTTPException) as exception_info:
        asyncio.run(require_owner_context(None))

    assert exception_info.value.status_code == 400

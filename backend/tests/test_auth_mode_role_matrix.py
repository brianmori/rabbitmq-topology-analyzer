from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rabbitmq_management_sdk.topology import ClusterTopology

import auth.router as auth_router
from access.router import router as access_router
from analyses.router import router as analyses_router
from analyses.snapshots import SnapshotMetadata, create_analysis_snapshot
from auth.config import AuthSettings, BasicSettings, OidcSettings
from auth.middleware import AuthenticatedRequest, PreBodyAuthMiddleware
from auth.router import router as auth_session_router
from auth.runtime import AuthRuntime
from auth.sessions import AuthSessionRepository
from core import state
from core.deployment import DeploymentMode
from core.errors import register_error_handlers
from core.security import (
    AppRole,
    AuthMode,
    Principal,
    configure_authentication_dependencies,
)
from core.session import OwnerContext
from core.state import (
    AnalysisSource,
    CaptureCompleteness,
    configure_analysis_store,
    publish_session_analysis,
    replace_static_analyses,
)
from topology.router import router as topology_router

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.types import ASGIApp, Scope

    from auth.basic import BasicAuthenticator
    from auth.oidc import OidcProvider

_PUBLIC_ORIGIN = "https://topology.example.test"
_OIDC_COOKIE_NAME = "matrix_session"
_OIDC_LOOKUP_TOKEN = "opaque-matrix-session"
_BASIC_CREDENTIAL = "Basic deterministic-test-credential"
_CSRF_TOKEN = "deterministic-csrf-token"
_BASIC_REALM = "Topology Matrix"


@dataclass(frozen=True, slots=True)
class _StaticBasicAuthenticator:
    authentication: AuthenticatedRequest

    async def __call__(
        self,
        credential: str,
        _scope: Scope,
    ) -> AuthenticatedRequest | None:
        return self.authentication if credential == _BASIC_CREDENTIAL else None


@dataclass(frozen=True, slots=True)
class _StaticOidcProvider:
    authentication: AuthenticatedRequest

    async def authenticate_session(
        self,
        _repository: AuthSessionRepository,
        credential: str,
    ) -> AuthenticatedRequest | None:
        return self.authentication if credential == _OIDC_LOOKUP_TOKEN else None


@pytest.fixture(autouse=True)
def isolated_request_matrix(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("RABBITMQ_SESSION_ANALYSIS_LIMIT", raising=False)
    monkeypatch.delenv("RABBITMQ_SESSION_ANALYSIS_GLOBAL_LIMIT", raising=False)
    monkeypatch.delenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", raising=False)
    state._session_store.clear()
    replace_static_analyses({})
    configure_analysis_store()
    configure_authentication_dependencies(AuthMode.NONE)
    yield
    state._session_store.clear()
    replace_static_analyses({})
    configure_authentication_dependencies(AuthMode.NONE)


def _oidc_settings() -> OidcSettings:
    return OidcSettings(
        issuer="https://issuer.example.test",
        discovery_url="https://issuer.example.test/.well-known/openid-configuration",
        client_id="matrix-client",
        client_secret_file=Path(__file__),
        client_secret="unused-test-secret",
        redirect_uri=f"{_PUBLIC_ORIGIN}/api/auth/oidc/callback",
        scopes=("openid",),
        role_claim="groups",
        role_mapping_file=Path(__file__),
        role_mapping=MappingProxyType({}),
        accepted_algorithms=("RS256",),
        clock_skew_seconds=60,
        http_timeout_seconds=5,
        refresh_leeway_seconds=60,
        session_lifetime_seconds=3_600,
        session_limit=16,
        login_ttl_seconds=600,
        login_limit=16,
        jwks_cache_seconds=300,
        cookie_name=_OIDC_COOKIE_NAME,
        cookie_secure=True,
    )


def _basic_settings() -> BasicSettings:
    return BasicSettings(
        users_file=Path(__file__),
        realm=_BASIC_REALM,
        users=MappingProxyType({}),
    )


def _runtime(
    mode: AuthMode,
    roles: frozenset[AppRole] = frozenset(),
    *,
    principal_id: str = "matrix-principal",
    oidc_session_id: str | None = None,
) -> AuthRuntime:
    sessions = AuthSessionRepository()
    if mode is AuthMode.NONE:
        return AuthRuntime(
            settings=AuthSettings(
                mode=mode,
                deployment_mode=DeploymentMode.DEVELOPMENT,
                public_origin=None,
                oidc=None,
                basic=None,
            ),
            sessions=sessions,
        )

    principal = Principal(
        principal_id=principal_id,
        roles=roles,
        auth_mode=mode,
    )
    if mode is AuthMode.OIDC:
        settings = _oidc_settings()
        owner = OwnerContext(
            principal_id=principal_id,
            session_id=oidc_session_id or str(uuid4()),
        )
        authentication = AuthenticatedRequest(
            principal=principal,
            owner_context=owner,
            csrf_token=_CSRF_TOKEN,
            session_lookup_token=_OIDC_LOOKUP_TOKEN,
        )
        provider = cast("OidcProvider", _StaticOidcProvider(authentication))
        return AuthRuntime(
            settings=AuthSettings(
                mode=mode,
                deployment_mode=DeploymentMode.DEVELOPMENT,
                public_origin=_PUBLIC_ORIGIN,
                oidc=settings,
                basic=None,
            ),
            sessions=sessions,
            oidc_provider=provider,
        )

    settings = _basic_settings()
    authentication = AuthenticatedRequest(principal=principal)
    authenticator = cast("BasicAuthenticator", _StaticBasicAuthenticator(authentication))
    return AuthRuntime(
        settings=AuthSettings(
            mode=mode,
            deployment_mode=DeploymentMode.DEVELOPMENT,
            public_origin=None,
            oidc=None,
            basic=settings,
        ),
        sessions=sessions,
        basic_authenticator=authenticator,
    )


def _client(runtime: AuthRuntime) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(auth_session_router, prefix="/api")
    app.include_router(access_router, prefix="/api")
    app.include_router(analyses_router, prefix="/api")
    app.include_router(topology_router, prefix="/api")
    app.dependency_overrides[auth_router.get_auth_runtime] = lambda: runtime

    settings = runtime.settings
    oidc = settings.oidc
    basic = settings.basic
    wrapped: ASGIApp = PreBodyAuthMiddleware(
        app,
        mode=settings.mode,
        authenticator=None if settings.mode is AuthMode.NONE else runtime.authenticate,
        public_origin=settings.public_origin,
        oidc_cookie_name=oidc.cookie_name if oidc is not None else "unused",
        oidc_cookie_secure=oidc.cookie_secure if oidc is not None else True,
        basic_realm=basic.realm if basic is not None else _BASIC_REALM,
    )
    configure_authentication_dependencies(
        settings.mode,
        basic_realm=basic.realm if basic is not None else _BASIC_REALM,
    )
    return TestClient(wrapped)


def _headers(
    mode: AuthMode,
    *,
    browser_session_id: str | None = None,
    unsafe: bool = False,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if mode is AuthMode.OIDC:
        headers["Cookie"] = f"{_OIDC_COOKIE_NAME}={_OIDC_LOOKUP_TOKEN}"
    elif mode is AuthMode.BASIC:
        headers["Authorization"] = _BASIC_CREDENTIAL
    if browser_session_id is not None:
        headers["X-Session-Token"] = browser_session_id
    if mode is AuthMode.OIDC and unsafe:
        headers["Origin"] = _PUBLIC_ORIGIN
        headers["X-CSRF-Token"] = _CSRF_TOKEN
    return headers


def _publish_private_capture(owner: OwnerContext, analysis_id: str) -> None:
    topology = ClusterTopology(
        exchanges=frozenset(),
        queues=frozenset(),
        shovels=frozenset(),
        edges=frozenset(),
        cluster_id="matrix-cluster",
        cluster_name="Matrix cluster",
    )
    snapshot = create_analysis_snapshot(
        topology,
        SnapshotMetadata(
            analysis_id=analysis_id,
            source=AnalysisSource.SESSION,
            label="Private matrix capture",
            capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
            snapshot_revision="sha256:" + "a" * 64,
            cluster_tags={},
            in_cluster_amqp_hosts=(),
            vhost_count=0,
        ),
    )
    publish_session_analysis(owner, snapshot)


def test_none_mode_preserves_synthetic_analyst_behavior() -> None:
    runtime = _runtime(AuthMode.NONE)
    with _client(runtime) as client:
        session = client.get("/api/auth/session")
        access = client.get("/api/access")
        catalogue = client.get("/api/analyses")

    assert session.status_code == 200
    assert session.json() == {
        "authMode": "none",
        "authenticated": True,
        "loginPath": None,
        "logoutPath": None,
        "csrfToken": None,
    }
    assert access.status_code == 200
    assert access.json() == {
        "authMode": "none",
        "roles": ["analyst"],
        "capabilities": {
            "viewTopology": True,
            "runGraphOperations": True,
            "manageUploads": True,
        },
    }
    assert catalogue.status_code == 200


@pytest.mark.parametrize("mode", [AuthMode.OIDC, AuthMode.BASIC])
@pytest.mark.parametrize(
    ("roles", "expected_roles", "expected_capabilities", "product_status", "delete_status"),
    [
        (
            frozenset({AppRole.VIEWER}),
            ["viewer"],
            (True, True, False),
            200,
            403,
        ),
        (
            frozenset({AppRole.ANALYST}),
            ["analyst"],
            (True, True, True),
            200,
            404,
        ),
        (frozenset(), [], (False, False, False), 403, 403),
    ],
    ids=["viewer", "analyst", "no-role"],
)
def test_authenticated_role_and_product_isolation_matrix(
    mode: AuthMode,
    roles: frozenset[AppRole],
    expected_roles: list[str],
    expected_capabilities: tuple[bool, bool, bool],
    product_status: int,
    delete_status: int,
) -> None:
    browser_session_id = str(uuid4())
    runtime = _runtime(mode, roles)
    headers = _headers(mode, browser_session_id=browser_session_id)
    with _client(runtime) as client:
        session = client.get("/api/auth/session", headers=headers)
        access = client.get("/api/access", headers=headers)
        catalogue = client.get("/api/analyses", headers=headers)
        deletion = client.delete(
            "/api/analyses/missing-capture",
            headers=_headers(
                mode,
                browser_session_id=browser_session_id,
                unsafe=True,
            ),
        )

    view_topology, run_graph_operations, manage_uploads = expected_capabilities
    assert session.status_code == 200
    assert session.json()["authenticated"] is True
    assert session.json()["authMode"] == mode.value
    assert access.status_code == 200
    assert access.json() == {
        "authMode": mode.value,
        "roles": expected_roles,
        "capabilities": {
            "viewTopology": view_topology,
            "runGraphOperations": run_graph_operations,
            "manageUploads": manage_uploads,
        },
    }
    assert catalogue.status_code == product_status
    assert deletion.status_code == delete_status


def test_oidc_ownership_ignores_browser_supplied_session_token() -> None:
    oidc_session_id = str(uuid4())
    owner = OwnerContext(principal_id="matrix-principal", session_id=oidc_session_id)
    _publish_private_capture(owner, "oidc-private")
    runtime = _runtime(
        AuthMode.OIDC,
        frozenset({AppRole.VIEWER}),
        oidc_session_id=oidc_session_id,
    )

    with _client(runtime) as client:
        response = client.get(
            "/api/analyses",
            headers=_headers(
                AuthMode.OIDC,
                browser_session_id="not-a-valid-browser-session",
            ),
        )

    assert response.status_code == 200
    assert [entry["analysisId"] for entry in response.json()["session"]] == ["oidc-private"]


def test_basic_ownership_binds_canonical_browser_session_token() -> None:
    owned_session_id = str(uuid4())
    other_session_id = str(uuid4())
    owner = OwnerContext(principal_id="matrix-principal", session_id=owned_session_id)
    _publish_private_capture(owner, "basic-private")
    runtime = _runtime(AuthMode.BASIC, frozenset({AppRole.VIEWER}))

    with _client(runtime) as client:
        owned = client.get(
            "/api/analyses",
            headers=_headers(
                AuthMode.BASIC,
                browser_session_id=owned_session_id.upper(),
            ),
        )
        other = client.get(
            "/api/analyses",
            headers=_headers(AuthMode.BASIC, browser_session_id=other_session_id),
        )
        omitted = client.get(
            "/api/analyses",
            headers=_headers(AuthMode.BASIC),
        )

    assert [entry["analysisId"] for entry in owned.json()["session"]] == ["basic-private"]
    assert other.json()["session"] == []
    assert omitted.json()["session"] == []


@pytest.mark.parametrize("mode", [AuthMode.OIDC, AuthMode.BASIC])
def test_other_owner_private_capture_is_indistinguishable_from_missing(mode: AuthMode) -> None:
    owner_session_id = str(uuid4())
    caller_session_id = str(uuid4())
    owner = OwnerContext(principal_id="matrix-principal", session_id=owner_session_id)
    _publish_private_capture(owner, "other-owner-private")
    runtime = _runtime(
        mode,
        frozenset({AppRole.VIEWER}),
        oidc_session_id=caller_session_id,
    )
    supplied_session_id = owner_session_id if mode is AuthMode.OIDC else caller_session_id
    headers = _headers(mode, browser_session_id=supplied_session_id)

    with _client(runtime) as client:
        private = client.get(
            "/api/topology?source=session&analysis_id=other-owner-private",
            headers=headers,
        )
        missing = client.get(
            "/api/topology?source=session&analysis_id=does-not-exist",
            headers=headers,
        )

    assert private.status_code == 404
    assert (
        private.json()
        == missing.json()
        == {
            "error": {
                "code": "not_found",
                "message": "The requested resource was not found.",
                "issues": [],
            }
        }
    )

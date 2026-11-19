import asyncio
import logging
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx2
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rabbitmq_management_sdk.topology import ClusterTopology

import auth.router as auth_router
from analyses.snapshots import SnapshotMetadata, create_analysis_snapshot
from auth.activity import OwnerActivityRegistry
from auth.basic import BasicAuthenticator
from auth.config import AuthSettings, BasicSettings, BasicUser
from auth.middleware import PreBodyAuthMiddleware
from auth.oidc import OidcProvider
from auth.router import router
from auth.runtime import AuthRuntime
from auth.sessions import AuthSessionRepository
from core.deployment import DeploymentMode
from core.errors import register_error_handlers
from core.security import AppRole, AuthMode
from core.state import (
    AnalysisSource,
    CaptureCompleteness,
    delete_owner_session_analyses,
    list_session_analyses,
    publish_session_analysis,
)
from jobs.results import ResultRepository
from test_oidc import ISSUER, MALFORMED_PROVIDER_JSON, FakeOidcProvider, _settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from starlette.types import ASGIApp

    from auth.sessions import AuthenticatedSession, LoginCorrelation
    from core.session import OwnerContext


def _runtime_app(runtime: AuthRuntime) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router, prefix="/api")
    app.dependency_overrides[auth_router.get_auth_runtime] = lambda: runtime
    return app


def _oidc_runtime() -> tuple[AuthRuntime, FakeOidcProvider, httpx2.AsyncClient]:
    fake = FakeOidcProvider()
    provider_client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=fake.app),
        base_url="http://127.0.0.1",
    )
    settings = _settings()
    provider = OidcProvider(settings, client=provider_client, clock=lambda: fake.now)
    asyncio.run(provider.start())
    repository = AuthSessionRepository(clock=lambda: fake.now)
    runtime = AuthRuntime(
        settings=AuthSettings(
            mode=AuthMode.OIDC,
            deployment_mode=DeploymentMode.DEVELOPMENT,
            public_origin="http://127.0.0.1",
            oidc=settings,
            basic=None,
        ),
        sessions=repository,
        oidc_provider=provider,
    )
    return runtime, fake, provider_client


def _wrapped_client(runtime: AuthRuntime, app: FastAPI) -> TestClient:
    settings = runtime.settings
    oidc = settings.oidc
    basic = settings.basic
    wrapped: ASGIApp = PreBodyAuthMiddleware(
        app,
        mode=settings.mode,
        authenticator=runtime.authenticate,
        public_origin=settings.public_origin,
        oidc_cookie_name=oidc.cookie_name if oidc is not None else "unused",
        oidc_cookie_secure=oidc.cookie_secure if oidc is not None else True,
        basic_realm=basic.realm if basic is not None else "Topology",
        activity_registry=OwnerActivityRegistry(),
    )
    return TestClient(wrapped)


def _complete_oidc_login(
    client: TestClient,
    runtime: AuthRuntime,
    fake: FakeOidcProvider,
) -> tuple[AuthenticatedSession, dict[str, object], str]:
    anonymous = client.get("/api/auth/session")
    assert anonymous.json() == {
        "authMode": "oidc",
        "authenticated": False,
        "loginPath": "/api/auth/oidc/login",
        "logoutPath": None,
        "csrfToken": None,
    }
    correlation = _begin_oidc_login(client, runtime, fake)
    state = correlation.state

    callback = client.get(
        "/api/auth/oidc/callback",
        params={
            "state": state,
            "code": "one-time-code",
            "session_state": "provider-session",
            "iss": ISSUER,
        },
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/workbench?focus=cycles"
    assert "HttpOnly" in callback.headers["set-cookie"]
    assert "Domain=" not in callback.headers["set-cookie"]
    assert "access-" not in callback.headers["set-cookie"]
    assert client.cookies.get("test_session_login") is None
    assert any(
        cookie.startswith("test_session_login=") and "Max-Age=0" in cookie
        for cookie in callback.headers.get_list("set-cookie")
    )

    established = client.get("/api/auth/session")
    body = established.json()
    assert body["authenticated"] is True
    assert body["csrfToken"]
    cookie = client.cookies.get("test_session")
    assert cookie is not None
    session = runtime.sessions.get_session(cookie)
    assert session is not None
    return session, body, state


def _begin_oidc_login(
    client: TestClient,
    runtime: AuthRuntime,
    fake: FakeOidcProvider,
) -> LoginCorrelation:
    login = client.get(
        "/api/auth/oidc/login?return_to=/workbench%3Ffocus%3Dcycles",
        follow_redirects=False,
    )
    assert login.status_code == 303
    provider_query = parse_qs(urlsplit(login.headers["location"]).query)
    assert provider_query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in provider_query
    state = provider_query["state"][0]
    fake.expected_nonce = provider_query["nonce"][0]
    correlation = runtime.sessions._login_correlations[state]
    fake.expected_verifier = correlation.pkce_verifier
    assert client.cookies.get("test_session_login") == correlation.browser_binding
    assert correlation.browser_binding not in login.headers["location"]
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=lax" in login.headers["set-cookie"]
    assert "Path=/" in login.headers["set-cookie"]
    assert "Max-Age=600" in login.headers["set-cookie"]
    assert "Domain=" not in login.headers["set-cookie"]
    return correlation


def test_oidc_login_callback_bootstrap_replay_refresh_and_logout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake, provider_client = _oidc_runtime()
    app = _runtime_app(runtime)
    cleaned: list[OwnerContext] = []

    async def cleanup(
        owner: OwnerContext,
        *,
        finalizer: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        cleaned.append(owner)
        if finalizer is not None:
            await finalizer()

    monkeypatch.setattr(auth_router, "cleanup_owner_session", cleanup)
    try:
        with _wrapped_client(runtime, app) as client:
            session, body, state = _complete_oidc_login(client, runtime, fake)

            replay = client.get(
                "/api/auth/oidc/callback",
                params={"state": state, "code": "one-time-code"},
            )
            assert replay.status_code == 401
            owner_before = session.owner_context

            # The initial fake access credential is within refresh leeway. A
            # protected bootstrap refreshes server-side and retains ownership.
            refreshed = client.get("/api/auth/session")
            assert refreshed.status_code == 200
            current = runtime.sessions.get_session(session.lookup_token)
            assert current is not None
            assert current.owner_context == owner_before
            assert current.provider_credentials.access_token == "access-2"

            rejected_logout = client.post(
                "/api/auth/logout",
                params={"unexpected": "private-value"},
            )
            assert rejected_logout.status_code == 403
            malformed_logout = client.post(
                "/api/auth/logout",
                params={"amqp://user:query-secret@private.invalid/": "private-value"},
                headers={
                    "Origin": "http://127.0.0.1",
                    "X-CSRF-Token": body["csrfToken"],
                },
            )
            assert malformed_logout.status_code == 422
            assert malformed_logout.json()["error"]["issues"] == [
                {"field": "query", "code": "extra_forbidden", "message": "Unknown field."},
            ]
            assert cleaned == []
            assert runtime.sessions.session_count == 1
            assert fake.revocations == []
            logout = client.post(
                "/api/auth/logout",
                headers={
                    "Origin": "http://127.0.0.1",
                    "X-CSRF-Token": body["csrfToken"],
                },
            )
            assert logout.status_code == 204
            assert runtime.sessions.session_count == 0
            assert cleaned == [owner_before]
            assert fake.revocations == ["refresh-2", "access-2"]
    finally:
        asyncio.run(provider_client.aclose())


def test_oidc_callback_rejects_invalid_state_and_confined_redirect() -> None:
    runtime, _fake, provider_client = _oidc_runtime()
    app = _runtime_app(runtime)
    try:
        with _wrapped_client(runtime, app) as client:
            invalid_login = client.get("/api/auth/oidc/login?return_to=https://evil.invalid")
            assert invalid_login.status_code == 400
            response = client.get(
                "/api/auth/oidc/callback",
                params={"state": "unknown", "code": "one-time-code"},
            )
            assert response.status_code == 401
    finally:
        asyncio.run(provider_client.aclose())


def test_oidc_callback_cannot_complete_another_browsers_login() -> None:
    runtime, fake, provider_client = _oidc_runtime()
    app = _runtime_app(runtime)
    try:
        with (
            _wrapped_client(runtime, app) as initiator,
            _wrapped_client(runtime, app) as other_browser,
        ):
            login = initiator.get("/api/auth/oidc/login", follow_redirects=False)
            query = parse_qs(urlsplit(login.headers["location"]).query)
            state = query["state"][0]
            correlation = runtime.sessions._login_correlations[state]
            fake.expected_nonce = correlation.nonce
            fake.expected_verifier = correlation.pkce_verifier

            callback = other_browser.get(
                "/api/auth/oidc/callback",
                params={"state": state, "code": "one-time-code"},
                follow_redirects=False,
            )

            assert callback.status_code == 401
            assert callback.json()["error"]["code"] == "authentication_failed"
            assert fake.token_calls == []
            assert runtime.sessions.session_count == 0
            assert other_browser.cookies.get("test_session") is None
            assert other_browser.get("/api/auth/session").json()["authenticated"] is False
            replay = initiator.get(
                "/api/auth/oidc/callback",
                params={"state": state, "code": "one-time-code"},
                follow_redirects=False,
            )
            assert replay.status_code == 401
            assert fake.token_calls == []
    finally:
        asyncio.run(provider_client.aclose())


@pytest.mark.parametrize(
    "binding_case",
    [
        "missing",
        "wrong",
        "state",
        "nonce",
        "verifier",
        "duplicate",
        "duplicate_headers",
        "non_ascii",
    ],
)
def test_oidc_callback_rejects_invalid_browser_binding_before_token_exchange(
    binding_case: str,
) -> None:
    runtime, fake, provider_client = _oidc_runtime()
    app = _runtime_app(runtime)
    try:
        with _wrapped_client(runtime, app) as client:
            correlation = _begin_oidc_login(client, runtime, fake)
            bindings = {
                "missing": "",
                "wrong": "wrong-browser",
                "state": correlation.state,
                "nonce": correlation.nonce,
                "verifier": correlation.pkce_verifier,
                "duplicate": (
                    f"{correlation.browser_binding}; "
                    f"test_session_login={correlation.browser_binding}"
                ),
                "duplicate_headers": correlation.browser_binding,
                "non_ascii": "\u00e9",
            }
            headers = [
                (b"cookie", f"test_session_login={bindings[binding_case]}".encode("latin-1"))
            ]
            if binding_case == "duplicate_headers":
                headers += headers

            callback = client.get(
                "/api/auth/oidc/callback",
                params={
                    "state": correlation.state,
                    "code": "one-time-code",
                    "browser_binding": correlation.browser_binding,
                },
                headers=headers,
                follow_redirects=False,
            )

            assert callback.status_code == 401
            assert callback.json()["error"]["code"] == "authentication_failed"
            assert fake.token_calls == []
            assert runtime.sessions.login_count == 0
            assert runtime.sessions.session_count == 0
            assert client.cookies.get("test_session") is None
    finally:
        asyncio.run(provider_client.aclose())


def test_oidc_callback_rejects_another_browsers_valid_binding_without_clearing_its_login() -> None:
    runtime, fake, provider_client = _oidc_runtime()
    app = _runtime_app(runtime)
    try:
        with (
            _wrapped_client(runtime, app) as initiator,
            _wrapped_client(runtime, app) as other_browser,
        ):
            original = _begin_oidc_login(initiator, runtime, fake)
            other = _begin_oidc_login(other_browser, runtime, fake)

            injected = other_browser.get(
                "/api/auth/oidc/callback",
                params={"state": original.state, "code": "one-time-code"},
                follow_redirects=False,
            )

            assert injected.status_code == 401
            assert fake.token_calls == []
            assert runtime.sessions.session_count == 0
            assert other_browser.cookies.get("test_session_login") == other.browser_binding
            legitimate = other_browser.get(
                "/api/auth/oidc/callback",
                params={"state": other.state, "code": "one-time-code"},
                follow_redirects=False,
            )
            assert legitimate.status_code == 303
            assert len(fake.token_calls) == 1
            assert runtime.sessions.session_count == 1
    finally:
        asyncio.run(provider_client.aclose())


def test_oidc_login_cookie_is_secure_host_only_and_distinct_from_application_cookie() -> None:
    runtime, _fake, provider_client = _oidc_runtime()
    settings = runtime.settings.oidc
    assert settings is not None
    runtime.settings = replace(runtime.settings, oidc=replace(settings, cookie_secure=True))
    try:
        with _wrapped_client(runtime, _runtime_app(runtime)) as client:
            login = client.get("/api/auth/oidc/login", follow_redirects=False)

            cookie = login.headers["set-cookie"]
            assert cookie.startswith("__Host-test_session_login=")
            assert "Secure" in cookie
            assert "HttpOnly" in cookie
            assert "Path=/" in cookie
            assert "SameSite=lax" in cookie
            assert "Max-Age=600" in cookie
            assert "Domain=" not in cookie
            correlation = next(iter(runtime.sessions._login_correlations.values()))
            assert correlation.browser_binding not in login.headers["location"]
            assert correlation.state not in cookie
            assert correlation.nonce not in cookie
            assert correlation.pkce_verifier not in cookie
    finally:
        asyncio.run(provider_client.aclose())


@pytest.mark.parametrize("callback_case", ["provider_error", "missing_code", "invalid_token"])
def test_bound_oidc_callback_failures_clear_login_cookie_and_cannot_replay(
    callback_case: str,
) -> None:
    runtime, fake, provider_client = _oidc_runtime()
    try:
        with _wrapped_client(runtime, _runtime_app(runtime)) as client:
            correlation = _begin_oidc_login(client, runtime, fake)
            params = {"state": correlation.state}
            if callback_case == "provider_error":
                params["error"] = "access_denied"
            elif callback_case == "invalid_token":
                params["code"] = "one-time-code"
                fake.expected_nonce = "different-provider-nonce"

            callback = client.get("/api/auth/oidc/callback", params=params, follow_redirects=False)

            assert callback.status_code == (400 if callback_case == "missing_code" else 401)
            assert "Max-Age=0" in callback.headers["set-cookie"]
            assert client.cookies.get("test_session_login") is None
            assert client.cookies.get("test_session") is None
            assert runtime.sessions.login_count == 0
            assert runtime.sessions.session_count == 0
            calls_before = len(fake.token_calls)
            replay = client.get(
                "/api/auth/oidc/callback",
                params={"state": correlation.state, "code": "one-time-code"},
                follow_redirects=False,
            )
            assert replay.status_code == 401
            assert len(fake.token_calls) == calls_before
    finally:
        asyncio.run(provider_client.aclose())


def test_latest_oidc_login_replaces_binding_without_unbound_callback_clearing_it() -> None:
    runtime, fake, provider_client = _oidc_runtime()
    try:
        with _wrapped_client(runtime, _runtime_app(runtime)) as client:
            first = _begin_oidc_login(client, runtime, fake)
            latest = _begin_oidc_login(client, runtime, fake)
            assert first.browser_binding != latest.browser_binding

            stale_callback = client.get(
                "/api/auth/oidc/callback",
                params={"state": first.state, "code": "one-time-code"},
                follow_redirects=False,
            )

            assert stale_callback.status_code == 401
            assert fake.token_calls == []
            assert client.cookies.get("test_session_login") == latest.browser_binding
            latest_callback = client.get(
                "/api/auth/oidc/callback",
                params={"state": latest.state, "code": "one-time-code"},
                follow_redirects=False,
            )
            assert latest_callback.status_code == 303
            assert len(fake.token_calls) == 1
            assert client.cookies.get("test_session_login") is None
            assert runtime.sessions.session_count == 1
    finally:
        asyncio.run(provider_client.aclose())


def test_expired_oidc_login_cannot_be_completed_even_with_its_binding_cookie() -> None:
    runtime, fake, provider_client = _oidc_runtime()
    try:
        with _wrapped_client(runtime, _runtime_app(runtime)) as client:
            correlation = _begin_oidc_login(client, runtime, fake)
            fake.now += timedelta(minutes=11)

            callback = client.get(
                "/api/auth/oidc/callback",
                params={"state": correlation.state, "code": "one-time-code"},
                follow_redirects=False,
            )

            assert callback.status_code == 401
            assert fake.token_calls == []
            assert runtime.sessions.login_count == 0
            assert runtime.sessions.session_count == 0
    finally:
        asyncio.run(provider_client.aclose())


def test_bound_oidc_callback_at_capacity_clears_binding_and_preserves_existing_session() -> None:
    runtime, fake, provider_client = _oidc_runtime()
    runtime.sessions = AuthSessionRepository(session_limit=1, clock=lambda: fake.now)
    try:
        with _wrapped_client(runtime, _runtime_app(runtime)) as client:
            existing, _body, _state = _complete_oidc_login(client, runtime, fake)
            correlation = _begin_oidc_login(client, runtime, fake)

            callback = client.get(
                "/api/auth/oidc/callback",
                params={"state": correlation.state, "code": "one-time-code"},
                follow_redirects=False,
            )

            assert callback.status_code == 503
            assert callback.json()["error"]["code"] == "authentication_capacity_unavailable"
            assert "Max-Age=0" in callback.headers["set-cookie"]
            assert client.cookies.get("test_session_login") is None
            assert client.cookies.get("test_session") == existing.lookup_token
            assert runtime.sessions.session_count == 1
            assert runtime.sessions.login_count == 0
    finally:
        asyncio.run(provider_client.aclose())


def _invalidate_provider_material(fake: FakeOidcProvider, failure: str, *, refresh: bool) -> None:
    fake.claim_overrides["private"] = "provider-payload-secret"
    if failure == "non_finite_date":
        fake.claim_overrides["iat" if refresh else "exp"] = float("nan")
    elif failure == "relative_expiry_overflow":
        if refresh:
            fake.refresh_token_overrides["refresh_expires_in"] = 10**30
        else:
            fake.initial_token_overrides["expires_in"] = 10**30
    elif failure.startswith("non_ascii_"):
        fake.claim_overrides[failure.removeprefix("non_ascii_")] = "é"
    elif refresh:
        fake.refresh_token_body = MALFORMED_PROVIDER_JSON[failure]
    else:
        fake.initial_token_body = MALFORMED_PROVIDER_JSON[failure]


@pytest.mark.parametrize(
    "failure",
    [
        "non_finite_date",
        "relative_expiry_overflow",
        "non_ascii_at_hash",
        "non_ascii_c_hash",
        *MALFORMED_PROVIDER_JSON,
    ],
)
def test_oidc_invalid_provider_material_returns_safe_callback_error_and_clears_binding(
    failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    runtime, fake, provider_client = _oidc_runtime()
    try:
        with _wrapped_client(runtime, _runtime_app(runtime)) as client:
            correlation = _begin_oidc_login(client, runtime, fake)
            _invalidate_provider_material(fake, failure, refresh=False)
            id_token = str(fake._initial_tokens()["id_token"])

            callback = client.get(
                "/api/auth/oidc/callback",
                params={"state": correlation.state, "code": "one-time-code"},
                follow_redirects=False,
            )

            assert callback.status_code == 401
            assert callback.json() == {
                "error": {
                    "code": "authentication_failed",
                    "message": "Authentication could not be completed.",
                    "issues": [],
                }
            }
            assert "Max-Age=0" in callback.headers["set-cookie"]
            assert client.cookies.get("test_session_login") is None
            assert client.cookies.get("test_session") is None
            assert runtime.sessions.login_count == 0
            assert runtime.sessions.session_count == 0
            for value in (
                fake.client_secret,
                "provider-payload-secret",
                "access-1",
                "refresh-1",
                id_token,
            ):
                assert value not in callback.text
                assert value not in caplog.text
            assert all(record.exc_info is None for record in caplog.records)

            latest = _begin_oidc_login(client, runtime, fake)
            calls_before = len(fake.token_calls)
            replay = client.get(
                "/api/auth/oidc/callback",
                params={"state": correlation.state, "code": "one-time-code"},
                follow_redirects=False,
            )
            assert replay.status_code == 401
            assert len(fake.token_calls) == calls_before
            assert client.cookies.get("test_session_login") == latest.browser_binding
    finally:
        asyncio.run(provider_client.aclose())


@pytest.mark.parametrize(
    "failure",
    ["non_finite_date", "relative_expiry_overflow", "non_ascii_at_hash", *MALFORMED_PROVIDER_JSON],
)
def test_oidc_invalid_provider_refresh_returns_safe_401_and_expires_application_cookie(
    failure: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    runtime, fake, provider_client = _oidc_runtime()
    cleaned: list[OwnerContext] = []

    async def cleanup(
        owner: OwnerContext,
        *,
        finalizer: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        cleaned.append(owner)
        if finalizer is not None:
            await finalizer()

    monkeypatch.setattr(auth_router, "cleanup_owner_session", cleanup)
    try:
        with _wrapped_client(runtime, _runtime_app(runtime)) as client:
            correlation = _begin_oidc_login(client, runtime, fake)
            callback = client.get(
                "/api/auth/oidc/callback",
                params={"state": correlation.state, "code": "one-time-code"},
                follow_redirects=False,
            )
            assert callback.status_code == 303
            cookie = client.cookies.get("test_session")
            assert cookie is not None
            _invalidate_provider_material(fake, failure, refresh=True)
            id_token = str(fake._refresh_tokens()["id_token"])

            refresh = client.get("/api/auth/session")

            assert refresh.status_code == 401
            assert refresh.json() == {
                "error": {
                    "code": "unauthorized",
                    "message": "Authentication is required.",
                    "issues": [],
                }
            }
            assert "Max-Age=0" in refresh.headers["set-cookie"]
            assert client.cookies.get("test_session") is None
            assert runtime.sessions.get_session(cookie) is None
            assert runtime.sessions.session_count == 0
            assert cleaned == []
            assert fake.revocations == []
            for value in (
                fake.client_secret,
                "provider-payload-secret",
                "access-2",
                "refresh-2",
                id_token,
            ):
                assert value not in refresh.text
                assert value not in caplog.text
            assert all(record.exc_info is None for record in caplog.records)
    finally:
        asyncio.run(provider_client.aclose())


def test_oidc_absolute_expiry_rejects_and_clears_the_opaque_cookie() -> None:
    runtime, fake, provider_client = _oidc_runtime()
    app = _runtime_app(runtime)
    try:
        with _wrapped_client(runtime, app) as client:
            _complete_oidc_login(client, runtime, fake)
            fake.now += timedelta(hours=2)

            expired = client.get("/api/auth/session")

            assert expired.status_code == 401
            assert "Max-Age=0" in expired.headers["set-cookie"]
            assert runtime.sessions.session_count == 0
    finally:
        asyncio.run(provider_client.aclose())


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_current_refresh_expiry_returns_safe_401_without_owner_resource_cleanup(
    method: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, fake, provider_client = _oidc_runtime()
    fake.refresh_token_overrides.update({"expires_in": 3600, "refresh_expires_in": 1})
    cleaned: list[OwnerContext] = []
    owner: OwnerContext | None = None

    async def cleanup(
        owner_context: OwnerContext,
        *,
        finalizer: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        cleaned.append(owner_context)
        delete_owner_session_analyses(owner_context)
        results.delete(run_id)
        if finalizer is not None:
            await finalizer()

    monkeypatch.setattr(auth_router, "cleanup_owner_session", cleanup)
    results = ResultRepository(tmp_path, 10_000)
    run_id = str(uuid4())
    result = b'{"retained":"analysis-result"}'
    spec = results.prepare(run_id)
    assert results.reserve(run_id, len(result))
    spec.committed_path.write_bytes(result)
    results.commit(run_id)
    try:
        with _wrapped_client(runtime, _runtime_app(runtime)) as client:
            session, bootstrap, _state = _complete_oidc_login(client, runtime, fake)
            owner = session.owner_context
            snapshot = create_analysis_snapshot(
                ClusterTopology(
                    exchanges=frozenset(),
                    queues=frozenset(),
                    shovels=frozenset(),
                    edges=frozenset(),
                ),
                SnapshotMetadata(
                    analysis_id="retained-capture",
                    source=AnalysisSource.SESSION,
                    label="Retained capture",
                    capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
                    snapshot_revision="sha256:" + "a" * 64,
                    cluster_tags={},
                    in_cluster_amqp_hosts=(),
                    vhost_count=0,
                ),
            )
            publish_session_analysis(owner, snapshot)
            fake.now += timedelta(microseconds=999_999)
            before = client.get("/api/auth/session")
            assert before.status_code == 200
            current = runtime.sessions.get_session(session.lookup_token)
            assert current is not None
            assert current.owner_context == owner

            fake.now += timedelta(microseconds=1)
            expired = client.request(
                method,
                "/api/auth/session" if method == "GET" else "/api/definitions/upload",
                headers={
                    "Origin": "http://127.0.0.1",
                    "X-CSRF-Token": str(bootstrap["csrfToken"]),
                },
                content=b"untrusted multipart body" if method == "POST" else None,
            )

            assert expired.status_code == 401
            assert expired.json() == {
                "error": {
                    "code": "unauthorized",
                    "message": "Authentication is required.",
                    "issues": [],
                },
            }
            assert "Max-Age=0" in expired.headers["set-cookie"]
            assert client.cookies.get("test_session") is None
            assert runtime.sessions.get_session(session.lookup_token) is None
            assert cleaned == []
            assert fake.revocations == []
            assert [entry.snapshot for entry in list_session_analyses(owner)] == [snapshot]
            assert results.read(run_id) == result
    finally:
        if owner is not None:
            delete_owner_session_analyses(owner)
        asyncio.run(provider_client.aclose())


def test_basic_bootstrap_challenge_and_exact_owner_logout(monkeypatch: pytest.MonkeyPatch) -> None:
    password = "correct horse battery staple"
    password_hash = PasswordHasher(
        time_cost=3,
        memory_cost=65_536,
        parallelism=1,
        hash_len=32,
        salt_len=16,
    ).hash(password)
    basic = BasicSettings(
        users_file=_settings().client_secret_file,
        realm="Topology Test",
        users={
            "analyst": BasicUser(
                username="analyst",
                password_hashes=(password_hash,),
                roles=frozenset({AppRole.ANALYST}),
            )
        },
    )
    runtime = AuthRuntime(
        settings=AuthSettings(
            mode=AuthMode.BASIC,
            deployment_mode=DeploymentMode.DEVELOPMENT,
            public_origin="http://127.0.0.1",
            oidc=None,
            basic=basic,
        ),
        sessions=AuthSessionRepository(),
        basic_authenticator=BasicAuthenticator(basic),
    )
    cleaned: list[OwnerContext] = []

    async def cleanup(owner: OwnerContext) -> None:
        cleaned.append(owner)

    monkeypatch.setattr(auth_router, "cleanup_owner_session", cleanup)
    app = _runtime_app(runtime)
    with _wrapped_client(runtime, app) as client:
        anonymous = client.get("/api/auth/session")
        assert anonymous.status_code == 200
        assert anonymous.json()["authenticated"] is False

        invalid = client.get("/api/auth/session", auth=("analyst", "wrong"))
        assert invalid.status_code == 401
        assert invalid.headers["www-authenticate"] == (
            'Basic realm="Topology Test", charset="UTF-8"'
        )

        established = client.get("/api/auth/session", auth=("analyst", password))
        assert established.status_code == 200
        assert established.json()["logoutPath"] == "/api/auth/logout"

        session_id = str(uuid4())
        logout = client.post(
            "/api/auth/logout",
            auth=("analyst", password),
            headers={"X-Session-Token": session_id},
        )
        assert logout.status_code == 204
        assert logout.headers["clear-site-data"] == '"cache", "cookies", "storage"'
        assert len(cleaned) == 1
        assert cleaned[0].session_id == session_id

        malformed = client.post(
            "/api/auth/logout",
            auth=("analyst", password),
            headers={"X-Session-Token": "not-a-uuid"},
        )
        assert malformed.status_code == 400


def test_none_bootstrap_preserves_synthetic_authenticated_session() -> None:
    settings = AuthSettings(
        mode=AuthMode.NONE,
        deployment_mode=DeploymentMode.DEVELOPMENT,
        public_origin=None,
        oidc=None,
        basic=None,
    )
    runtime = AuthRuntime(settings=settings, sessions=AuthSessionRepository())
    app = _runtime_app(runtime)
    with _wrapped_client(runtime, app) as client:
        response = client.get("/api/auth/session")
    assert response.json() == {
        "authMode": "none",
        "authenticated": True,
        "loginPath": None,
        "logoutPath": None,
        "csrfToken": None,
    }

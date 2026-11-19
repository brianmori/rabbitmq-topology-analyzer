import asyncio
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, Request

from auth.activity import OwnerActivityRegistry
from auth.basic import BasicAuthenticator
from auth.config import AuthSettings, _load_public_origin
from auth.middleware import (
    AuthenticatedRequest,
    AuthenticationRejected,
    PreBodyAuthMiddleware,
    RequestAuthenticator,
)
from auth.runtime import AuthRuntime
from auth.sessions import AuthSessionRepository
from core.deployment import DeploymentMode
from core.errors import register_error_handlers
from core.security import AppRole, AuthMode, Principal, configure_authentication_dependencies
from core.session import OwnerContext, get_optional_owner_context, require_owner_context
from definitions.router import router as definitions_router
from public_origin_cases import PUBLIC_ORIGIN_CASES
from test_basic_auth import RecordingPasswordHasher, _authorization, _settings, _user
from test_oidc import FakeOidcProvider, _establish_provider_session, _provider

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

PUBLIC_ORIGIN = "https://topology.example.test"
COOKIE_NAME = "test_topology_session"
CSRF_TOKEN = "csrf-session-secret"
COOKIE_VALUE = "opaque-session-lookup"
_BASIC_CREDENTIAL = _authorization("analyst", "correct-password")


@dataclass(slots=True)
class ProbeApp:
    calls: int = 0
    receive_calls: int = 0
    states: list[dict[str, object]] = field(default_factory=list)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        self.states.append(dict(scope.get("state", {})))
        if scope.get("method") in {"POST", "PUT", "PATCH", "DELETE"}:
            await receive()
            self.receive_calls += 1
        body = b'{"downstream":true}'
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            },
        )
        await send({"type": "http.response.body", "body": body})


@dataclass(frozen=True, slots=True)
class AsgiResponse:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    receive_calls: int

    def header(self, name: str) -> str | None:
        expected = name.lower().encode("ascii")
        for header_name, value in self.headers:
            if header_name.lower() == expected:
                return value.decode("latin-1")
        return None


class StaticAuthenticator:
    def __init__(self, result: AuthenticatedRequest | None) -> None:
        self._result = result
        self.credentials: list[str] = []

    async def __call__(self, credential: str, _scope: Scope) -> AuthenticatedRequest | None:
        self.credentials.append(credential)
        return self._result


class RejectingAuthenticator:
    async def __call__(self, _credential: str, _scope: Scope) -> AuthenticatedRequest:
        raise AuthenticationRejected


def _authentication(mode: AuthMode, *roles: AppRole) -> AuthenticatedRequest:
    owner = OwnerContext(principal_id="test-principal", session_id="test-app-session")
    return AuthenticatedRequest(
        principal=Principal(
            principal_id=owner.principal_id,
            roles=frozenset(roles),
            auth_mode=mode,
        ),
        owner_context=owner if mode is AuthMode.OIDC else None,
        csrf_token=CSRF_TOKEN if mode is AuthMode.OIDC else None,
    )


def _headers(**values: str) -> tuple[tuple[bytes, bytes], ...]:
    return tuple(
        (name.replace("_", "-").encode("ascii"), value.encode("latin-1"))
        for name, value in values.items()
    )


def _request(
    app: ASGIApp,
    *,
    method: str,
    path: str,
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> AsgiResponse:
    messages: list[Message] = []
    receive_calls = 0

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        return {
            "type": "http.request",
            "body": b"untrusted multipart body",
            "more_body": False,
        }

    async def send(message: Message) -> None:
        messages.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 12345),
        "server": ("topology.example.test", 443),
        "root_path": "",
        "state": {},
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    bodies = [
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    ]
    return AsgiResponse(
        status=start["status"],
        headers=tuple(start.get("headers", ())),
        body=b"".join(bodies),
        receive_calls=receive_calls,
    )


def _oidc_middleware(
    downstream: ProbeApp,
    authenticator: RequestAuthenticator,
) -> PreBodyAuthMiddleware:
    return PreBodyAuthMiddleware(
        downstream,
        mode=AuthMode.OIDC,
        authenticator=authenticator,
        public_origin=PUBLIC_ORIGIN,
        oidc_cookie_name=COOKIE_NAME,
    )


def _basic_runtime() -> AuthRuntime:
    hasher = RecordingPasswordHasher()
    basic = _settings(_user("analyst", (hasher.hash("correct-password"),), AppRole.ANALYST))
    return AuthRuntime(
        settings=AuthSettings(
            mode=AuthMode.BASIC,
            deployment_mode=DeploymentMode.DEVELOPMENT,
            public_origin=PUBLIC_ORIGIN,
            oidc=None,
            basic=basic,
        ),
        sessions=AuthSessionRepository(),
        basic_authenticator=BasicAuthenticator(basic, password_hasher=hasher),
    )


@pytest.mark.parametrize("mode", [AuthMode.NONE, AuthMode.BASIC, AuthMode.OIDC])
@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_session_headers_cannot_create_untracked_owner_work(
    mode: AuthMode,
    conflicting: bool,
) -> None:
    configure_authentication_dependencies(AuthMode.NONE)
    downstream = FastAPI()
    register_error_handlers(downstream)
    registry = OwnerActivityRegistry()
    calls: list[OwnerContext] = []
    tracked: list[bool] = []

    @downstream.post("/api/owner")
    async def mutate_owner(
        request: Request,
        owner: Annotated[OwnerContext, Depends(require_owner_context)],
    ) -> dict[str, bool]:
        calls.append(owner)
        tracked.append(
            request.state.owner_context == owner
            and asyncio.current_task() in registry._tasks.get(owner, set())
            if mode is not AuthMode.NONE
            else False
        )
        await request.body()
        return {"ok": True}

    runtime = _basic_runtime()
    authentication = _authentication(AuthMode.OIDC, AppRole.ANALYST)
    middleware = PreBodyAuthMiddleware(
        downstream,
        mode=mode,
        authenticator=(
            runtime.authenticate if mode is AuthMode.BASIC else StaticAuthenticator(authentication)
        ),
        public_origin=PUBLIC_ORIGIN,
        oidc_cookie_name=COOKIE_NAME,
        activity_registry=registry,
    )
    first = str(uuid4())
    second = str(uuid4()) if conflicting else first
    try:
        response = _request(
            middleware,
            method="POST",
            path="/api/owner",
            headers=(
                *_headers(
                    authorization=_BASIC_CREDENTIAL,
                    cookie=f"{COOKIE_NAME}={COOKIE_VALUE}",
                    origin=PUBLIC_ORIGIN,
                    x_csrf_token=CSRF_TOKEN,
                ),
                (b"x-session-token", first.encode("ascii")),
                (b"x-session-token", second.encode("ascii")),
            ),
        )
    finally:
        assert runtime.basic_authenticator is not None
        asyncio.run(runtime.basic_authenticator.close())

    if mode is AuthMode.OIDC:
        assert response.status == 200
        assert calls == [authentication.owner_context]
        assert tracked == [True]
    else:
        assert response.status == 400
        assert json.loads(response.body) == {
            "error": {
                "code": "invalid_session_token",
                "message": "The browser-session identifier must be a canonical UUIDv4.",
                "issues": [],
            },
        }
        assert calls == []
        assert response.receive_calls == 0
        assert first not in response.body.decode()
        assert second not in response.body.decode()
    assert registry._tasks == {}


@pytest.mark.parametrize("header", [None, "canonical", "uppercase"])
def test_basic_optional_owner_matches_activity_gate_for_every_accepted_request(
    header: str | None,
) -> None:
    runtime = _basic_runtime()
    downstream = FastAPI()
    registry = OwnerActivityRegistry()
    session_id = str(uuid4())
    owners: list[OwnerContext | None] = []

    @downstream.get("/api/optional-owner")
    async def optional_owner(
        request: Request,
        owner: Annotated[OwnerContext | None, Depends(get_optional_owner_context)],
    ) -> dict[str, bool]:
        owners.append(owner)
        assert request.state.owner_context == owner
        if owner is not None:
            assert owner.session_id == session_id
            assert asyncio.current_task() in registry._tasks[owner]
        return {"hasOwner": owner is not None}

    headers = _headers(authorization=_BASIC_CREDENTIAL)
    if header is not None:
        value = session_id.upper() if header == "uppercase" else session_id
        headers += _headers(x_session_token=value)
    try:
        response = _request(
            PreBodyAuthMiddleware(
                downstream,
                mode=AuthMode.BASIC,
                authenticator=runtime.authenticate,
                activity_registry=registry,
            ),
            method="GET",
            path="/api/optional-owner",
            headers=headers,
        )
        assert response.status == 200
        assert json.loads(response.body) == {"hasOwner": header is not None}
        assert len(owners) == 1
        assert registry._tasks == {}
    finally:
        assert runtime.basic_authenticator is not None
        asyncio.run(runtime.basic_authenticator.close())


@pytest.mark.parametrize("conflicting", [False, True])
@pytest.mark.parametrize("valid_credential", [False, True])
def test_basic_duplicate_headers_cannot_reach_upload_body_parsing(
    conflicting: bool,
    valid_credential: bool,
) -> None:
    runtime = _basic_runtime()
    downstream = FastAPI()
    register_error_handlers(downstream)
    downstream.include_router(definitions_router, prefix="/api")
    token = str(uuid4())
    other_token = str(uuid4()) if conflicting else token
    credential = _BASIC_CREDENTIAL if valid_credential else _authorization("analyst", "wrong")
    try:
        response = _request(
            PreBodyAuthMiddleware(
                downstream,
                mode=AuthMode.BASIC,
                authenticator=runtime.authenticate,
            ),
            method="POST",
            path="/api/definitions/upload",
            headers=(
                *_headers(
                    authorization=credential,
                    content_type="multipart/form-data; boundary=untrusted",
                ),
                (b"x-session-token", token.encode("ascii")),
                (b"x-session-token", other_token.encode("ascii")),
            ),
        )
        assert response.status == (400 if valid_credential else 401)
        assert json.loads(response.body)["error"]["code"] == (
            "invalid_session_token" if valid_credential else "unauthorized"
        )
        assert response.receive_calls == 0
        assert (response.header("www-authenticate") is None) is valid_credential
    finally:
        assert runtime.basic_authenticator is not None
        asyncio.run(runtime.basic_authenticator.close())


def test_none_mode_passes_through_without_auth_or_csrf_body_preflight() -> None:
    downstream = ProbeApp()
    middleware = PreBodyAuthMiddleware(downstream, mode=AuthMode.NONE)

    response = _request(
        middleware,
        method="POST",
        path="/api/definitions/upload",
    )

    assert response.status == 200
    assert response.receive_calls == 1
    assert downstream.calls == 1
    assert downstream.receive_calls == 1


def test_expired_refreshed_oidc_credentials_reject_unsafe_request_without_body_reads() -> None:
    fake = FakeOidcProvider(
        refresh_token_overrides={"expires_in": 3600, "refresh_expires_in": 1},
    )
    provider, client = _provider(fake)
    repository = AuthSessionRepository(clock=lambda: fake.now)

    async def establish() -> str:
        await provider.start()
        session = await _establish_provider_session(provider, repository)
        assert await provider.authenticate_session(repository, session.lookup_token) is not None
        return session.lookup_token

    async def authenticate(credential: str, _scope: Scope) -> AuthenticatedRequest | None:
        return await provider.authenticate_session(repository, credential)

    try:
        lookup_token = asyncio.run(establish())
        fake.now += timedelta(seconds=1)
        downstream = ProbeApp()
        middleware = _oidc_middleware(downstream, authenticate)

        response = _request(
            middleware,
            method="POST",
            path="/api/definitions/upload",
            headers=_headers(cookie=f"{COOKIE_NAME}={lookup_token}"),
        )

        assert response.status == 401
        assert json.loads(response.body)["error"]["code"] == "unauthorized"
        assert "Max-Age=0" in (response.header("set-cookie") or "")
        assert response.receive_calls == 0
        assert downstream.calls == 0
        assert repository.session_count == 0
    finally:
        asyncio.run(client.aclose())


def test_oidc_product_request_without_cookie_is_rejected_without_body_read() -> None:
    downstream = ProbeApp()
    middleware = _oidc_middleware(
        downstream,
        StaticAuthenticator(_authentication(AuthMode.OIDC, AppRole.ANALYST)),
    )

    response = _request(
        middleware,
        method="POST",
        path="/api/scc-runs",
        headers=_headers(origin=PUBLIC_ORIGIN, x_csrf_token=CSRF_TOKEN),
    )

    assert response.status == 401
    assert response.receive_calls == 0
    assert downstream.calls == 0
    assert response.header("Cache-Control") == "no-store"
    assert response.header("WWW-Authenticate") is None
    assert response.header("Set-Cookie") == (
        f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax; Secure"
    )
    assert json.loads(response.body) == {
        "error": {
            "code": "unauthorized",
            "message": "Authentication is required.",
            "issues": [],
        },
    }


@pytest.mark.parametrize(
    ("origin", "csrf"),
    [
        (None, CSRF_TOKEN),
        ("https://attacker.example.test", CSRF_TOKEN),
        (f"{PUBLIC_ORIGIN}.attacker.test", CSRF_TOKEN),
        ("http://topology.example.test", CSRF_TOKEN),
        (f"{PUBLIC_ORIGIN}:8443", CSRF_TOKEN),
        ("null", CSRF_TOKEN),
        (f"{PUBLIC_ORIGIN}/", CSRF_TOKEN),
        (f"{PUBLIC_ORIGIN}, {PUBLIC_ORIGIN}", CSRF_TOKEN),
        (PUBLIC_ORIGIN, None),
        (PUBLIC_ORIGIN, "wrong-csrf"),
    ],
)
def test_oidc_unsafe_request_rejects_nonexact_origin_or_csrf_before_body(
    origin: str | None,
    csrf: str | None,
) -> None:
    downstream = ProbeApp()
    authenticator = StaticAuthenticator(_authentication(AuthMode.OIDC, AppRole.ANALYST))
    middleware = _oidc_middleware(downstream, authenticator)
    values = {"cookie": f"{COOKIE_NAME}={COOKIE_VALUE}"}
    if origin is not None:
        values["origin"] = origin
    if csrf is not None:
        values["x_csrf_token"] = csrf

    response = _request(
        middleware,
        method="POST",
        path="/api/scc-runs",
        headers=_headers(**values),
    )

    assert response.status == 403
    assert response.receive_calls == 0
    assert downstream.calls == 0
    assert response.header("Cache-Control") == "no-store"
    assert response.header("Set-Cookie") is None
    assert authenticator.credentials == [COOKIE_VALUE]


def test_oidc_upload_csrf_rejection_happens_before_multipart_receive() -> None:
    downstream = ProbeApp()
    middleware = _oidc_middleware(
        downstream,
        StaticAuthenticator(_authentication(AuthMode.OIDC, AppRole.ANALYST)),
    )

    response = _request(
        middleware,
        method="POST",
        path="/api/definitions/upload",
        headers=_headers(
            cookie=f"{COOKIE_NAME}={COOKIE_VALUE}",
            origin="https://attacker.example.test",
            x_csrf_token=CSRF_TOKEN,
            content_type="multipart/form-data; boundary=untrusted",
        ),
    )

    assert response.status == 403
    assert response.receive_calls == 0
    assert downstream.calls == 0


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    ("header", "value"),
    [
        (b"origin", b"https://topology.example.test/\xff"),
        (b"origin", b"https://topology.example.test/\xc3\xa9"),
        (b"x-csrf-token", b"csrf-session-secret-\xff"),
        (b"x-csrf-token", b"csrf-session-secret-\xc3\xa9"),
    ],
    ids=["origin-latin1", "origin-utf8", "csrf-latin1", "csrf-utf8"],
)
def test_malformed_csrf_headers_return_safe_403_without_reading_multipart(
    method: str,
    header: bytes,
    value: bytes,
) -> None:
    downstream = ProbeApp()
    authenticator = StaticAuthenticator(_authentication(AuthMode.OIDC, AppRole.ANALYST))
    middleware = _oidc_middleware(downstream, authenticator)
    headers = _headers(
        cookie=f"{COOKIE_NAME}={COOKIE_VALUE}",
        origin=PUBLIC_ORIGIN,
        x_csrf_token=CSRF_TOKEN,
        content_type="multipart/form-data; boundary=untrusted",
    )
    headers = tuple((name, value if name == header else original) for name, original in headers)

    response = _request(
        middleware,
        method=method,
        path="/api/definitions/upload",
        headers=headers,
    )

    assert response.status == 403
    assert response.receive_calls == 0
    assert downstream.calls == 0
    assert authenticator.credentials == [COOKIE_VALUE]
    assert response.header("Cache-Control") == "no-store"
    assert response.header("Set-Cookie") is None
    assert json.loads(response.body) == {
        "error": {
            "code": "forbidden",
            "message": "The current principal cannot perform this action.",
            "issues": [],
        }
    }


def test_oidc_exact_origin_and_csrf_establish_state_before_downstream_body_read() -> None:
    downstream = ProbeApp()
    authentication = _authentication(AuthMode.OIDC, AppRole.ANALYST)
    authenticator = StaticAuthenticator(authentication)
    middleware = _oidc_middleware(downstream, authenticator)

    response = _request(
        middleware,
        method="POST",
        path="/api/scc-runs",
        headers=_headers(
            cookie=f"{COOKIE_NAME}={COOKIE_VALUE}",
            origin=PUBLIC_ORIGIN,
            x_csrf_token=CSRF_TOKEN,
        ),
    )

    assert response.status == 200
    assert response.receive_calls == 1
    assert downstream.states == [
        {
            "authentication": authentication,
            "principal": authentication.principal,
            "owner_context": authentication.owner_context,
        },
    ]


@pytest.mark.parametrize(("configured", "browser_origin"), PUBLIC_ORIGIN_CASES)
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/definitions/upload"),
        ("POST", "/api/scc-runs"),
        ("DELETE", "/api/scc-runs/run-id"),
        ("POST", "/api/auth/logout"),
    ],
)
def test_configured_origin_accepts_browser_serialization_before_downstream_body(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
    browser_origin: str,
    method: str,
    path: str,
) -> None:
    monkeypatch.setenv("RABBITMQ_PUBLIC_ORIGIN", configured)
    downstream = ProbeApp()
    middleware = PreBodyAuthMiddleware(
        downstream,
        mode=AuthMode.OIDC,
        authenticator=StaticAuthenticator(_authentication(AuthMode.OIDC, AppRole.ANALYST)),
        public_origin=_load_public_origin(DeploymentMode.DEVELOPMENT),
        oidc_cookie_name=COOKIE_NAME,
    )

    response = _request(
        middleware,
        method=method,
        path=path,
        headers=_headers(
            cookie=f"{COOKIE_NAME}={COOKIE_VALUE}",
            origin=browser_origin,
            x_csrf_token=CSRF_TOKEN,
        ),
    )

    assert response.status == 200
    assert response.receive_calls == 1
    assert downstream.calls == 1


@pytest.mark.parametrize("header", [b"origin", b"x-csrf-token"])
def test_duplicate_csrf_headers_are_rejected_before_body(header: bytes) -> None:
    downstream = ProbeApp()
    middleware = _oidc_middleware(
        downstream,
        StaticAuthenticator(_authentication(AuthMode.OIDC, AppRole.ANALYST)),
    )
    headers = _headers(
        cookie=f"{COOKIE_NAME}={COOKIE_VALUE}",
        origin=PUBLIC_ORIGIN,
        x_csrf_token=CSRF_TOKEN,
    )
    duplicate = next(pair for pair in headers if pair[0] == header)

    response = _request(
        middleware,
        method="POST",
        path="/api/definitions/upload",
        headers=(*headers, duplicate),
    )

    assert response.status == 403
    assert response.receive_calls == 0
    assert downstream.calls == 0


@pytest.mark.parametrize(
    ("configured", "origin"),
    [
        ("https://TOPOLOGY.EXAMPLE:08443", "https://topology.example"),
        ("https://TOPOLOGY.EXAMPLE:08443", "https://topology.example:443"),
        ("https://TOPOLOGY.EXAMPLE:08443", "https://topology.example:9443"),
        ("https://TOPOLOGY.EXAMPLE:08443", "http://topology.example:8443"),
        ("https://TOPOLOGY.EXAMPLE:443", "https://TOPOLOGY.EXAMPLE"),
        ("https://TOPOLOGY.EXAMPLE:443", "https://topology.example:443"),
        ("https://[2001:DB8::1]:8443", "https://[2001:db8::1]"),
        ("https://[2001:DB8::1]:443", "https://[2001:db8::2]"),
    ],
)
def test_origin_canonicalization_preserves_exact_request_policy_before_body(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
    origin: str,
) -> None:
    monkeypatch.setenv("RABBITMQ_PUBLIC_ORIGIN", configured)
    downstream = ProbeApp()
    middleware = PreBodyAuthMiddleware(
        downstream,
        mode=AuthMode.OIDC,
        authenticator=StaticAuthenticator(_authentication(AuthMode.OIDC, AppRole.ANALYST)),
        public_origin=_load_public_origin(DeploymentMode.PRODUCTION),
        oidc_cookie_name=COOKIE_NAME,
    )
    authority = origin.partition("://")[2]

    response = _request(
        middleware,
        method="POST",
        path="/api/definitions/upload",
        headers=_headers(
            cookie=f"{COOKIE_NAME}={COOKIE_VALUE}",
            origin=origin,
            x_csrf_token=CSRF_TOKEN,
            host=authority,
            forwarded=f"host={authority};proto=https",
            x_forwarded_host=authority,
        ),
    )

    assert response.status == 403
    assert response.receive_calls == 0
    assert downstream.calls == 0


def test_viewer_upload_is_rejected_before_multipart_receive() -> None:
    downstream = ProbeApp()
    middleware = _oidc_middleware(
        downstream,
        StaticAuthenticator(_authentication(AuthMode.OIDC, AppRole.VIEWER)),
    )

    response = _request(
        middleware,
        method="POST",
        path="/api/definitions/upload",
        headers=_headers(
            cookie=f"{COOKIE_NAME}={COOKIE_VALUE}",
            origin=PUBLIC_ORIGIN,
            x_csrf_token=CSRF_TOKEN,
            content_type="multipart/form-data; boundary=untrusted",
        ),
    )

    assert response.status == 403
    assert response.receive_calls == 0
    assert downstream.calls == 0
    assert json.loads(response.body)["error"]["code"] == "forbidden"


@pytest.mark.parametrize(
    ("rejection", "expected_status"),
    [("credential", 401), ("csrf", 403), ("viewer", 403)],
)
def test_real_upload_parser_stays_behind_pre_body_authentication(
    rejection: str,
    expected_status: int,
) -> None:
    downstream = FastAPI()
    downstream.include_router(definitions_router, prefix="/api")
    authentication = _authentication(
        AuthMode.OIDC,
        AppRole.VIEWER if rejection == "viewer" else AppRole.ANALYST,
    )
    middleware = PreBodyAuthMiddleware(
        downstream,
        mode=AuthMode.OIDC,
        authenticator=StaticAuthenticator(None if rejection == "credential" else authentication),
        public_origin=PUBLIC_ORIGIN,
        oidc_cookie_name=COOKIE_NAME,
    )

    response = _request(
        middleware,
        method="POST",
        path="/api/definitions/upload",
        headers=_headers(
            cookie=f"{COOKIE_NAME}={COOKIE_VALUE}",
            origin=PUBLIC_ORIGIN,
            x_csrf_token="wrong" if rejection == "csrf" else CSRF_TOKEN,
            content_type="multipart/form-data; boundary=untrusted",
        ),
    )

    assert response.status == expected_status
    assert response.receive_calls == 0


def test_basic_missing_authorization_uses_safe_challenge_without_body_read() -> None:
    downstream = ProbeApp()
    middleware = PreBodyAuthMiddleware(
        downstream,
        mode=AuthMode.BASIC,
        authenticator=StaticAuthenticator(_authentication(AuthMode.BASIC, AppRole.ANALYST)),
        basic_realm="Topology Test",
    )

    response = _request(
        middleware,
        method="POST",
        path="/api/definitions/upload",
    )

    assert response.status == 401
    assert response.receive_calls == 0
    assert downstream.calls == 0
    assert response.header("WWW-Authenticate") == ('Basic realm="Topology Test", charset="UTF-8"')
    assert response.header("Cache-Control") == "no-store"
    assert response.header("Set-Cookie") is None


@pytest.mark.parametrize(
    ("mode", "path"),
    [
        (AuthMode.OIDC, "/api/health"),
        (AuthMode.OIDC, "/api/health/live"),
        (AuthMode.OIDC, "/api/health/ready"),
        (AuthMode.OIDC, "/api/auth/session"),
        (AuthMode.BASIC, "/api/auth/session"),
    ],
)
def test_anonymous_safe_gets_reach_downstream_without_credentials(
    mode: AuthMode,
    path: str,
) -> None:
    downstream = ProbeApp()
    authenticator = StaticAuthenticator(_authentication(mode, AppRole.ANALYST))
    middleware = PreBodyAuthMiddleware(
        downstream,
        mode=mode,
        authenticator=authenticator,
        public_origin=PUBLIC_ORIGIN if mode is AuthMode.OIDC else None,
        oidc_cookie_name=COOKIE_NAME,
    )

    response = _request(middleware, method="GET", path=path)

    assert response.status == 200
    assert downstream.calls == 1
    assert authenticator.credentials == []
    if path == "/api/auth/session":
        assert response.header("Cache-Control") == "no-store"


def test_invalid_supplied_oidc_bootstrap_cookie_is_cleared_and_rejected_safely() -> None:
    downstream = ProbeApp()
    middleware = _oidc_middleware(downstream, RejectingAuthenticator())

    response = _request(
        middleware,
        method="GET",
        path="/api/auth/session",
        headers=_headers(cookie=f"{COOKIE_NAME}=stale"),
    )

    assert response.status == 401
    assert downstream.calls == 0
    assert response.header("Cache-Control") == "no-store"
    assert response.header("Set-Cookie") is not None


def test_duplicate_oidc_cookie_values_are_never_accepted_ambiguously() -> None:
    downstream = ProbeApp()
    middleware = _oidc_middleware(
        downstream,
        StaticAuthenticator(_authentication(AuthMode.OIDC, AppRole.ANALYST)),
    )

    response = _request(
        middleware,
        method="GET",
        path="/api/access",
        headers=_headers(cookie=f"{COOKIE_NAME}=first; {COOKIE_NAME}=second"),
    )

    assert response.status == 401
    assert downstream.calls == 0

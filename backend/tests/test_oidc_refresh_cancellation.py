import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Literal, cast

import httpx2
import pytest
from fastapi import Request  # noqa: TC002 - FastAPI resolves endpoint annotations at runtime.
from fastapi.responses import JSONResponse

from auth.middleware import AuthenticatedRequest, PreBodyAuthMiddleware
from auth.oidc import OidcProvider
from auth.sessions import AuthSessionRepository
from core.security import AppRole, AuthMode
from test_oidc import ISSUER, FakeOidcProvider, _establish_provider_session, _settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.types import Message, Receive, Scope, Send

    from auth.sessions import AuthenticatedSession, ProviderCredentialState
    from core.security import Principal

type Boundary = Literal["before_rotation", "response", "jwks"]
type Failure = Literal["none", "rejected", "timeout", "wrong_subject", "internal"]


class _RotationHarness:
    def __init__(self, boundary: Boundary, failure: Failure) -> None:
        self.fake = FakeOidcProvider(roles=["viewers"], refresh_roles=["analysts"])
        self.boundary = boundary
        self.failure = failure
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.refresh_tokens: list[str] = []
        self.valid_refresh = "refresh-1"
        self.rotations = 0
        self.requests: list[asyncio.Task[AuthenticatedRequest | None]] = []
        self.fake.app.router.routes[:] = [
            route for route in self.fake.app.router.routes if getattr(route, "path", "") != "/token"
        ]
        self.fake.app.post("/token")(self.token)
        self.client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.provider_app),
            base_url=ISSUER,
        )
        self.provider = OidcProvider(_settings(), client=self.client, clock=lambda: self.fake.now)
        self.repository = AuthSessionRepository(clock=lambda: self.fake.now)
        self.session: AuthenticatedSession

    async def barrier(self) -> None:
        self.entered.set()
        await self.release.wait()

    async def token(self, request: Request) -> JSONResponse:
        assert self.fake._valid_client_auth(request)
        form = await request.form()
        if form["grant_type"] == "authorization_code":
            assert form["code"] == "one-time-code"
            assert form["code_verifier"] == self.fake.expected_verifier
            return JSONResponse(self.fake._initial_tokens())
        assert form["grant_type"] == "refresh_token"
        supplied = str(form["refresh_token"])
        self.refresh_tokens.append(supplied)
        if supplied != self.valid_refresh:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if self.boundary == "before_rotation":
            await self.barrier()
        self.valid_refresh = "refresh-2"
        self.rotations += 1
        if self.boundary == "jwks":
            self.fake.active_kid = "kid-b"
            self.fake.published_kids = ["kid-b"]
        if self.boundary == "response":
            await self.barrier()
        if self.failure == "rejected":
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if self.failure == "timeout":
            raise httpx2.ReadTimeout("private provider timeout detail")
        if self.failure == "internal":
            raise RuntimeError("private unexpected provider detail")
        if self.failure == "wrong_subject":
            self.fake.refresh_subject = "another-subject"
        return JSONResponse(self.fake._refresh_tokens())

    async def provider_app(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("path") == "/jwks" and self.rotations and self.boundary == "jwks":
            await self.barrier()
        await self.fake.app(scope, receive, send)

    def authenticate(self) -> asyncio.Task[AuthenticatedRequest | None]:
        task = asyncio.create_task(
            self.provider.authenticate_session(self.repository, self.session.lookup_token),
        )
        self.requests.append(task)
        return task

    async def http_status(self) -> int:
        messages: list[Message] = []

        async def authenticate(cookie: str, _scope: Scope) -> AuthenticatedRequest | None:
            return await self.provider.authenticate_session(self.repository, cookie)

        async def downstream(scope: Scope, _receive: Receive, send: Send) -> None:
            assert scope["state"]["owner_context"] == self.session.owner_context
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive() -> Message:
            pytest.fail("authentication must not read a request body")

        async def send(message: Message) -> None:
            messages.append(message)

        middleware = PreBodyAuthMiddleware(
            downstream,
            mode=AuthMode.OIDC,
            authenticator=authenticate,
            public_origin=ISSUER,
            oidc_cookie_name="test_session",
            oidc_cookie_secure=False,
        )
        await middleware(
            cast(
                "Scope",
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/analyses",
                    "headers": [
                        (b"cookie", f"test_session={self.session.lookup_token}".encode()),
                    ],
                },
            ),
            receive,
            send,
        )
        headers = dict(messages[0]["headers"])
        if messages[0]["status"] == 401:
            assert b"Max-Age=0" in headers[b"set-cookie"]
            assert b"unauthorized" in messages[1]["body"]
        else:
            assert b"set-cookie" not in headers
        assert b"refresh-" not in messages[1]["body"]
        return cast("int", messages[0]["status"])


@asynccontextmanager
async def _harness(
    boundary: Boundary = "response",
    failure: Failure = "none",
) -> AsyncIterator[_RotationHarness]:
    harness = _RotationHarness(boundary, failure)
    try:
        async with asyncio.timeout(5):
            await harness.provider.start()
            harness.session = await _establish_provider_session(
                harness.provider, harness.repository
            )
            yield harness
    finally:
        harness.release.set()
        await asyncio.gather(*harness.requests, return_exceptions=True)
        await harness.client.aclose()


@pytest.mark.parametrize("boundary", ["before_rotation", "response", "jwks"])
def test_cancelled_refresh_finishes_publication_before_unlocking_for_waiters(
    boundary: Boundary,
) -> None:
    async def scenario() -> None:
        async with _harness(boundary) as harness:
            first = harness.authenticate()
            await harness.entered.wait()
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(0)
            assert not first.done(), "caller cancellation abandoned the refresh transaction"
            assert harness.session.refresh_lock.locked()
            waiting = [harness.authenticate() for _ in range(4)]
            await asyncio.sleep(0)
            assert harness.refresh_tokens == ["refresh-1"]
            harness.release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            authentications = await asyncio.gather(*waiting)
            assert all(result is not None for result in authentications)
            for result in authentications:
                assert result is not None
                assert result.owner_context == harness.session.owner_context
                assert result.principal.roles == frozenset({AppRole.ANALYST})
            current = harness.repository.get_session(harness.session.lookup_token)
            assert current is not None
            assert current.provider_credentials.refresh_token == "refresh-2"
            assert current.csrf_token == harness.session.csrf_token
            assert current.owner_context == harness.session.owner_context
            assert current.absolute_expires_at == harness.session.absolute_expires_at
            assert not current.refresh_lock.locked()
            assert await harness.http_status() == 200
            assert harness.rotations == 1
            assert harness.refresh_tokens == ["refresh-1"]

    asyncio.run(scenario())


def test_cancelling_a_lock_waiter_does_not_dispatch_a_second_refresh() -> None:
    async def scenario() -> None:
        async with _harness() as harness:
            first = harness.authenticate()
            await harness.entered.wait()
            queued = harness.authenticate()
            await asyncio.sleep(0)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert not first.done()
            assert harness.refresh_tokens == ["refresh-1"]
            harness.release.set()
            assert await first is not None
            assert await harness.http_status() == 200
            assert harness.refresh_tokens == ["refresh-1"]

    asyncio.run(scenario())


@pytest.mark.parametrize("end_session", ["logout", "expiry"])
def test_session_end_after_publication_is_rechecked_before_authentication_returns(
    monkeypatch: pytest.MonkeyPatch,
    end_session: str,
) -> None:
    async def scenario() -> None:
        async with _harness() as harness:
            original_publish = harness.repository.replace_provider_credentials
            loop = asyncio.get_running_loop()

            def end_authentication() -> None:
                if end_session == "logout":
                    harness.repository.delete_session(harness.session.lookup_token)
                else:
                    harness.fake.now = harness.session.authentication_expires_at

            def publish_then_end(
                lookup_token: str,
                *,
                principal: Principal,
                provider_credentials: ProviderCredentialState,
            ) -> AuthenticatedSession | None:
                updated = original_publish(
                    lookup_token,
                    principal=principal,
                    provider_credentials=provider_credentials,
                )
                assert updated is not None
                # Publication is synchronous. End authentication on the next
                # turn, before its waiting request can consume the task result.
                loop.call_soon(end_authentication)
                return updated

            monkeypatch.setattr(
                harness.repository, "replace_provider_credentials", publish_then_end
            )
            first = harness.authenticate()
            await harness.entered.wait()
            harness.release.set()
            assert await first is None
            assert harness.repository.get_session(harness.session.lookup_token) is None
            assert await harness.http_status() == 401
            assert harness.refresh_tokens == ["refresh-1"]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["rejected", "timeout", "wrong_subject", "internal"])
def test_failed_cancelled_refresh_invalidates_before_waiters_can_replay_old_credentials(
    failure: Failure,
) -> None:
    async def scenario() -> None:
        errors: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        async with _harness(failure=failure) as harness:
            first = harness.authenticate()
            await harness.entered.wait()
            first.cancel()
            await asyncio.sleep(0)
            assert not first.done()
            first.cancel()
            await asyncio.sleep(0)
            assert not first.done()
            waiting = [harness.authenticate() for _ in range(3)]
            harness.release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert await asyncio.gather(*waiting) == [None] * 3
            assert harness.repository.get_session(harness.session.lookup_token) is None
            assert await harness.http_status() == 401
            assert harness.refresh_tokens == ["refresh-1"]
            assert not harness.session.refresh_lock.locked()
        await asyncio.sleep(0)
        assert errors == [], "a protected refresh task left an unobserved failure"

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_caller", [False, True])
@pytest.mark.parametrize("end_session", ["logout", "absolute_expiry", "refresh_expiry"])
def test_deleted_or_expired_sessions_win_over_late_refresh_publication(
    cancel_caller: bool,
    end_session: str,
) -> None:
    async def scenario() -> None:
        async with _harness() as harness:
            if end_session == "refresh_expiry":
                credentials = replace(
                    harness.session.provider_credentials,
                    refresh_token_expires_at=harness.fake.now + timedelta(seconds=1),
                )
                updated = harness.repository.replace_provider_credentials(
                    harness.session.lookup_token,
                    principal=harness.session.principal,
                    provider_credentials=credentials,
                )
                assert updated is not None
                harness.session = updated
            first = harness.authenticate()
            await harness.entered.wait()
            if cancel_caller:
                first.cancel()
                await asyncio.sleep(0)
                assert not first.done()
            if end_session == "logout":
                harness.repository.delete_session(harness.session.lookup_token)
            else:
                harness.fake.now = harness.session.authentication_expires_at
            harness.release.set()
            if cancel_caller:
                with pytest.raises(asyncio.CancelledError):
                    await first
            else:
                assert await first is None
            assert harness.repository.get_session(harness.session.lookup_token) is None
            assert await harness.http_status() == 401
            assert harness.refresh_tokens == ["refresh-1"]
            assert not harness.session.refresh_lock.locked()

    asyncio.run(scenario())

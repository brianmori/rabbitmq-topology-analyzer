import asyncio
import json
from pathlib import Path
from threading import Event, Semaphore
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

import pytest
from argon2 import PasswordHasher

from auth.basic import BasicAuthenticator
from auth.config import AuthSettings, BasicSettings, BasicUser
from auth.middleware import AuthenticationCapacityUnavailable, PreBodyAuthMiddleware
from auth.runtime import configure_auth_runtime, shutdown_auth_runtime
from core.deployment import DeploymentMode
from core.security import AppRole, AuthMode

if TYPE_CHECKING:
    from typing import Literal

    from starlette.types import Message, Receive, Scope, Send

KNOWN = "Basic YWxpY2U6cGFzc3dvcmQ="
UNKNOWN = "Basic dW5rbm93bjpwYXNzd29yZA=="


class BlockingHasher(PasswordHasher):
    def __init__(self) -> None:
        super().__init__()
        self.started = Semaphore(0)
        self.release = Event()
        self.calls = 0
        self.fail = False

    def verify(self, hash: str | bytes, password: str | bytes) -> Literal[True]:
        self.calls += 1
        self.started.release()
        if not self.release.wait(timeout=5):
            raise AssertionError("test did not release the verifier")
        if self.fail:
            raise RuntimeError("test worker failure")
        return True

    async def wait_started(self) -> None:
        assert await asyncio.to_thread(self.started.acquire, True, 5)


def _settings(limit: int = 2) -> BasicSettings:
    user = BasicUser("alice", ("test-only-hash",), frozenset({AppRole.ANALYST}))
    return BasicSettings(
        users_file=Path("/mounted/basic-users.json"),
        realm="Topology Test",
        users=MappingProxyType({user.username: user}),
        verification_limit=limit,
    )


@pytest.mark.parametrize("credential", [KNOWN, UNKNOWN])
def test_capacity_is_shared_by_known_and_unknown_users_without_queueing(credential: str) -> None:
    async def scenario() -> None:
        hasher = BlockingHasher()
        verifier = BasicAuthenticator(_settings(), password_hasher=hasher)
        requests = []
        try:
            for _ in range(2):
                requests.append(asyncio.create_task(verifier(credential, cast("Scope", {}))))
                await hasher.wait_started()
            with pytest.raises(AuthenticationCapacityUnavailable):
                await verifier(KNOWN, cast("Scope", {}))
            assert hasher.calls == 2
            hasher.release.set()
            results = await asyncio.gather(*requests)
            assert all((result is not None) == (credential == KNOWN) for result in results)
            assert await verifier(KNOWN, cast("Scope", {})) is not None
        finally:
            hasher.release.set()
            await asyncio.gather(*requests, return_exceptions=True)
            await verifier.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("worker_fails", [False, True])
def test_cancelling_waiters_does_not_release_running_hash_capacity(worker_fails: bool) -> None:
    async def scenario() -> None:
        hasher = BlockingHasher()
        hasher.fail = worker_fails
        verifier = BasicAuthenticator(_settings(1), password_hasher=hasher)
        request = asyncio.create_task(verifier(KNOWN, cast("Scope", {})))
        try:
            await hasher.wait_started()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            with pytest.raises(AuthenticationCapacityUnavailable):
                await verifier(UNKNOWN, cast("Scope", {}))
            assert hasher.calls == 1
        finally:
            hasher.release.set()
            await asyncio.gather(request, return_exceptions=True)
            await verifier.close()
        # Shutdown joins the actual worker, including the abandoned waiter.
        assert verifier._verification_slots.acquire(blocking=False)
        verifier._verification_slots.release()

    asyncio.run(scenario())


def test_shutdown_stops_admission_before_waiting_for_running_verifications() -> None:
    async def scenario() -> None:
        hasher = BlockingHasher()
        verifier = BasicAuthenticator(_settings(1), password_hasher=hasher)
        request = asyncio.create_task(verifier(KNOWN, cast("Scope", {})))
        closing = None
        try:
            await hasher.wait_started()
            closing = asyncio.create_task(verifier.close())
            await asyncio.sleep(0)
            assert not closing.done()
            with pytest.raises(AuthenticationCapacityUnavailable):
                await verifier(KNOWN, cast("Scope", {}))
            hasher.release.set()
            await closing
            assert await request is not None
        finally:
            hasher.release.set()
            await asyncio.gather(request, return_exceptions=True)
            if closing is not None:
                await closing
            else:
                await verifier.close()

    asyncio.run(scenario())


def test_worker_failure_releases_capacity_and_shutdown_stops_admission() -> None:
    async def scenario() -> None:
        hasher = BlockingHasher()
        hasher.release.set()
        hasher.fail = True
        verifier = BasicAuthenticator(_settings(1), password_hasher=hasher)
        try:
            with pytest.raises(RuntimeError, match="test worker failure"):
                await verifier(KNOWN, cast("Scope", {}))
            hasher.fail = False
            assert await verifier(KNOWN, cast("Scope", {})) is not None
        finally:
            await verifier.close()
        with pytest.raises(AuthenticationCapacityUnavailable):
            await verifier(KNOWN, cast("Scope", {}))

    asyncio.run(scenario())


def test_real_verifier_saturation_rejects_upload_without_reading_body_or_challenging() -> None:
    async def scenario() -> None:
        hasher = BlockingHasher()
        verifier = BasicAuthenticator(_settings(1), password_hasher=hasher)
        request = asyncio.create_task(verifier(KNOWN, cast("Scope", {})))
        messages: list[Message] = []

        async def downstream(_scope: Scope, _receive: Receive, _send: Send) -> None:
            raise AssertionError("saturated request reached FastAPI")

        async def receive() -> Message:
            raise AssertionError("saturated request read the multipart body")

        async def send(message: Message) -> None:
            messages.append(message)

        middleware = PreBodyAuthMiddleware(downstream, mode=AuthMode.BASIC, authenticator=verifier)
        scope = cast(
            "Scope",
            {
                "type": "http",
                "method": "POST",
                "path": "/api/definitions/upload",
                "headers": [
                    (b"authorization", KNOWN.encode()),
                    (b"content-type", b"multipart/form-data"),
                ],
            },
        )
        try:
            await hasher.wait_started()
            await middleware(scope, receive, send)
            assert messages[0]["status"] == 503
            headers = dict(messages[0]["headers"])
            assert headers[b"retry-after"] == b"1"
            assert headers[b"cache-control"] == b"no-store"
            assert b"www-authenticate" not in headers
            assert b"set-cookie" not in headers
            assert json.loads(messages[1]["body"]) == {
                "error": {
                    "code": "authentication_capacity_unavailable",
                    "message": "Authentication capacity is temporarily unavailable.",
                    "issues": [],
                }
            }
            assert hasher.calls == 1
        finally:
            hasher.release.set()
            await request
            await verifier.close()

    asyncio.run(scenario())


def test_runtime_shutdown_closes_the_basic_verifier() -> None:
    async def scenario() -> None:
        runtime = await configure_auth_runtime(
            AuthSettings(
                mode=AuthMode.BASIC,
                deployment_mode=DeploymentMode.DEVELOPMENT,
                public_origin="https://topology.example",
                oidc=None,
                basic=_settings(),
            )
        )
        verifier = runtime.basic_authenticator
        assert verifier is not None
        await shutdown_auth_runtime()
        with pytest.raises(AuthenticationCapacityUnavailable):
            await verifier(KNOWN, cast("Scope", {}))

    asyncio.run(scenario())

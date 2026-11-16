import asyncio
import base64
from dataclasses import replace
from pathlib import Path
from threading import get_ident
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from auth import basic
from auth.basic import BasicAuthenticator, basic_challenge
from auth.config import BasicSettings, BasicUser
from core.security import AppRole, AuthMode

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from starlette.types import Scope

    from auth.middleware import AuthenticatedRequest


class RecordingPasswordHasher(PasswordHasher):
    def __init__(self) -> None:
        super().__init__(
            time_cost=1,
            memory_cost=1024,
            parallelism=1,
            hash_len=16,
            salt_len=16,
        )
        self.verified_hashes: list[str] = []
        self.verification_threads: list[int] = []

    def verify(self, hash: str | bytes, password: str | bytes) -> bool:
        self.verified_hashes.append(hash.decode() if isinstance(hash, bytes) else hash)
        self.verification_threads.append(get_ident())
        return super().verify(hash, password)


class RejectingPasswordHasher(PasswordHasher):
    def __init__(self) -> None:
        super().__init__(
            time_cost=1,
            memory_cost=1024,
            parallelism=1,
            hash_len=16,
            salt_len=16,
        )
        self.calls: list[tuple[str, str | bytes]] = []

    def verify(self, hash: str | bytes, password: str | bytes) -> bool:
        rendered_hash = hash.decode() if isinstance(hash, bytes) else hash
        self.calls.append((rendered_hash, password))
        raise VerifyMismatchError


def _authorization(username: str, password: str, *, scheme: str = "Basic") -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"{scheme} {encoded}"


def _settings(*users: BasicUser, realm: str = "Topology Test") -> BasicSettings:
    return BasicSettings(
        users_file=Path("/mounted/basic-users.json"),
        realm=realm,
        users=MappingProxyType({user.username: user for user in users}),
    )


def _user(
    username: str,
    password_hashes: tuple[str, ...],
    *roles: AppRole,
) -> BasicUser:
    return BasicUser(
        username=username,
        password_hashes=password_hashes,
        roles=frozenset(roles),
    )


def _run(
    authentication: Coroutine[object, object, AuthenticatedRequest | None],
) -> AuthenticatedRequest | None:
    return asyncio.run(authentication)


def _authenticate(authenticator: BasicAuthenticator, header: str) -> AuthenticatedRequest | None:
    return _run(authenticator(header, cast("Scope", {})))


def test_current_and_overlap_password_hashes_verify_in_configured_order() -> None:
    hasher = RecordingPasswordHasher()
    current_hash = hasher.hash("current-password")
    overlap_hash = hasher.hash("overlap-password")
    user = _user("alice", (current_hash, overlap_hash), AppRole.ANALYST)
    authenticator = BasicAuthenticator(_settings(user), password_hasher=hasher)

    current = _authenticate(authenticator, _authorization("alice", "current-password"))
    assert current is not None
    assert hasher.verified_hashes == [current_hash]

    hasher.verified_hashes.clear()
    overlap = _authenticate(authenticator, _authorization("alice", "overlap-password"))
    assert overlap is not None
    assert hasher.verified_hashes == [current_hash, overlap_hash]

    hasher.verified_hashes.clear()
    invalid = _authenticate(authenticator, _authorization("alice", "wrong-password"))
    assert invalid is None
    assert hasher.verified_hashes == [current_hash, overlap_hash]


def test_scheme_is_case_insensitive_and_password_splits_only_at_first_colon() -> None:
    hasher = RecordingPasswordHasher()
    password_hash = hasher.hash("päss:with:colons")
    authenticator = BasicAuthenticator(
        _settings(_user("Alice", (password_hash,), AppRole.VIEWER)),
        password_hasher=hasher,
    )

    authentication = _authenticate(
        authenticator,
        _authorization("Alice", "päss:with:colons", scheme="bAsIc"),
    )

    assert authentication is not None
    assert authentication.principal.auth_mode is AuthMode.BASIC


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Basic",
        "Basic ",
        "Bearer YWxpY2U6cGFzc3dvcmQ=",
        " Basic YWxpY2U6cGFzc3dvcmQ=",
        "Basic  YWxpY2U6cGFzc3dvcmQ=",
        "Basic\tYWxpY2U6cGFzc3dvcmQ=",
        "Basic YWxpY2U6cGFzc3dvcmQ= trailing",
        "Basic !!!not-base64!!!",
        "Basic YWxpY2U6cGFzc3dvcmQ==",
        "Basic YWxpY2U6cGFzc3dvcmQ",
        f"Basic {base64.b64encode(b'not-colon-delimited').decode()}",
        f"Basic {base64.b64encode(b'bad user:password').decode()}",
        f"Basic {base64.b64encode(b':password').decode()}",
        f"Basic {base64.b64encode((b'a' * 129) + b':password').decode()}",
        f"Basic {base64.b64encode(b'\xff:password').decode()}",
    ],
)
def test_malformed_authorization_values_fail_equivalently(header: str) -> None:
    authenticator = BasicAuthenticator(_settings())

    assert _authenticate(authenticator, header) is None


def test_unknown_user_performs_one_fixed_valid_dummy_verification() -> None:
    hasher = RejectingPasswordHasher()
    authenticator = BasicAuthenticator(_settings(), password_hasher=hasher)

    assert _authenticate(authenticator, _authorization("unknown-one", "guess-one")) is None
    assert _authenticate(authenticator, _authorization("unknown-two", "guess-two")) is None

    assert len(hasher.calls) == 2
    first_hash, first_password = hasher.calls[0]
    second_hash, second_password = hasher.calls[1]
    assert first_hash == second_hash
    assert first_hash.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    assert first_password == "guess-one"
    assert second_password == "guess-two"


def test_fixed_dummy_record_is_a_real_minimum_cost_argon2id_hash() -> None:
    with pytest.raises(VerifyMismatchError):
        PasswordHasher().verify(basic._DUMMY_PASSWORD_HASH, "an-unrelated-password")


def test_invalid_hash_encoding_fails_safely_and_releases_the_real_verifier_slot() -> None:
    async def scenario() -> None:
        password_hash = PasswordHasher(memory_cost=65536, time_cost=3, parallelism=1).hash(
            "password"
        )
        invalid_hash = password_hash.replace("m=65536", "m=٦٥٥٣٦")
        settings = replace(
            _settings(
                _user("invalid", (invalid_hash,), AppRole.VIEWER),
                _user("valid", (password_hash,), AppRole.VIEWER),
            ),
            verification_limit=1,
        )
        verifier = BasicAuthenticator(settings)
        try:
            assert await verifier(_authorization("invalid", "password"), cast("Scope", {})) is None
            assert (
                await verifier(_authorization("valid", "password"), cast("Scope", {})) is not None
            )
        finally:
            await verifier.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        ((), frozenset()),
        ((AppRole.VIEWER,), frozenset({AppRole.VIEWER})),
        ((AppRole.ANALYST,), frozenset({AppRole.ANALYST})),
    ],
)
def test_verified_roles_are_mapped_without_constructing_owner_context(
    roles: tuple[AppRole, ...],
    expected: frozenset[AppRole],
) -> None:
    hasher = RecordingPasswordHasher()
    user = _user("mapped-user", (hasher.hash("password"),), *roles)
    authenticator = BasicAuthenticator(_settings(user), password_hasher=hasher)

    authentication = _authenticate(authenticator, _authorization("mapped-user", "password"))

    assert authentication is not None
    assert authentication.principal.roles == expected
    assert authentication.owner_context is None
    assert authentication.csrf_token is None


def test_principal_identity_is_stable_namespaced_and_distinct_by_username() -> None:
    hasher = RecordingPasswordHasher()
    password_hash = hasher.hash("shared-password")
    authenticator = BasicAuthenticator(
        _settings(
            _user("alice", (password_hash,), AppRole.VIEWER),
            _user("bob", (password_hash,), AppRole.VIEWER),
        ),
        password_hasher=hasher,
    )

    first_alice = _authenticate(authenticator, _authorization("alice", "shared-password"))
    second_alice = _authenticate(authenticator, _authorization("alice", "shared-password"))
    bob = _authenticate(authenticator, _authorization("bob", "shared-password"))

    assert first_alice is not None
    assert second_alice is not None
    assert bob is not None
    assert first_alice.principal.principal_id == second_alice.principal.principal_id
    assert first_alice.principal.principal_id != bob.principal.principal_id
    assert first_alice.principal.principal_id.startswith("basic:sha256:")
    assert len(first_alice.principal.principal_id.removeprefix("basic:sha256:")) == 64
    assert "alice" not in first_alice.principal.principal_id


def test_password_verification_runs_outside_the_event_loop_thread() -> None:
    hasher = RecordingPasswordHasher()
    user = _user("alice", (hasher.hash("password"),), AppRole.VIEWER)
    authenticator = BasicAuthenticator(_settings(user), password_hasher=hasher)

    assert _authenticate(authenticator, _authorization("alice", "password")) is not None
    assert len(hasher.verification_threads) == 1
    assert hasher.verification_threads[0] != get_ident()


def test_challenge_and_representations_contain_no_password_or_hash() -> None:
    hasher = RecordingPasswordHasher()
    password = "do-not-retain-this-password"
    password_hash = hasher.hash(password)
    settings = _settings(
        _user("alice", (password_hash,), AppRole.ANALYST),
        realm="Topology Realm",
    )
    authenticator = BasicAuthenticator(settings, password_hasher=hasher)

    authentication = _authenticate(authenticator, _authorization("alice", password))

    assert authentication is not None
    assert authenticator.challenge == 'Basic realm="Topology Realm", charset="UTF-8"'
    assert basic_challenge(settings.realm) == authenticator.challenge
    for rendered in (repr(authenticator), repr(settings), repr(authentication)):
        assert password not in rendered
        assert password_hash not in rendered
    assert password not in repr(authenticator.__dict__)
    assert password_hash not in repr(authenticator.__dict__)

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID

import pytest

from auth.sessions import (
    AuthenticatedSession,
    AuthenticatedSessionCapacityError,
    AuthSessionRepository,
    LoginCorrelationCapacityError,
    ProviderCredentialState,
)
from core.security import AppRole, AuthMode, Principal


@dataclass(slots=True)
class MutableClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current

    def advance(self, interval: timedelta) -> None:
        self.current += interval


class DeterministicTokens:
    def __init__(self) -> None:
        self.requested_bytes: list[int] = []
        self._next = 1

    def __call__(self, byte_count: int) -> str:
        self.requested_bytes.append(byte_count)
        value = f"secret-token-{self._next:032d}"
        self._next += 1
        return value


class DeterministicUuids:
    def __init__(self) -> None:
        self._next = 1

    def __call__(self) -> UUID:
        value = UUID(f"00000000-0000-4000-8000-{self._next:012x}")
        self._next += 1
        return value


def _principal(
    principal_id: str = "oidc:verified-principal",
    roles: frozenset[AppRole] = frozenset({AppRole.VIEWER}),
    auth_mode: AuthMode = AuthMode.OIDC,
) -> Principal:
    return Principal(principal_id=principal_id, roles=roles, auth_mode=auth_mode)


def _credentials(
    now: datetime,
    *,
    access_token: str = "provider-access-token",
    refresh_token: str = "provider-refresh-token",
) -> ProviderCredentialState:
    return ProviderCredentialState(
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_expires_at=now + timedelta(minutes=5),
        refresh_token_expires_at=now + timedelta(hours=1),
    )


def _repository(
    *,
    login_limit: int = 2,
    login_ttl: timedelta = timedelta(minutes=10),
    session_limit: int = 2,
    session_lifetime: timedelta = timedelta(hours=8),
) -> tuple[AuthSessionRepository, MutableClock, DeterministicTokens]:
    clock = MutableClock(datetime(2026, 8, 13, 12, tzinfo=UTC))
    tokens = DeterministicTokens()
    repository = AuthSessionRepository(
        login_limit=login_limit,
        login_ttl=login_ttl,
        session_limit=session_limit,
        session_lifetime=session_lifetime,
        clock=clock,
        token_generator=tokens,
        uuid_generator=DeterministicUuids(),
    )
    return repository, clock, tokens


def _establish(
    repository: AuthSessionRepository,
    now: datetime,
    *,
    principal: Principal | None = None,
    provider_absolute_expires_at: datetime | None = None,
) -> AuthenticatedSession:
    return repository.establish_session(
        principal=principal or _principal(),
        issuer="https://issuer.example.test",
        subject="provider-subject",
        nonce="initial-login-nonce",
        provider_credentials=_credentials(now),
        provider_absolute_expires_at=provider_absolute_expires_at,
    )


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("login_limit", 0),
        ("login_ttl", timedelta(0)),
        ("session_limit", -1),
        ("session_lifetime", timedelta(seconds=-1)),
    ],
)
def test_repository_rejects_non_positive_bounds(argument: str, value: object) -> None:
    with pytest.raises(ValueError):
        AuthSessionRepository(**{argument: value})  # type: ignore[arg-type]


def test_login_correlation_uses_independent_256_bit_values_and_hides_them() -> None:
    repository, clock, tokens = _repository()

    correlation = repository.create_login_correlation("/workbench?analysis=orders")

    assert tokens.requested_bytes == [32, 32, 32, 32]
    assert correlation.created_at == clock.current
    assert correlation.expires_at == clock.current + timedelta(minutes=10)
    assert correlation.return_to == "/workbench?analysis=orders"
    assert (
        len(
            {
                correlation.state,
                correlation.browser_binding,
                correlation.nonce,
                correlation.pkce_verifier,
            }
        )
        == 4
    )
    rendered = repr(correlation)
    assert correlation.state not in rendered
    assert correlation.browser_binding not in rendered
    assert correlation.nonce not in rendered
    assert correlation.pkce_verifier not in rendered


def test_login_correlation_is_consumed_once_and_callback_replay_fails() -> None:
    repository, _clock, _tokens = _repository()
    correlation = repository.create_login_correlation()

    assert (
        repository.consume_login_correlation(
            correlation.state, browser_binding=correlation.browser_binding
        )
        is correlation
    )
    assert (
        repository.consume_login_correlation(
            correlation.state, browser_binding=correlation.browser_binding
        )
        is None
    )
    assert repository.consume_login_correlation("unknown-state", browser_binding="unknown") is None
    assert repository.login_count == 0


@pytest.mark.parametrize("browser_binding", [None, "", "wrong-browser", "non-ascii-\u00e9"])
def test_missing_or_incorrect_browser_binding_consumes_state(browser_binding: str | None) -> None:
    repository, _clock, _tokens = _repository()
    correlation = repository.create_login_correlation()

    assert (
        repository.consume_login_correlation(correlation.state, browser_binding=browser_binding)
        is None
    )
    assert repository.login_count == 0
    assert (
        repository.consume_login_correlation(
            correlation.state, browser_binding=correlation.browser_binding
        )
        is None
    )


def test_concurrent_callback_replay_can_consume_state_only_once() -> None:
    repository, _clock, _tokens = _repository()
    correlation = repository.create_login_correlation()
    barrier = Barrier(8)

    def consume() -> object | None:
        barrier.wait()
        return repository.consume_login_correlation(
            correlation.state, browser_binding=correlation.browser_binding
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        consumed = list(executor.map(lambda _index: consume(), range(8)))

    assert sum(item is correlation for item in consumed) == 1
    assert sum(item is None for item in consumed) == 7


def test_login_capacity_never_evicts_live_state_but_expiry_frees_a_slot() -> None:
    repository, clock, _tokens = _repository(login_limit=1, login_ttl=timedelta(seconds=5))
    first = repository.create_login_correlation("/first")

    with pytest.raises(LoginCorrelationCapacityError):
        repository.create_login_correlation("/must-not-evict")
    assert (
        repository.consume_login_correlation(first.state, browser_binding=first.browser_binding)
        is first
    )

    expiring = repository.create_login_correlation("/expiring")
    clock.advance(timedelta(seconds=5))
    replacement = repository.create_login_correlation("/replacement")

    assert (
        repository.consume_login_correlation(
            expiring.state, browser_binding=expiring.browser_binding
        )
        is None
    )
    assert (
        repository.consume_login_correlation(
            replacement.state, browser_binding=replacement.browser_binding
        )
        is replacement
    )


def test_concurrent_login_admission_cannot_exceed_the_configured_limit() -> None:
    repository, _clock, _tokens = _repository(login_limit=2)
    barrier = Barrier(10)

    def create(index: int) -> bool:
        barrier.wait()
        try:
            repository.create_login_correlation(f"/attempt-{index}")
        except LoginCorrelationCapacityError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=10) as executor:
        admitted = list(executor.map(create, range(10)))

    assert sum(admitted) == 2
    assert repository.login_count == 2


def test_established_session_has_independent_opaque_values_and_stable_owner() -> None:
    repository, clock, tokens = _repository()
    provider_boundary = clock.current + timedelta(hours=2)

    session = _establish(
        repository,
        clock.current,
        provider_absolute_expires_at=provider_boundary,
    )

    assert tokens.requested_bytes == [32, 32]
    assert session.lookup_token != session.csrf_token
    assert session.application_session_id.version == 4
    assert session.owner_context.principal_id == session.principal.principal_id
    assert session.owner_context.session_id == str(session.application_session_id)
    assert session.created_at == clock.current
    assert session.absolute_expires_at == provider_boundary
    assert repository.get_session(session.lookup_token) is session

    rendered = repr(session)
    for secret in (
        session.lookup_token,
        session.csrf_token,
        session.subject,
        session.nonce,
        session.provider_credentials.access_token,
        session.provider_credentials.refresh_token,
    ):
        assert secret not in rendered
    assert session.provider_credentials.access_token not in repr(session.provider_credentials)
    assert session.provider_credentials.refresh_token not in repr(session.provider_credentials)


def test_established_capacity_never_evicts_live_session_but_expiry_frees_slot() -> None:
    repository, clock, _tokens = _repository(
        session_limit=1,
        session_lifetime=timedelta(seconds=5),
    )
    first = _establish(repository, clock.current)

    with pytest.raises(AuthenticatedSessionCapacityError):
        _establish(repository, clock.current)
    assert repository.get_session(first.lookup_token) is first

    clock.advance(timedelta(seconds=5))
    replacement = _establish(repository, clock.current)

    assert repository.get_session(first.lookup_token) is None
    assert repository.get_session(replacement.lookup_token) is replacement
    assert repository.session_count == 1


def test_concurrent_session_admission_cannot_exceed_the_configured_limit() -> None:
    repository, clock, _tokens = _repository(session_limit=2)
    barrier = Barrier(10)

    def establish(_index: int) -> bool:
        barrier.wait()
        try:
            _establish(repository, clock.current)
        except AuthenticatedSessionCapacityError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=10) as executor:
        admitted = list(executor.map(establish, range(10)))

    assert sum(admitted) == 2
    assert repository.session_count == 2


def test_provider_refresh_replaces_only_validated_mutable_state() -> None:
    repository, clock, _tokens = _repository()
    session = _establish(repository, clock.current)
    refreshed_principal = _principal(roles=frozenset({AppRole.ANALYST}))
    refreshed_credentials = _credentials(
        clock.current,
        access_token="rotated-access-token",
        refresh_token="rotated-refresh-token",
    )

    refreshed = repository.replace_provider_credentials(
        session.lookup_token,
        principal=refreshed_principal,
        provider_credentials=refreshed_credentials,
    )

    assert refreshed is not None
    assert refreshed.principal is refreshed_principal
    assert refreshed.provider_credentials is refreshed_credentials
    assert refreshed.lookup_token == session.lookup_token
    assert refreshed.csrf_token == session.csrf_token
    assert refreshed.application_session_id == session.application_session_id
    assert refreshed.owner_context == session.owner_context
    assert refreshed.absolute_expires_at == refreshed_credentials.refresh_token_expires_at
    assert refreshed.refresh_lock is session.refresh_lock


@pytest.mark.parametrize("later_expiry", [None, timedelta(hours=2)])
def test_refresh_shortens_expiry_and_cannot_extend_the_session_again(
    later_expiry: timedelta | None,
) -> None:
    repository, clock, _tokens = _repository()
    session = _establish(repository, clock.current)
    boundary = clock.current + timedelta(seconds=1)
    credentials = replace(
        session.provider_credentials,
        access_token_expires_at=clock.current + timedelta(hours=1),
        refresh_token_expires_at=boundary,
    )
    shortened = repository.replace_provider_credentials(
        session.lookup_token,
        principal=session.principal,
        provider_credentials=credentials,
    )
    assert shortened is not None
    assert shortened.absolute_expires_at == boundary
    assert repository.remaining_session_lifetime_seconds(session.lookup_token) == 1

    updated = repository.replace_provider_credentials(
        session.lookup_token,
        principal=session.principal,
        provider_credentials=replace(
            credentials,
            refresh_token_expires_at=clock.current + later_expiry if later_expiry else None,
        ),
    )
    assert updated is not None
    assert updated.absolute_expires_at == boundary
    clock.advance(timedelta(microseconds=999_999))
    assert repository.get_session(session.lookup_token) is updated
    clock.advance(timedelta(microseconds=1))
    assert repository.reap_expired().authenticated_sessions == 1
    assert repository.get_session(session.lookup_token) is None


def test_refresh_cannot_publish_already_expired_credentials() -> None:
    repository, clock, _tokens = _repository()
    session = _establish(repository, clock.current)

    assert (
        repository.replace_provider_credentials(
            session.lookup_token,
            principal=session.principal,
            provider_credentials=replace(
                session.provider_credentials,
                refresh_token_expires_at=clock.current,
            ),
        )
        is None
    )
    assert repository.get_session(session.lookup_token) is None
    assert repository.session_count == 0


def test_reaping_enforces_current_credentials_even_without_an_explicit_provider_boundary() -> None:
    repository, clock, _tokens = _repository()
    session = _establish(repository, clock.current)
    clock.advance(timedelta(hours=1))

    assert repository.reap_expired().authenticated_sessions == 1
    assert repository.get_session(session.lookup_token) is None


@pytest.mark.parametrize(
    "principal",
    [
        _principal(principal_id="oidc:different-principal"),
        _principal(auth_mode=AuthMode.BASIC),
    ],
)
def test_provider_refresh_cannot_change_stable_identity(principal: Principal) -> None:
    repository, clock, _tokens = _repository()
    session = _establish(repository, clock.current)

    with pytest.raises(ValueError, match="stable identity"):
        repository.replace_provider_credentials(
            session.lookup_token,
            principal=principal,
            provider_credentials=_credentials(clock.current),
        )

    assert repository.get_session(session.lookup_token) is session


def test_delete_and_explicit_reap_are_deterministic() -> None:
    repository, clock, _tokens = _repository(
        login_ttl=timedelta(seconds=5),
        session_lifetime=timedelta(seconds=5),
    )
    correlation = repository.create_login_correlation()
    session = _establish(repository, clock.current)

    clock.advance(timedelta(seconds=5))
    reaped = repository.reap_expired()

    assert reaped.login_correlations == 1
    assert reaped.authenticated_sessions == 1
    assert (
        repository.consume_login_correlation(
            correlation.state, browser_binding=correlation.browser_binding
        )
        is None
    )
    assert repository.delete_session(session.lookup_token) is None

    replacement = _establish(repository, clock.current)
    assert repository.delete_session(replacement.lookup_token) is replacement
    assert repository.delete_session(replacement.lookup_token) is None


def test_repository_rejects_naive_clock_and_expired_provider_boundary() -> None:
    def naive_clock() -> datetime:
        return datetime(2026, 8, 13, 12)

    repository = AuthSessionRepository(clock=naive_clock)
    with pytest.raises(ValueError, match="timezone-aware"):
        repository.create_login_correlation()

    repository, clock, _tokens = _repository()
    with pytest.raises(ValueError, match="future"):
        _establish(
            repository,
            clock.current,
            provider_absolute_expires_at=clock.current,
        )

"""Bounded process-local authentication session state.

This module stores OIDC login correlations and established application
sessions without performing provider discovery, token exchange, validation, or
refresh. Those protocol operations consume this repository in later
iterations.
"""

import asyncio
import hmac
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from threading import RLock
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from core.session import OwnerContext

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from core.security import Principal

_TOKEN_BYTES = 32
_TOKEN_GENERATION_ATTEMPTS = 32
_APPLICATION_SESSION_UUID_VERSION = 4
_DEFAULT_LOGIN_LIMIT = 256
_DEFAULT_LOGIN_TTL = timedelta(minutes=10)
_DEFAULT_SESSION_LIMIT = 1024
_DEFAULT_SESSION_LIFETIME = timedelta(hours=8)


def _utc_now() -> datetime:
    """Return the current aware UTC time."""
    return datetime.now(UTC)


def _random_token(byte_count: int) -> str:
    """Return a URL-safe token backed by the requested random-byte count."""
    return token_urlsafe(byte_count)


@dataclass(frozen=True, slots=True)
class LoginCorrelation:
    """One pending OIDC Authorization Code correlation.

    The state lookup value, browser binding, nonce, and PKCE verifier are
    bearer-like secrets: none is included in the generated representation.
    """

    state: str = field(repr=False)
    browser_binding: str = field(repr=False)
    nonce: str = field(repr=False)
    pkce_verifier: str = field(repr=False)
    return_to: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderCredentialState:
    """Opaque provider credentials retained for a later server-side refresh.

    The repository deliberately does not interpret these token values. R6.3
    owns validation, refresh, and identity-continuity checks before replacing
    this immutable state.
    """

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """One established opaque application session.

    ``application_session_id`` is the stable ownership component. It is
    independent of the cookie lookup value and all provider credentials, and
    therefore survives a valid provider-token refresh.
    """

    lookup_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    application_session_id: UUID
    principal: Principal
    issuer: str
    subject: str = field(repr=False)
    nonce: str = field(repr=False)
    provider_credentials: ProviderCredentialState = field(repr=False)
    created_at: datetime
    absolute_expires_at: datetime
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    @property
    def owner_context(self) -> OwnerContext:
        """Return the stable domain ownership key for this session."""
        return OwnerContext(
            principal_id=self.principal.principal_id,
            session_id=str(self.application_session_id),
        )

    @property
    def authentication_expires_at(self) -> datetime:
        """Return the earlier application or current provider refresh boundary."""
        refresh_expiry = self.provider_credentials.refresh_token_expires_at
        if refresh_expiry is None:
            return self.absolute_expires_at
        return min(self.absolute_expires_at, refresh_expiry)


@dataclass(frozen=True, slots=True)
class ExpiryReapResult:
    """Counts removed during one explicit expiry sweep."""

    login_correlations: int
    authenticated_sessions: int


class LoginCorrelationCapacityError(Exception):
    """Raised when every pending-correlation slot contains a live record."""


class AuthenticatedSessionCapacityError(Exception):
    """Raised when every established-session slot contains a live record."""


class AuthSessionRepository:
    """Own bounded login and established-session state for one web process."""

    def __init__(
        self,
        *,
        login_limit: int = _DEFAULT_LOGIN_LIMIT,
        login_ttl: timedelta = _DEFAULT_LOGIN_TTL,
        session_limit: int = _DEFAULT_SESSION_LIMIT,
        session_lifetime: timedelta = _DEFAULT_SESSION_LIFETIME,
        clock: Callable[[], datetime] = _utc_now,
        token_generator: Callable[[int], str] = _random_token,
        uuid_generator: Callable[[], UUID] = uuid4,
    ) -> None:
        """Create an empty repository with deterministic seams for tests.

        Args:
            login_limit: Maximum simultaneously live login correlations.
            login_ttl: Absolute lifetime of each login correlation.
            session_limit: Maximum simultaneously live established sessions.
            session_lifetime: Configured absolute application-session lifetime.
            clock: Aware UTC-compatible clock.
            token_generator: Generates URL-safe values from a requested number
                of random bytes. Production requests 32 bytes for every opaque
                lookup, nonce, verifier, and CSRF value.
            uuid_generator: Generates application-session UUIDs.

        Raises:
            ValueError: When a configured bound is not positive.
        """
        if login_limit <= 0:
            raise ValueError("login correlation limit must be positive")
        if login_ttl <= timedelta(0):
            raise ValueError("login correlation lifetime must be positive")
        if session_limit <= 0:
            raise ValueError("authenticated session limit must be positive")
        if session_lifetime <= timedelta(0):
            raise ValueError("authenticated session lifetime must be positive")

        self._login_limit = login_limit
        self._login_ttl = login_ttl
        self._session_limit = session_limit
        self._session_lifetime = session_lifetime
        self._clock = clock
        self._token_generator = token_generator
        self._uuid_generator = uuid_generator
        self._login_correlations: dict[str, LoginCorrelation] = {}
        self._authenticated_sessions: dict[str, AuthenticatedSession] = {}
        self._application_session_ids: set[UUID] = set()
        self._lock = RLock()

    @property
    def login_count(self) -> int:
        """Return the number of currently live login correlations."""
        with self._lock:
            self._reap_expired_locked(self._now())
            return len(self._login_correlations)

    @property
    def session_count(self) -> int:
        """Return the number of currently live established sessions."""
        with self._lock:
            self._reap_expired_locked(self._now())
            return len(self._authenticated_sessions)

    def create_login_correlation(self, return_to: str = "/") -> LoginCorrelation:
        """Create one bounded, expiring, single-use OIDC login correlation.

        Args:
            return_to: Already validated same-origin application path retained
                for the confined callback redirect.

        Returns:
            The newly stored correlation.

        Raises:
            LoginCorrelationCapacityError: When all slots contain live records.
            RuntimeError: When the injected generator repeatedly collides.
        """
        with self._lock:
            now = self._now()
            self._reap_expired_locked(now)
            if len(self._login_correlations) >= self._login_limit:
                raise LoginCorrelationCapacityError

            state = self._unique_token(self._login_correlations)
            correlation = LoginCorrelation(
                state=state,
                browser_binding=self._new_token(),
                nonce=self._new_token(),
                pkce_verifier=self._new_token(),
                return_to=return_to,
                created_at=now,
                expires_at=now + self._login_ttl,
            )
            self._login_correlations[state] = correlation
            return correlation

    def consume_login_correlation(
        self,
        state: str,
        *,
        browser_binding: str | None,
    ) -> LoginCorrelation | None:
        """Consume a live correlation only for the browser that initiated it.

        A repeated, unknown, expired, or incorrectly bound state returns
        ``None``. Every attempt consumes a known state under the repository
        lock, so failures and concurrent callbacks cannot replay it. The opaque
        browser binding must come from the separate HttpOnly login cookie,
        never from a callback query parameter.
        """
        with self._lock:
            self._reap_expired_locked(self._now())
            correlation = self._login_correlations.pop(state, None)
            if (
                correlation is None
                or browser_binding is None
                or not browser_binding.isascii()
                or not hmac.compare_digest(browser_binding, correlation.browser_binding)
            ):
                return None
            return correlation

    def establish_session(
        self,
        *,
        principal: Principal,
        issuer: str,
        subject: str,
        nonce: str,
        provider_credentials: ProviderCredentialState,
        provider_absolute_expires_at: datetime | None = None,
    ) -> AuthenticatedSession:
        """Create one bounded opaque session after external OIDC validation.

        Args:
            principal: Verified identity and centrally mapped roles.
            issuer: Exact verified provider issuer.
            subject: Exact verified provider subject.
            nonce: Initial correlation nonce retained for optional refresh ID
                token validation.
            provider_credentials: Already validated opaque provider state.
            provider_absolute_expires_at: Optional earlier provider boundary for
                the application session.

        Returns:
            The established immutable application-session record.

        Raises:
            AuthenticatedSessionCapacityError: When all slots contain live
                sessions.
            ValueError: When an explicit provider boundary is already expired.
            RuntimeError: When an injected token or UUID generator repeatedly
                collides or does not produce UUIDv4 values.
        """
        with self._lock:
            now = self._now()
            self._reap_expired_locked(now)
            if len(self._authenticated_sessions) >= self._session_limit:
                raise AuthenticatedSessionCapacityError

            absolute_expires_at = now + self._session_lifetime
            if provider_absolute_expires_at is not None:
                provider_boundary = self._aware_datetime(
                    provider_absolute_expires_at,
                    "provider absolute expiry",
                )
                if provider_boundary <= now:
                    raise ValueError("provider absolute expiry must be in the future")
                absolute_expires_at = min(absolute_expires_at, provider_boundary)

            lookup_token = self._unique_token(self._authenticated_sessions)
            application_session_id = self._unique_application_session_id()
            session = AuthenticatedSession(
                lookup_token=lookup_token,
                csrf_token=self._new_token(),
                application_session_id=application_session_id,
                principal=principal,
                issuer=issuer,
                subject=subject,
                nonce=nonce,
                provider_credentials=provider_credentials,
                created_at=now,
                absolute_expires_at=absolute_expires_at,
            )
            self._authenticated_sessions[lookup_token] = session
            self._application_session_ids.add(application_session_id)
            return session

    def get_session(self, lookup_token: str) -> AuthenticatedSession | None:
        """Return a live established session by its opaque cookie value."""
        with self._lock:
            self._reap_expired_locked(self._now())
            return self._authenticated_sessions.get(lookup_token)

    def remaining_session_lifetime_seconds(self, lookup_token: str) -> int:
        """Return a live session's whole remaining seconds for cookie Max-Age."""
        with self._lock:
            now = self._now()
            self._reap_expired_locked(now)
            session = self._authenticated_sessions.get(lookup_token)
            if session is None:
                return 0
            return max(0, int((session.authentication_expires_at - now).total_seconds()))

    def replace_provider_credentials(
        self,
        lookup_token: str,
        *,
        principal: Principal,
        provider_credentials: ProviderCredentialState,
    ) -> AuthenticatedSession | None:
        """Replace validated refresh state without changing session ownership.

        R6.3 performs provider I/O and identity validation. This atomic storage
        operation additionally refuses a changed stable principal identifier or
        authentication mode, while allowing centrally mapped roles to change.
        A refreshed provider boundary can shorten, but never extend, the
        application deadline. Credentials already expired on arrival invalidate
        authentication without performing owner-resource logout cleanup.
        """
        with self._lock:
            now = self._now()
            self._reap_expired_locked(now)
            current = self._authenticated_sessions.get(lookup_token)
            if current is None:
                return None
            if (
                principal.principal_id != current.principal.principal_id
                or principal.auth_mode is not current.principal.auth_mode
            ):
                raise ValueError("provider refresh cannot change stable identity")
            absolute_expires_at = current.absolute_expires_at
            refresh_expiry = provider_credentials.refresh_token_expires_at
            if refresh_expiry is not None:
                absolute_expires_at = min(
                    absolute_expires_at,
                    self._aware_datetime(refresh_expiry, "provider refresh expiry"),
                )
            if absolute_expires_at <= now:
                self._authenticated_sessions.pop(lookup_token)
                self._application_session_ids.discard(current.application_session_id)
                return None
            updated = replace(
                current,
                principal=principal,
                provider_credentials=provider_credentials,
                absolute_expires_at=absolute_expires_at,
            )
            self._authenticated_sessions[lookup_token] = updated
            return updated

    def delete_session(self, lookup_token: str) -> AuthenticatedSession | None:
        """Remove and return one session during logout or credential failure."""
        with self._lock:
            self._reap_expired_locked(self._now())
            session = self._authenticated_sessions.pop(lookup_token, None)
            if session is not None:
                self._application_session_ids.discard(session.application_session_id)
            return session

    def reap_expired(self) -> ExpiryReapResult:
        """Remove every expired correlation and established session."""
        with self._lock:
            return self._reap_expired_locked(self._now())

    def _now(self) -> datetime:
        """Read and validate the injected clock."""
        return self._aware_datetime(self._clock(), "authentication repository clock")

    @staticmethod
    def _aware_datetime(value: datetime, label: str) -> datetime:
        """Require an aware datetime so expiry comparisons cannot drift."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} must be timezone-aware")
        return value

    def _new_token(self) -> str:
        """Generate one independent value from 256 random bits."""
        token = self._token_generator(_TOKEN_BYTES)
        if not token:
            raise RuntimeError("authentication token generator returned an empty value")
        return token

    def _unique_token(self, records: Mapping[str, object]) -> str:
        """Generate a lookup value that cannot overwrite a live record."""
        for _attempt in range(_TOKEN_GENERATION_ATTEMPTS):
            token = self._new_token()
            if token not in records:
                return token
        raise RuntimeError("authentication token generator repeatedly collided")

    def _unique_application_session_id(self) -> UUID:
        """Generate a live-set-unique UUIDv4 ownership component."""
        for _attempt in range(_TOKEN_GENERATION_ATTEMPTS):
            session_id = self._uuid_generator()
            if session_id.version != _APPLICATION_SESSION_UUID_VERSION:
                raise RuntimeError("application session generator must return UUIDv4")
            if session_id not in self._application_session_ids:
                return session_id
        raise RuntimeError("application session generator repeatedly collided")

    def _reap_expired_locked(self, now: datetime) -> ExpiryReapResult:
        """Remove expired records while the repository lock is held."""
        expired_login_states = [
            state
            for state, correlation in self._login_correlations.items()
            if correlation.expires_at <= now
        ]
        for state in expired_login_states:
            self._login_correlations.pop(state)

        expired_session_tokens = [
            token
            for token, session in self._authenticated_sessions.items()
            if session.authentication_expires_at <= now
        ]
        for token in expired_session_tokens:
            session = self._authenticated_sessions.pop(token)
            self._application_session_ids.discard(session.application_session_id)

        return ExpiryReapResult(
            login_correlations=len(expired_login_states),
            authenticated_sessions=len(expired_session_tokens),
        )

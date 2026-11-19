"""Server-owned OpenID Connect Authorization Code and refresh operations."""

import asyncio
import base64
import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import httpx2
import jwt

from auth.middleware import AuthenticatedRequest
from auth.oidc_client import OidcRequestAdapter
from auth.sessions import (
    AuthenticatedSession,
    AuthSessionRepository,
    ProviderCredentialState,
)
from core.security import AppRole, AuthMode, Principal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jwt import PyJWK

    from auth.config import OidcSettings

_HTTP_OK = 200
_JSON_OBJECT = dict[str, object]
_ASCII_CONTROL_END = 0x1F
_ASCII_SPACE = 0x20
_ASCII_DELETE = 0x7F
_ALGORITHM_HASHES: Mapping[str, Callable[[bytes], Any]] = {
    "RS256": hashlib.sha256,
    "RS384": hashlib.sha384,
    "RS512": hashlib.sha512,
    "PS256": hashlib.sha256,
    "PS384": hashlib.sha384,
    "PS512": hashlib.sha512,
    "ES256": hashlib.sha256,
    "ES384": hashlib.sha384,
    "ES512": hashlib.sha512,
}
_ALGORITHM_KEY_REQUIREMENTS: Mapping[str, tuple[str, str | None]] = {
    "RS256": ("RSA", None),
    "RS384": ("RSA", None),
    "RS512": ("RSA", None),
    "PS256": ("RSA", None),
    "PS384": ("RSA", None),
    "PS512": ("RSA", None),
    "ES256": ("EC", "P-256"),
    "ES384": ("EC", "P-384"),
    "ES512": ("EC", "P-521"),
}


def _jwk_allows_algorithm(raw_key: Mapping[str, object], algorithm: str) -> bool:
    """Enforce declared JWK restrictions and compatible asymmetric key material."""
    requirements = _ALGORITHM_KEY_REQUIREMENTS.get(algorithm)
    if requirements is None:
        return False
    key_type, curve = requirements
    return (
        raw_key.get("alg", algorithm) == algorithm
        and raw_key.get("kty") == key_type
        and (curve is None or raw_key.get("crv") == curve)
    )


def _utc_now() -> datetime:
    """Return the current aware UTC time."""
    return datetime.now(UTC)


class OidcProviderError(Exception):
    """Represent a safe OIDC discovery, token, validation, or refresh failure."""


@dataclass(frozen=True, slots=True)
class OidcDiscovery:
    """Validated provider endpoints used only by the FastAPI BFF."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    revocation_endpoint: str | None


@dataclass(frozen=True, slots=True)
class OidcTokenResult:
    """Validated initial credentials and stable provider identity."""

    principal: Principal
    issuer: str
    subject: str
    credentials: ProviderCredentialState


class OidcProvider:
    """Own discovery, signing keys, token exchange, validation, and refresh."""

    def __init__(
        self,
        settings: OidcSettings,
        *,
        client: httpx2.AsyncClient | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Create an unstarted provider client around immutable settings."""
        self.settings = settings
        self._clock = clock
        self._client = client or httpx2.AsyncClient(
            timeout=httpx2.Timeout(settings.http_timeout_seconds),
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._requests = OidcRequestAdapter(settings, self._client)
        self._discovery: OidcDiscovery | None = None
        self._keys: dict[str, _JSON_OBJECT] = {}
        self._keys_loaded_at: datetime | None = None

    @property
    def discovery(self) -> OidcDiscovery:
        """Return startup-validated discovery metadata."""
        if self._discovery is None:
            raise RuntimeError("OIDC provider has not completed discovery")
        return self._discovery

    async def start(self) -> None:
        """Load and validate discovery plus the initial signing-key set."""
        self._discovery = await self._load_discovery()
        await self._reload_keys()

    async def close(self) -> None:
        """Close an internally owned HTTP client without touching injected clients."""
        if self._owns_client:
            await self._client.aclose()

    def authorization_url(self, correlation_state: str, nonce: str, verifier: str) -> str:
        """Build a confined Authorization Code + PKCE S256 provider redirect."""
        return self._requests.authorization_url(
            self.discovery.authorization_endpoint, correlation_state, nonce, verifier
        )

    async def exchange_code(
        self,
        *,
        code: str,
        verifier: str,
        nonce: str,
    ) -> OidcTokenResult:
        """Exchange one correlated code and validate the returned ID token."""
        if not code or not verifier or not nonce:
            raise OidcProviderError
        payload = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.redirect_uri,
                "code_verifier": verifier,
            }
        )
        access_token, refresh_token, expires_in = _initial_token_fields(payload)
        id_token = _required_string(payload, "id_token")
        claims = await self._verify_id_token(
            id_token,
            nonce=nonce,
            nonce_required=True,
            access_token=access_token,
            authorization_code=code,
        )
        now = self._now()
        refresh_expires_at = _optional_relative_expiry(payload, "refresh_expires_in", now)
        principal, subject = self._principal_from_claims(claims)
        return OidcTokenResult(
            principal=principal,
            issuer=self.settings.issuer,
            subject=subject,
            credentials=ProviderCredentialState(
                access_token=access_token,
                refresh_token=refresh_token,
                access_token_expires_at=_relative_expiry(now, expires_in),
                refresh_token_expires_at=refresh_expires_at,
            ),
        )

    async def authenticate_session(
        self,
        repository: AuthSessionRepository,
        lookup_token: str,
    ) -> AuthenticatedRequest | None:
        """Resolve and refresh one opaque cookie session without changing ownership."""
        session = repository.get_session(lookup_token)
        if session is None:
            return None
        try:
            session = await self._refresh_if_needed(repository, session)
        except OidcProviderError:
            repository.delete_session(lookup_token)
            return None
        return AuthenticatedRequest(
            principal=session.principal,
            owner_context=session.owner_context,
            csrf_token=session.csrf_token,
            session_lookup_token=session.lookup_token,
        )

    async def revoke(self, session: AuthenticatedSession) -> None:
        """Best-effort revoke refresh then access credentials after local logout."""
        endpoint = self.discovery.revocation_endpoint
        if endpoint is None:
            return
        credentials = session.provider_credentials
        for token, hint in (
            (credentials.refresh_token, "refresh_token"),
            (credentials.access_token, "access_token"),
        ):
            try:
                await self._requests.post_form(
                    endpoint,
                    {"token": token, "token_type_hint": hint},
                )
            except httpx2.HTTPError:
                continue

    async def _refresh_if_needed(
        self,
        repository: AuthSessionRepository,
        session: AuthenticatedSession,
    ) -> AuthenticatedSession:
        now = self._now()
        if session.authentication_expires_at <= now:
            raise OidcProviderError
        credentials = session.provider_credentials
        if credentials.access_token_expires_at > now + timedelta(
            seconds=self.settings.refresh_leeway_seconds
        ):
            return session
        async with session.refresh_lock:
            current = repository.get_session(session.lookup_token)
            if current is None:
                raise OidcProviderError
            now = self._now()
            if current.authentication_expires_at <= now:
                raise OidcProviderError
            current_credentials = current.provider_credentials
            if current_credentials.access_token_expires_at > now + timedelta(
                seconds=self.settings.refresh_leeway_seconds
            ):
                return current
            operation = asyncio.create_task(self._refresh_and_publish(repository, current))
            # A success-only signal avoids Python 3.14 shield logging an inner
            # exception before we retrieve it after caller cancellation.
            settled: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            operation.add_done_callback(lambda _operation: settled.set_result(None))
            cancellation_requested = False
            try:
                while not operation.done():
                    try:
                        await asyncio.shield(settled)
                    except asyncio.CancelledError:
                        # Keep the session lock until rotation and publication
                        # settle, including when the caller is cancelled again.
                        cancellation_requested = True
                operation.result()
                # Publication and resuming this request are separate loop turns;
                # logout or expiry may have won after the protected task ended.
                updated = repository.get_session(current.lookup_token)
                if updated is None:
                    raise OidcProviderError
                return updated
            finally:
                if cancellation_requested:
                    # Observe failures even when cancellation wins. The protected
                    # transaction invalidates unusable credentials before unlock.
                    if not operation.cancelled():
                        operation.exception()
                    raise asyncio.CancelledError from None

    async def _refresh_and_publish(
        self,
        repository: AuthSessionRepository,
        current: AuthenticatedSession,
    ) -> AuthenticatedSession:
        """Settle one refresh while its caller retains the session's lock."""
        try:
            refreshed_principal, refreshed_credentials = await self._refresh(current)
            updated = repository.replace_provider_credentials(
                current.lookup_token,
                principal=refreshed_principal,
                provider_credentials=refreshed_credentials,
            )
            if updated is None:
                raise OidcProviderError
            if updated.authentication_expires_at <= self._now():
                raise OidcProviderError
            return updated
        except Exception, asyncio.CancelledError:
            # Rotation may already have consumed the old token. Invalidate on
            # any unsuccessful transaction before a waiting request can retry it.
            repository.delete_session(current.lookup_token)
            raise

    async def _refresh(
        self,
        session: AuthenticatedSession,
    ) -> tuple[Principal, ProviderCredentialState]:
        now = self._now()
        current = session.provider_credentials
        if current.refresh_token_expires_at is not None and current.refresh_token_expires_at <= now:
            raise OidcProviderError
        payload = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token,
            }
        )
        access_token = _required_string(payload, "access_token")
        _require_bearer(payload)
        expires_in = _positive_seconds(payload, "expires_in")
        refresh_token = _optional_string(payload, "refresh_token") or current.refresh_token
        refresh_expires_at = _optional_relative_expiry(payload, "refresh_expires_in", now)
        if refresh_expires_at is None:
            refresh_expires_at = current.refresh_token_expires_at

        id_token = _optional_string(payload, "id_token")
        principal = session.principal
        if id_token is not None:
            claims = await self._verify_id_token(
                id_token,
                nonce=session.nonce,
                nonce_required=False,
                access_token=access_token,
                authorization_code=None,
            )
            refreshed_principal, subject = self._principal_from_claims(claims)
            if subject != session.subject or self.settings.issuer != session.issuer:
                raise OidcProviderError
            principal = refreshed_principal

        return (
            principal,
            ProviderCredentialState(
                access_token=access_token,
                refresh_token=refresh_token,
                access_token_expires_at=_relative_expiry(now, expires_in),
                refresh_token_expires_at=refresh_expires_at,
            ),
        )

    async def _load_discovery(self) -> OidcDiscovery:
        payload = await self._get_json(self.settings.discovery_url)
        issuer = _required_string(payload, "issuer")
        if issuer != self.settings.issuer:
            raise OidcProviderError
        authorization_endpoint = self._provider_endpoint(payload, "authorization_endpoint")
        token_endpoint = self._provider_endpoint(payload, "token_endpoint")
        jwks_uri = self._provider_endpoint(payload, "jwks_uri")
        revocation_endpoint = self._optional_provider_endpoint(payload, "revocation_endpoint")
        _require_supported(payload, "response_types_supported", "code")
        _require_supported(payload, "code_challenge_methods_supported", "S256")
        _require_supported(
            payload,
            "token_endpoint_auth_methods_supported",
            "client_secret_basic",
        )
        return OidcDiscovery(
            issuer=issuer,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            jwks_uri=jwks_uri,
            revocation_endpoint=revocation_endpoint,
        )

    async def _get_json(self, url: str) -> _JSON_OBJECT:
        try:
            response = await self._client.get(
                url,
                headers={"Accept": "application/json"},
                timeout=self.settings.http_timeout_seconds,
            )
        except httpx2.HTTPError as exc:
            raise OidcProviderError from exc
        return _response_json(response)

    async def _token_request(self, data: Mapping[str, str]) -> _JSON_OBJECT:
        try:
            response = await self._requests.post_form(
                self.discovery.token_endpoint,
                data,
            )
        except httpx2.HTTPError as exc:
            raise OidcProviderError from exc
        return _response_json(response)

    async def _reload_keys(self) -> None:
        payload = await self._get_json(self.discovery.jwks_uri)
        raw_keys = payload.get("keys")
        if not isinstance(raw_keys, list) or not raw_keys:
            raise OidcProviderError
        keys: dict[str, _JSON_OBJECT] = {}
        try:
            for raw_key in raw_keys:
                if not isinstance(raw_key, dict):
                    raise OidcProviderError
                kid = raw_key.get("kid")
                if not isinstance(kid, str) or not kid or kid in keys:
                    raise OidcProviderError
                if raw_key.get("use") not in {None, "sig"}:
                    continue
                key_ops = raw_key.get("key_ops")
                if key_ops is not None and (
                    not isinstance(key_ops, list) or "verify" not in key_ops
                ):
                    continue
                compatible_algorithm = next(
                    (
                        algorithm
                        for algorithm in self.settings.accepted_algorithms
                        if _jwk_allows_algorithm(raw_key, algorithm)
                    ),
                    None,
                )
                if compatible_algorithm is None:
                    continue
                # Validate material at startup, but retain the provider's raw
                # restrictions rather than PyJWT's inferred default algorithm.
                jwt.PyJWK.from_dict(
                    cast("dict[str, Any]", raw_key),
                    algorithm=compatible_algorithm,
                )
                keys[kid] = raw_key
        except jwt.PyJWTError, ValueError, TypeError:
            # PyJWT key errors can include the raw provider JWK.
            raise OidcProviderError from None
        if not keys:
            raise OidcProviderError
        self._keys = keys
        self._keys_loaded_at = self._now()

    async def _signing_key(self, token: str) -> tuple[str, PyJWK]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OidcProviderError from exc
        algorithm = header.get("alg")
        kid = header.get("kid")
        if (
            not isinstance(algorithm, str)
            or algorithm not in self.settings.accepted_algorithms
            or not isinstance(kid, str)
            or not kid
        ):
            raise OidcProviderError
        loaded_at = self._keys_loaded_at
        if (
            loaded_at is None
            or loaded_at + timedelta(seconds=self.settings.jwks_cache_seconds) <= self._now()
        ):
            await self._reload_keys()
        raw_key = self._keys.get(kid)
        if raw_key is None:
            await self._reload_keys()
            raw_key = self._keys.get(kid)
        if raw_key is None or not _jwk_allows_algorithm(raw_key, algorithm):
            raise OidcProviderError
        try:
            key = jwt.PyJWK.from_dict(cast("dict[str, Any]", raw_key), algorithm=algorithm)
        except jwt.PyJWTError, ValueError, TypeError:
            raise OidcProviderError from None
        return algorithm, key

    async def _verify_id_token(
        self,
        token: str,
        *,
        nonce: str,
        nonce_required: bool,
        access_token: str,
        authorization_code: str | None,
    ) -> _JSON_OBJECT:
        algorithm, key = await self._signing_key(token)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self.settings.client_id,
                issuer=self.settings.issuer,
                options={
                    "require": ["iss", "sub", "aud", "exp", "iat"],
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.PyJWTError as exc:
            raise OidcProviderError from exc
        validated = cast("_JSON_OBJECT", claims)
        self._validate_time_claims(validated)
        self._validate_audience(validated)
        token_nonce = validated.get("nonce")
        if nonce_required:
            if not isinstance(token_nonce, str) or token_nonce != nonce:
                raise OidcProviderError
        elif token_nonce is not None and (not isinstance(token_nonce, str) or token_nonce != nonce):
            raise OidcProviderError
        _validate_hash_claim(validated, "at_hash", access_token, algorithm)
        if authorization_code is not None:
            _validate_hash_claim(validated, "c_hash", authorization_code, algorithm)
        return validated

    def _validate_time_claims(self, claims: Mapping[str, object]) -> None:
        now = self._now().timestamp()
        skew = self.settings.clock_skew_seconds
        expiry = _numeric_date(claims, "exp")
        issued_at = _numeric_date(claims, "iat")
        if expiry <= now - skew or issued_at > now + skew or expiry <= issued_at:
            raise OidcProviderError
        if "nbf" in claims and _numeric_date(claims, "nbf") > now + skew:
            raise OidcProviderError

    def _validate_audience(self, claims: Mapping[str, object]) -> None:
        audience = claims.get("aud")
        audiences: tuple[str, ...]
        if isinstance(audience, str):
            audiences = (audience,)
        elif (
            isinstance(audience, list)
            and audience
            and all(isinstance(value, str) and value for value in audience)
        ):
            audiences = tuple(cast("list[str]", audience))
        else:
            raise OidcProviderError
        if self.settings.client_id not in audiences:
            raise OidcProviderError
        authorized_party = claims.get("azp")
        if len(audiences) > 1 and authorized_party is None:
            raise OidcProviderError
        if authorized_party is not None and authorized_party != self.settings.client_id:
            raise OidcProviderError

    def _principal_from_claims(self, claims: Mapping[str, object]) -> tuple[Principal, str]:
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise OidcProviderError
        roles = self._mapped_roles(claims.get(self.settings.role_claim))
        principal = Principal(
            principal_id=oidc_principal_id(self.settings.issuer, subject),
            roles=roles,
            auth_mode=AuthMode.OIDC,
        )
        return principal, subject

    def _mapped_roles(self, claim: object) -> frozenset[AppRole]:
        values: tuple[str, ...]
        if isinstance(claim, str):
            values = (claim,)
        elif isinstance(claim, list) and all(isinstance(value, str) for value in claim):
            values = tuple(cast("list[str]", claim))
        else:
            return frozenset()
        roles: set[AppRole] = set()
        for value in values:
            roles.update(self.settings.role_mapping.get(value, ()))
        return frozenset(roles)

    def _provider_endpoint(self, payload: Mapping[str, object], name: str) -> str:
        return self._validate_provider_url(_required_string(payload, name))

    def _optional_provider_endpoint(self, payload: Mapping[str, object], name: str) -> str | None:
        value = _optional_string(payload, name)
        return None if value is None else self._validate_provider_url(value)

    def _validate_provider_url(self, value: str) -> str:
        parsed = urlsplit(value)
        try:
            _ = parsed.port
        except ValueError as exc:
            raise OidcProviderError from exc
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(
                ord(character) <= _ASCII_SPACE or ord(character) == _ASCII_DELETE
                for character in value
            )
        ):
            raise OidcProviderError
        if parsed.scheme == "http":
            configured_issuer = urlsplit(self.settings.issuer)
            if configured_issuer.scheme != "http" or parsed.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise OidcProviderError
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("OIDC clock must be timezone-aware")
        return value.astimezone(UTC)


def oidc_principal_id(issuer: str, subject: str) -> str:
    """Derive a non-reversible, collision-safe ID from verified OIDC identity."""
    digest = hashlib.sha256()
    digest.update(b"rabbitmq-topology-oidc-principal-v1\x00")
    for value in (issuer, subject):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"oidc:{digest.hexdigest()}"


def validate_return_to(value: str | None) -> str:
    """Accept only a confined origin-relative non-API application target."""
    if value is None:
        return "/"
    if (
        not value.startswith("/")
        or value.startswith("//")
        or value.startswith("/api")
        or "\\" in value
        or "#" in value
        or any(
            ord(character) <= _ASCII_CONTROL_END or ord(character) == _ASCII_DELETE
            for character in value
        )
    ):
        raise ValueError("return_to must be a confined application path")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("return_to must be a confined application path")
    return value


def _response_json(response: httpx2.Response) -> _JSON_OBJECT:
    if response.status_code != _HTTP_OK:
        raise OidcProviderError

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise OidcProviderError
            result[key] = value
        return result

    try:
        payload = json.loads(
            response.text,
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(OidcProviderError()),
        )
    except (ValueError, RecursionError) as exc:
        # JSON syntax/encoding errors, integer digit limits, and nesting limits
        # are all invalid provider material. Hook-raised provider errors propagate.
        raise OidcProviderError from exc
    if not isinstance(payload, dict):
        raise OidcProviderError
    return cast("_JSON_OBJECT", payload)


def _required_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise OidcProviderError
    return value


def _optional_string(payload: Mapping[str, object], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise OidcProviderError
    return value


def _positive_seconds(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OidcProviderError
    return value


def _optional_relative_expiry(
    payload: Mapping[str, object], name: str, now: datetime
) -> datetime | None:
    if name not in payload:
        return None
    return _relative_expiry(now, _positive_seconds(payload, name))


def _relative_expiry(now: datetime, seconds: int) -> datetime:
    """Convert a validated duration without leaking arithmetic overflow."""
    try:
        return now + timedelta(seconds=seconds)
    except OverflowError as exc:
        raise OidcProviderError from exc


def _require_bearer(payload: Mapping[str, object]) -> None:
    token_type = payload.get("token_type")
    if not isinstance(token_type, str) or token_type.casefold() != "bearer":
        raise OidcProviderError


def _initial_token_fields(payload: Mapping[str, object]) -> tuple[str, str, int]:
    access_token = _required_string(payload, "access_token")
    refresh_token = _required_string(payload, "refresh_token")
    _require_bearer(payload)
    return access_token, refresh_token, _positive_seconds(payload, "expires_in")


def _numeric_date(claims: Mapping[str, object], name: str) -> float:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OidcProviderError
    try:
        numeric_date = float(value)
    except OverflowError as exc:
        raise OidcProviderError from exc
    if not math.isfinite(numeric_date):
        raise OidcProviderError
    return numeric_date


def _require_supported(payload: Mapping[str, object], name: str, required: str) -> None:
    value = payload.get(name)
    if value is None:
        return
    if not isinstance(value, list) or required not in value:
        raise OidcProviderError


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _validate_hash_claim(
    claims: Mapping[str, object],
    name: str,
    source: str,
    algorithm: str,
) -> None:
    value = claims.get(name)
    if value is None:
        return
    if not isinstance(value, str) or not value.isascii():
        raise OidcProviderError
    digest_factory = _ALGORITHM_HASHES.get(algorithm)
    if digest_factory is None:
        raise OidcProviderError
    try:
        encoded_source = source.encode("ascii")
    except UnicodeEncodeError as exc:
        raise OidcProviderError from exc
    digest = digest_factory(encoded_source).digest()
    expected = _base64url(digest[: len(digest) // 2])
    if not hmac.compare_digest(value, expected):
        raise OidcProviderError

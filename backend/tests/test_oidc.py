import asyncio
import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote_plus, urlsplit

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from auth.config import OidcSettings
from auth.oidc import OidcProvider, OidcProviderError, oidc_principal_id, validate_return_to
from auth.sessions import AuthSessionRepository
from core.security import AppRole, AuthMode

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from auth.sessions import AuthenticatedSession

ISSUER = "http://127.0.0.1"
NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)
CLIENT_ID = "topology-client"
CLIENT_SECRET = "provider-client-secret"
MALFORMED_PROVIDER_JSON = {
    "integer_limit": b'{"private":"provider-payload-secret","expires_in":' + b"9" * 5000 + b"}",
    "nesting_limit": b'{"private":"provider-payload-secret","nested":'
    + b"[" * 100_000
    + b"0"
    + b"]" * 100_000
    + b"}",
    "invalid_json": b'{"private":"provider-payload-secret",',
    "duplicate_keys": b'{"private":"provider-payload-secret","expires_in":1,"expires_in":2}',
    "non_finite_json": b'{"private":"provider-payload-secret","expires_in":NaN}',
}


def _base64url_int(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode()


def _jwk(
    private_key: rsa.RSAPrivateKey,
    kid: str,
    *,
    algorithm: str | None = "RS256",
) -> dict[str, object]:
    numbers = private_key.public_key().public_numbers()
    result: dict[str, object] = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "n": _base64url_int(numbers.n),
        "e": _base64url_int(numbers.e),
    }
    if algorithm is not None:
        result["alg"] = algorithm
    return result


@dataclass(slots=True)
class FakeOidcProvider:
    app: FastAPI = field(init=False, repr=False)
    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    now: datetime = NOW
    subject: str = "provider-user-1"
    roles: object = field(default_factory=lambda: ["analysts"])
    claim_overrides: dict[str, object] = field(default_factory=dict)
    initial_token_overrides: dict[str, object] = field(default_factory=dict)
    refresh_token_overrides: dict[str, object] = field(default_factory=dict)
    initial_token_body: bytes | None = None
    refresh_token_body: bytes | None = None
    discovery_overrides: dict[str, object] = field(default_factory=dict)
    token_algorithm: str = "RS256"
    jwk_algorithm: str | None = "RS256"
    jwk_overrides: dict[str, object] = field(default_factory=dict)
    expected_nonce: str = "login-nonce"
    expected_verifier: str = "pkce-verifier"
    key_a: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
        repr=False,
    )
    key_b: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
        repr=False,
    )
    active_kid: str = "kid-a"
    published_kids: list[str] = field(default_factory=lambda: ["kid-a"])
    discovery_calls: int = 0
    jwks_calls: int = 0
    token_calls: list[dict[str, str]] = field(default_factory=list)
    revocations: list[str] = field(default_factory=list)
    refresh_subject: str | None = None
    refresh_roles: object | None = None
    include_refresh_id_token: bool = True

    def __post_init__(self) -> None:
        self.app = FastAPI()

        @self.app.get("/.well-known/openid-configuration")
        async def discovery() -> dict[str, object]:
            self.discovery_calls += 1
            payload: dict[str, object] = {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks",
                "revocation_endpoint": f"{ISSUER}/revoke",
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            }
            payload.update(self.discovery_overrides)
            return payload

        @self.app.get("/jwks")
        async def jwks() -> dict[str, object]:
            self.jwks_calls += 1
            keys = {
                "kid-a": _jwk(self.key_a, "kid-a", algorithm=self.jwk_algorithm),
                "kid-b": _jwk(self.key_b, "kid-b", algorithm=self.jwk_algorithm),
            }
            return {"keys": [keys[kid] | self.jwk_overrides for kid in self.published_kids]}

        @self.app.post("/token")
        async def token(request: Request) -> Response:
            if not self._valid_client_auth(request):
                return JSONResponse(status_code=401, content={"error": "invalid_client"})
            form = await request.form()
            values = {str(key): str(value) for key, value in form.multi_items()}
            self.token_calls.append(values)
            grant_type = values.get("grant_type")
            if grant_type == "authorization_code":
                if (
                    values.get("code") != "one-time-code"
                    or values.get("redirect_uri") != f"{ISSUER}/api/auth/oidc/callback"
                    or values.get("code_verifier") != self.expected_verifier
                ):
                    return JSONResponse(status_code=400, content={"error": "invalid_grant"})
                return (
                    Response(content=self.initial_token_body, media_type="application/json")
                    if self.initial_token_body is not None
                    else JSONResponse(content=self._initial_tokens())
                )
            if grant_type == "refresh_token" and values.get("refresh_token") == "refresh-1":
                return (
                    Response(content=self.refresh_token_body, media_type="application/json")
                    if self.refresh_token_body is not None
                    else JSONResponse(content=self._refresh_tokens())
                )
            return JSONResponse(status_code=400, content={"error": "invalid_grant"})

        @self.app.post("/revoke")
        async def revoke(request: Request) -> JSONResponse:
            if not self._valid_client_auth(request):
                return JSONResponse(status_code=401, content={"error": "invalid_client"})
            form = await request.form()
            self.revocations.append(str(form["token"]))
            return JSONResponse(content={})

    def _valid_client_auth(self, request: Request) -> bool:
        """Independently decode OAuth client_secret_basic before comparing credentials."""
        scheme, _, encoded = request.headers.get("Authorization", "").partition(" ")
        if scheme != "Basic":
            return False
        try:
            credentials = base64.b64decode(encoded, validate=True).decode("ascii")
            client_id, separator, client_secret = credentials.partition(":")
            if not separator or any(
                re.fullmatch(r"(?:[A-Za-z0-9._~*+-]|%[0-9A-Fa-f]{2})*", value) is None
                for value in (client_id, client_secret)
            ):
                return False
            return (
                unquote_plus(client_id, errors="strict") == self.client_id
                and unquote_plus(client_secret, errors="strict") == self.client_secret
            )
        except binascii.Error, UnicodeError, ValueError:
            return False

    def _private_key(self) -> rsa.RSAPrivateKey:
        return self.key_a if self.active_kid == "kid-a" else self.key_b

    def _id_token(
        self,
        *,
        subject: str,
        roles: object,
        nonce: str | None,
        access_token: str,
        code: str | None,
    ) -> str:
        claims: dict[str, object] = {
            "iss": ISSUER,
            "sub": subject,
            "aud": self.client_id,
            "iat": int(self.now.timestamp()),
            "exp": int((self.now + timedelta(minutes=10)).timestamp()),
            "groups": roles,
        }
        if nonce is not None:
            claims["nonce"] = nonce
        hash_function = {
            "256": hashlib.sha256,
            "384": hashlib.sha384,
            "512": hashlib.sha512,
        }.get(self.token_algorithm[-3:], hashlib.sha256)
        digest = hash_function(access_token.encode()).digest()
        claims["at_hash"] = (
            base64.urlsafe_b64encode(digest[: len(digest) // 2]).rstrip(b"=").decode()
        )
        if code is not None:
            code_digest = hash_function(code.encode()).digest()
            claims["c_hash"] = (
                base64.urlsafe_b64encode(code_digest[: len(code_digest) // 2]).rstrip(b"=").decode()
            )
        claims.update(self.claim_overrides)
        if self.token_algorithm == "HS256":
            return jwt.encode(
                claims,
                "provider-controlled-symmetric-key",
                algorithm="HS256",
                headers={"kid": self.active_kid},
            )
        if self.token_algorithm == "none":
            return jwt.encode(claims, None, algorithm="none", headers={"kid": self.active_kid})
        return jwt.encode(
            claims,
            self._private_key(),
            algorithm=self.token_algorithm,
            headers={"kid": self.active_kid},
        )

    def _initial_tokens(self) -> dict[str, object]:
        access_token = "access-1"
        result: dict[str, object] = {
            "token_type": "Bearer",
            "access_token": access_token,
            "refresh_token": "refresh-1",
            "expires_in": 30,
            "refresh_expires_in": 3600,
            "id_token": self._id_token(
                subject=self.subject,
                roles=self.roles,
                nonce=self.expected_nonce,
                access_token=access_token,
                code="one-time-code",
            ),
        }
        result.update(self.initial_token_overrides)
        return result

    def _refresh_tokens(self) -> dict[str, object]:
        access_token = "access-2"
        result: dict[str, object] = {
            "token_type": "Bearer",
            "access_token": access_token,
            "refresh_token": "refresh-2",
            "expires_in": 600,
            "refresh_expires_in": 3600,
        }
        if self.include_refresh_id_token:
            result["id_token"] = self._id_token(
                subject=self.refresh_subject or self.subject,
                roles=self.roles if self.refresh_roles is None else self.refresh_roles,
                nonce=self.expected_nonce,
                access_token=access_token,
                code=None,
            )
        result.update(self.refresh_token_overrides)
        return result


def _settings(*, client_id: str = CLIENT_ID, client_secret: str = CLIENT_SECRET) -> OidcSettings:
    return OidcSettings(
        issuer=ISSUER,
        discovery_url=f"{ISSUER}/.well-known/openid-configuration",
        client_id=client_id,
        client_secret_file=Path(__file__),
        client_secret=client_secret,
        redirect_uri=f"{ISSUER}/api/auth/oidc/callback",
        scopes=("openid", "offline_access"),
        role_claim="groups",
        role_mapping_file=Path(__file__),
        role_mapping=MappingProxyType(
            {
                "viewers": frozenset({AppRole.VIEWER}),
                "analysts": frozenset({AppRole.ANALYST}),
            }
        ),
        accepted_algorithms=("RS256",),
        clock_skew_seconds=60,
        http_timeout_seconds=5,
        refresh_leeway_seconds=60,
        session_lifetime_seconds=28_800,
        session_limit=16,
        login_ttl_seconds=600,
        login_limit=16,
        jwks_cache_seconds=300,
        cookie_name="test_session",
        cookie_secure=False,
    )


def _provider(
    fake: FakeOidcProvider,
    *,
    accepted_algorithms: tuple[str, ...] = ("RS256",),
) -> tuple[OidcProvider, httpx2.AsyncClient]:
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=fake.app),
        base_url=ISSUER,
    )
    settings = replace(
        _settings(client_id=fake.client_id, client_secret=fake.client_secret),
        accepted_algorithms=accepted_algorithms,
    )
    return OidcProvider(settings, client=client, clock=lambda: fake.now), client


@pytest.mark.parametrize(
    "endpoint_query",
    ["", "tenant=one", "tenant=one&tenant=two&hint=a%2Bb+%25&empty="],
    ids=["no-query", "tenant-query", "repeated-and-encoded-query"],
)
def test_discovery_login_exchange_and_claim_mapping_are_server_owned(endpoint_query: str) -> None:
    async def run() -> None:
        endpoint = f"{ISSUER}/authorize" + (f"?{endpoint_query}" if endpoint_query else "")
        fake = FakeOidcProvider(discovery_overrides={"authorization_endpoint": endpoint})
        provider, client = _provider(fake)
        try:
            await provider.start()
            url = provider.authorization_url("state-secret", "login-nonce", "pkce-verifier")
            parsed = urlsplit(url)
            assert parsed.scheme == "http"
            assert parsed.netloc == "127.0.0.1"
            assert parsed.path == "/authorize"
            assert not parsed.fragment
            assert url.count("?") == 1
            if endpoint_query:
                assert parsed.query.startswith(f"{endpoint_query}&")
            query = parse_qs(parsed.query, keep_blank_values=True)
            assert query == parse_qs(endpoint_query, keep_blank_values=True) | {
                "client_id": [CLIENT_ID],
                "redirect_uri": [f"{ISSUER}/api/auth/oidc/callback"],
                "response_type": ["code"],
                "scope": ["openid offline_access"],
                "state": ["state-secret"],
                "nonce": ["login-nonce"],
                "code_challenge": [
                    base64.urlsafe_b64encode(hashlib.sha256(b"pkce-verifier").digest())
                    .rstrip(b"=")
                    .decode()
                ],
                "code_challenge_method": ["S256"],
            }

            result = await provider.exchange_code(
                code="one-time-code",
                verifier="pkce-verifier",
                nonce="login-nonce",
            )

            assert result.principal.auth_mode is AuthMode.OIDC
            assert result.principal.roles == frozenset({AppRole.ANALYST})
            assert result.principal.principal_id == oidc_principal_id(ISSUER, fake.subject)
            assert result.subject == fake.subject
            assert result.credentials.access_token == "access-1"
            assert fake.discovery_calls == 1
            assert fake.jwks_calls == 1
            assert fake.token_calls[0]["code_verifier"] == "pkce-verifier"
        finally:
            await client.aclose()

    asyncio.run(run())


def test_unknown_kid_forces_jwks_reload_for_provider_key_rotation() -> None:
    async def run() -> None:
        fake = FakeOidcProvider()
        provider, client = _provider(fake)
        try:
            await provider.start()
            fake.active_kid = "kid-b"
            fake.published_kids = ["kid-a", "kid-b"]

            result = await provider.exchange_code(
                code="one-time-code",
                verifier="pkce-verifier",
                nonce="login-nonce",
            )

            assert result.subject == fake.subject
            assert fake.jwks_calls == 2
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("algorithm", ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512"])
@pytest.mark.parametrize("alongside_rs256", [False, True])
@pytest.mark.parametrize("explicit_jwk_alg", [False, True])
def test_configured_rsa_algorithms_accept_optional_jwk_alg(
    algorithm: str,
    alongside_rs256: bool,
    explicit_jwk_alg: bool,
) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(
            token_algorithm=algorithm,
            jwk_algorithm=algorithm if explicit_jwk_alg else None,
            jwk_overrides={"key_ops": ["verify"]},
        )
        accepted = (
            ("RS256", algorithm) if alongside_rs256 and algorithm != "RS256" else (algorithm,)
        )
        provider, client = _provider(fake, accepted_algorithms=accepted)
        try:
            await provider.start()
            result = await provider.exchange_code(
                code="one-time-code",
                verifier="pkce-verifier",
                nonce="login-nonce",
            )
            assert result.subject == fake.subject
            assert result.principal.roles == frozenset({AppRole.ANALYST})
            assert result.credentials.access_token == "access-1"
            assert provider.settings.accepted_algorithms == accepted
            assert fake.discovery_calls == 1
            assert fake.jwks_calls == 1
            assert len(fake.token_calls) == 1
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("rotate_key", [False, True])
def test_alg_less_jwk_supports_refresh_algorithm_change_and_key_rotation(rotate_key: bool) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(jwk_algorithm=None, roles=["viewers"], refresh_roles=["analysts"])
        provider, client = _provider(fake, accepted_algorithms=("RS256", "PS256"))
        repository = AuthSessionRepository(clock=lambda: fake.now)
        try:
            await provider.start()
            session = await _establish_provider_session(provider, repository)
            owner = session.owner_context
            fake.token_algorithm = "PS256"
            if rotate_key:
                fake.active_kid = "kid-b"
                fake.published_kids = ["kid-a", "kid-b"]
            fake.now += timedelta(seconds=1)

            authentication = await provider.authenticate_session(repository, session.lookup_token)

            assert authentication is not None
            assert authentication.owner_context == owner
            assert authentication.principal.roles == frozenset({AppRole.ANALYST})
            refreshed = repository.get_session(session.lookup_token)
            assert refreshed is not None
            assert refreshed.provider_credentials.access_token == "access-2"
            assert refreshed.provider_credentials.refresh_token == "refresh-2"
            assert fake.jwks_calls == (2 if rotate_key else 1)
            assert [call["grant_type"] for call in fake.token_calls] == [
                "authorization_code",
                "refresh_token",
            ]
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(("declared", "signed"), [("RS256", "PS256"), ("PS256", "RS256")])
def test_explicit_jwk_alg_conflict_is_rejected_even_when_both_algorithms_are_configured(
    declared: str,
    signed: str,
) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(token_algorithm=signed, jwk_algorithm=declared)
        provider, client = _provider(fake, accepted_algorithms=("RS256", "PS256"))
        try:
            await provider.start()
            with pytest.raises(OidcProviderError):
                await provider.exchange_code(
                    code="one-time-code",
                    verifier="pkce-verifier",
                    nonce="login-nonce",
                )
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("algorithm", ["PS256", "RS384", "PS512", "HS256", "none"])
def test_alg_less_jwk_rejects_unconfigured_or_symmetric_token_algorithms(algorithm: str) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(token_algorithm=algorithm, jwk_algorithm=None)
        provider, client = _provider(fake)
        try:
            await provider.start()
            with pytest.raises(OidcProviderError):
                await provider.exchange_code(
                    code="one-time-code",
                    verifier="pkce-verifier",
                    nonce="login-nonce",
                )
            assert fake.jwks_calls == 1
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("wrong_kid", [False, True])
def test_alg_less_rsa_jwk_requires_the_matching_kid_and_valid_signature(wrong_kid: bool) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(token_algorithm="PS256", jwk_algorithm=None)
        if wrong_kid:
            fake.active_kid = "kid-b"
        else:
            fake.jwk_overrides = _jwk(fake.key_b, "kid-a", algorithm=None)
        provider, client = _provider(fake, accepted_algorithms=("PS256",))
        try:
            await provider.start()
            with pytest.raises(OidcProviderError):
                await provider.exchange_code(
                    code="one-time-code",
                    verifier="pkce-verifier",
                    nonce="login-nonce",
                )
            assert fake.jwks_calls == (2 if wrong_kid else 1)
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "overrides",
    [
        {"use": "enc"},
        {"key_ops": ["sign"]},
        {"key_ops": ["encrypt"]},
        {"key_ops": "verify"},
        {"alg": "HS256"},
        {"alg": None},
        {"alg": ""},
        {"kty": "oct", "k": "c2VjcmV0"},
        {"n": ""},
    ],
)
def test_jwk_restrictions_and_malformed_keys_are_not_treated_as_optional_alg(
    overrides: dict[str, object],
) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(jwk_algorithm=None, jwk_overrides=overrides)
        provider, client = _provider(fake, accepted_algorithms=("RS256", "PS256"))
        try:
            with pytest.raises(OidcProviderError):
                await provider.start()
            assert fake.token_calls == []
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("algorithm", "valid"),
    [("ES256", True), ("ES384", False), ("PS256", False)],
)
@pytest.mark.parametrize("explicit_jwk_alg", [False, True])
def test_ec_jwk_retains_key_type_and_curve_constraints(
    algorithm: str,
    valid: bool,
    explicit_jwk_alg: bool,
) -> None:
    async def run() -> None:
        public_key = ec.generate_private_key(ec.SECP256R1()).public_key()
        raw_key = jwt.algorithms.ECAlgorithm.to_jwk(public_key, as_dict=True)
        fake = FakeOidcProvider(
            jwk_algorithm=algorithm if explicit_jwk_alg else None,
            jwk_overrides=raw_key,
        )
        provider, client = _provider(fake, accepted_algorithms=(algorithm,))
        try:
            if valid:
                await provider.start()
            else:
                with pytest.raises(OidcProviderError):
                    await provider.start()
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer": "https://wrong-issuer.test"},
        {"response_types_supported": ["implicit"]},
        {"code_challenge_methods_supported": ["plain"]},
        {"token_endpoint_auth_methods_supported": ["client_secret_post"]},
    ],
    ids=["issuer", "code-flow", "pkce-s256", "client-secret-basic"],
)
def test_discovery_rejects_provider_contract_mismatch(overrides: dict[str, object]) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(discovery_overrides=overrides)
        provider, client = _provider(fake)
        try:
            with pytest.raises(OidcProviderError):
                await provider.start()
        finally:
            await client.aclose()

    asyncio.run(run())


def test_token_exchange_rejects_an_invalid_pkce_verifier() -> None:
    async def run() -> None:
        fake = FakeOidcProvider()
        provider, client = _provider(fake)
        try:
            await provider.start()
            with pytest.raises(OidcProviderError):
                await provider.exchange_code(
                    code="one-time-code",
                    verifier="wrong-pkce-verifier",
                    nonce="login-nonce",
                )
            assert fake.token_calls[0]["code_verifier"] == "wrong-pkce-verifier"
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda fake: fake.claim_overrides.update({"iss": "https://wrong-issuer.test"}),
        lambda fake: fake.claim_overrides.update({"aud": "another-client"}),
        lambda fake: fake.claim_overrides.update(
            {"exp": int((fake.now - timedelta(minutes=2)).timestamp())}
        ),
        lambda fake: fake.claim_overrides.update(
            {"iat": int((fake.now + timedelta(minutes=2)).timestamp())}
        ),
        lambda fake: fake.claim_overrides.update(
            {"nbf": int((fake.now + timedelta(minutes=2)).timestamp())}
        ),
        lambda fake: setattr(fake, "expected_nonce", "provider-nonce"),
        lambda fake: setattr(fake, "token_algorithm", "HS256"),
    ],
    ids=["issuer", "audience", "expiry", "issued-at", "not-before", "nonce", "algorithm"],
)
def test_invalid_id_token_contract_is_rejected(
    mutate: Callable[[FakeOidcProvider], object],
) -> None:
    async def run() -> None:
        fake = FakeOidcProvider()
        mutate(fake)
        provider, client = _provider(fake)
        try:
            await provider.start()
            with pytest.raises(OidcProviderError):
                await provider.exchange_code(
                    code="one-time-code",
                    verifier="pkce-verifier",
                    nonce="login-nonce",
                )
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("claim", ["at_hash", "c_hash"])
@pytest.mark.parametrize("value", ["é", "\ud800", 17, "incorrect-ascii-hash"])
def test_invalid_hash_claim_representation_is_a_provider_error(claim: str, value: object) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(claim_overrides={claim: value})
        provider, client = _provider(fake)
        try:
            await provider.start()
            with pytest.raises(OidcProviderError):
                await provider.exchange_code(
                    code="one-time-code", verifier="pkce-verifier", nonce="login-nonce"
                )
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("failure", MALFORMED_PROVIDER_JSON)
def test_malformed_token_response_json_is_a_provider_error(failure: str) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(initial_token_body=MALFORMED_PROVIDER_JSON[failure])
        provider, client = _provider(fake)
        try:
            await provider.start()
            with pytest.raises(OidcProviderError):
                await provider.exchange_code(
                    code="one-time-code", verifier="pkce-verifier", nonce="login-nonce"
                )
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("claim", ["exp", "iat", "nbf"])
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), 10**400],
    ids=["nan", "positive_infinity", "negative_infinity", "float_overflow"],
)
def test_initial_token_rejects_non_finite_or_overflowing_numeric_dates(
    claim: str,
    value: int | float,
) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(claim_overrides={claim: value})
        provider, client = _provider(fake)
        try:
            await provider.start()
            with pytest.raises(OidcProviderError):
                await provider.exchange_code(
                    code="one-time-code",
                    verifier="pkce-verifier",
                    nonce="login-nonce",
                )
        finally:
            await client.aclose()

    asyncio.run(run())


async def _establish_provider_session(
    provider: OidcProvider,
    repository: AuthSessionRepository,
) -> AuthenticatedSession:
    initial = await provider.exchange_code(
        code="one-time-code",
        verifier="pkce-verifier",
        nonce="login-nonce",
    )
    return repository.establish_session(
        principal=initial.principal,
        issuer=initial.issuer,
        subject=initial.subject,
        nonce="login-nonce",
        provider_credentials=initial.credentials,
        provider_absolute_expires_at=initial.credentials.refresh_token_expires_at,
    )


@pytest.mark.parametrize(
    "character", ["+", ":", "%", " ", "é中"], ids=["plus", "colon", "percent", "space", "unicode"]
)
def test_provider_credentials_are_oauth_form_encoded_for_exchange_refresh_and_revocation(
    character: str,
) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(
            client_id=f"client{character}id", client_secret=f"secret{character}value"
        )
        provider, client = _provider(fake)
        repository = AuthSessionRepository(clock=lambda: fake.now)
        try:
            await provider.start()
            url = provider.authorization_url("state-secret", "login-nonce", "pkce-verifier")
            assert parse_qs(urlsplit(url).query)["client_id"] == [fake.client_id]

            session = await _establish_provider_session(provider, repository)
            owner = session.owner_context
            authentication = await provider.authenticate_session(repository, session.lookup_token)
            assert authentication is not None
            assert authentication.owner_context == owner
            refreshed = repository.get_session(session.lookup_token)
            assert refreshed is not None
            assert refreshed.provider_credentials.access_token == "access-2"

            await provider.revoke(refreshed)

            assert [call["grant_type"] for call in fake.token_calls] == [
                "authorization_code",
                "refresh_token",
            ]
            assert all(
                "client_id" not in call and "client_secret" not in call for call in fake.token_calls
            )
            assert fake.revocations == ["refresh-2", "access-2"]
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("claim", ["exp", "iat", "nbf"])
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), 10**400],
    ids=["nan", "positive_infinity", "negative_infinity", "float_overflow"],
)
def test_refresh_rejects_invalid_numeric_dates_and_invalidates_session(
    claim: str,
    value: int | float,
) -> None:
    async def run() -> None:
        fake = FakeOidcProvider()
        provider, client = _provider(fake)
        repository = AuthSessionRepository(clock=lambda: fake.now)
        try:
            await provider.start()
            session = await _establish_provider_session(provider, repository)
            fake.claim_overrides[claim] = value

            assert await provider.authenticate_session(repository, session.lookup_token) is None
            assert repository.get_session(session.lookup_token) is None
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("field_name", ["expires_in", "refresh_expires_in"])
@pytest.mark.parametrize(
    "seconds", [10**30, 2**40], ids=["timedelta_overflow", "datetime_overflow"]
)
def test_initial_token_rejects_unrepresentable_relative_expiry(
    field_name: str,
    seconds: int,
) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(initial_token_overrides={field_name: seconds})
        provider, client = _provider(fake)
        try:
            await provider.start()
            with pytest.raises(OidcProviderError):
                await provider.exchange_code(
                    code="one-time-code",
                    verifier="pkce-verifier",
                    nonce="login-nonce",
                )
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("field_name", ["expires_in", "refresh_expires_in"])
@pytest.mark.parametrize(
    "seconds", [10**30, 2**40], ids=["timedelta_overflow", "datetime_overflow"]
)
def test_refresh_rejects_unrepresentable_relative_expiry_and_invalidates_session(
    field_name: str,
    seconds: int,
) -> None:
    async def run() -> None:
        fake = FakeOidcProvider()
        provider, client = _provider(fake)
        repository = AuthSessionRepository(clock=lambda: fake.now)
        try:
            await provider.start()
            session = await _establish_provider_session(provider, repository)
            fake.refresh_token_overrides[field_name] = seconds

            assert await provider.authenticate_session(repository, session.lookup_token) is None
            assert repository.get_session(session.lookup_token) is None
        finally:
            await client.aclose()

    asyncio.run(run())


def test_refresh_rotates_provider_credentials_roles_without_changing_ownership() -> None:
    async def run() -> None:
        fake = FakeOidcProvider(roles=["viewers"], refresh_roles=["analysts"])
        provider, client = _provider(fake)
        repository = AuthSessionRepository(
            session_lifetime=timedelta(hours=8),
            clock=lambda: fake.now,
        )
        try:
            await provider.start()
            initial = await provider.exchange_code(
                code="one-time-code",
                verifier="pkce-verifier",
                nonce="login-nonce",
            )
            session = repository.establish_session(
                principal=initial.principal,
                issuer=initial.issuer,
                subject=initial.subject,
                nonce="login-nonce",
                provider_credentials=initial.credentials,
                provider_absolute_expires_at=initial.credentials.refresh_token_expires_at,
            )
            owner = session.owner_context
            fake.now += timedelta(seconds=1)

            authentication = await provider.authenticate_session(repository, session.lookup_token)

            assert authentication is not None
            assert authentication.owner_context == owner
            assert authentication.principal.roles == frozenset({AppRole.ANALYST})
            refreshed = repository.get_session(session.lookup_token)
            assert refreshed is not None
            assert refreshed.provider_credentials.access_token == "access-2"
            assert refreshed.provider_credentials.refresh_token == "refresh-2"
            assert refreshed.owner_context == owner
        finally:
            await client.aclose()

    asyncio.run(run())


def test_concurrent_refresh_is_single_flight_and_preserves_one_owner() -> None:
    async def run() -> None:
        fake = FakeOidcProvider()
        provider, client = _provider(fake)
        repository = AuthSessionRepository(clock=lambda: fake.now)
        try:
            await provider.start()
            initial = await provider.exchange_code(
                code="one-time-code",
                verifier="pkce-verifier",
                nonce="login-nonce",
            )
            session = repository.establish_session(
                principal=initial.principal,
                issuer=initial.issuer,
                subject=initial.subject,
                nonce="login-nonce",
                provider_credentials=initial.credentials,
            )
            owner = session.owner_context

            authentications = await asyncio.gather(
                *(
                    provider.authenticate_session(repository, session.lookup_token)
                    for _index in range(8)
                )
            )

            assert all(authentication is not None for authentication in authentications)
            assert {
                authentication.owner_context for authentication in authentications if authentication
            } == {owner}
            refresh_calls = [
                call for call in fake.token_calls if call.get("grant_type") == "refresh_token"
            ]
            assert len(refresh_calls) == 1
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("elapsed", "authenticated"),
    [
        (timedelta(microseconds=999_999), True),
        (timedelta(seconds=1), False),
        (timedelta(seconds=2), False),
    ],
    ids=["immediately-before", "exact-boundary", "after-boundary"],
)
def test_shorter_refreshed_expiry_ends_authentication_despite_long_lived_access_token(
    elapsed: timedelta,
    authenticated: bool,
) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(
            refresh_token_overrides={"expires_in": 3600, "refresh_expires_in": 1},
        )
        provider, client = _provider(fake)
        repository = AuthSessionRepository(clock=lambda: fake.now)
        try:
            await provider.start()
            session = await _establish_provider_session(provider, repository)
            first = await provider.authenticate_session(repository, session.lookup_token)
            assert first is not None
            assert first.owner_context == session.owner_context

            fake.now += elapsed
            authentications = await asyncio.gather(
                *(provider.authenticate_session(repository, session.lookup_token) for _ in range(8))
            )

            assert all((result is not None) is authenticated for result in authentications)
            if authenticated:
                assert all(
                    result.owner_context == session.owner_context for result in authentications
                )
            else:
                assert repository.get_session(session.lookup_token) is None
            assert [call["grant_type"] for call in fake.token_calls] == [
                "authorization_code",
                "refresh_token",
            ]
        finally:
            await client.aclose()

    asyncio.run(run())


def test_refresh_expiring_during_provider_io_rejects_all_waiting_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        fake = FakeOidcProvider(
            refresh_token_overrides={"expires_in": 3600, "refresh_expires_in": 1},
        )
        provider, client = _provider(fake)
        repository = AuthSessionRepository(clock=lambda: fake.now)
        response_ready = asyncio.Event()
        release_response = asyncio.Event()
        try:
            await provider.start()
            session = await _establish_provider_session(provider, repository)
            token_request = provider._token_request

            async def delayed_token_request(data: Mapping[str, str]) -> dict[str, object]:
                result = await token_request(data)
                response_ready.set()
                await release_response.wait()
                return result

            monkeypatch.setattr(provider, "_token_request", delayed_token_request)
            async with asyncio.timeout(5):
                first = asyncio.create_task(
                    provider.authenticate_session(repository, session.lookup_token),
                )
                await response_ready.wait()
                waiting = [
                    asyncio.create_task(
                        provider.authenticate_session(repository, session.lookup_token),
                    )
                    for _ in range(7)
                ]
                await asyncio.sleep(0)
                fake.now += timedelta(seconds=2)
                release_response.set()
                authentications = await asyncio.gather(first, *waiting)

            assert authentications == [None] * 8
            assert repository.session_count == 0
            assert [call["grant_type"] for call in fake.token_calls] == [
                "authorization_code",
                "refresh_token",
            ]
        finally:
            release_response.set()
            await client.aclose()

    asyncio.run(run())


def test_refresh_identity_change_invalidates_cookie_session() -> None:
    async def run() -> None:
        fake = FakeOidcProvider(refresh_subject="different-provider-user")
        provider, client = _provider(fake)
        repository = AuthSessionRepository(clock=lambda: fake.now)
        try:
            await provider.start()
            initial = await provider.exchange_code(
                code="one-time-code",
                verifier="pkce-verifier",
                nonce="login-nonce",
            )
            session = repository.establish_session(
                principal=initial.principal,
                issuer=initial.issuer,
                subject=initial.subject,
                nonce="login-nonce",
                provider_credentials=initial.credentials,
            )
            fake.now += timedelta(seconds=1)

            assert await provider.authenticate_session(repository, session.lookup_token) is None
            assert repository.get_session(session.lookup_token) is None
        finally:
            await client.aclose()

    asyncio.run(run())


def test_logout_revocation_is_refresh_first_and_best_effort() -> None:
    async def run() -> None:
        fake = FakeOidcProvider()
        provider, client = _provider(fake)
        repository = AuthSessionRepository(clock=lambda: fake.now)
        try:
            await provider.start()
            result = await provider.exchange_code(
                code="one-time-code",
                verifier="pkce-verifier",
                nonce="login-nonce",
            )
            session = repository.establish_session(
                principal=result.principal,
                issuer=result.issuer,
                subject=result.subject,
                nonce="login-nonce",
                provider_credentials=result.credentials,
            )

            await provider.revoke(session)

            assert fake.revocations == ["refresh-1", "access-1"]
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "value",
    ["//attacker.test", "https://attacker.test", "/api/access", "/path#fragment", "/bad\\path"],
)
def test_return_to_rejects_external_api_or_ambiguous_targets(value: str) -> None:
    with pytest.raises(ValueError):
        validate_return_to(value)


def test_oidc_principal_identity_is_length_delimited_and_stable() -> None:
    assert oidc_principal_id("ab", "c") != oidc_principal_id("a", "bc")
    assert oidc_principal_id(ISSUER, "subject") == oidc_principal_id(ISSUER, "subject")

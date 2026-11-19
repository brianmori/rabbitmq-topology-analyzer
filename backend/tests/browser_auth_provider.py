"""Loopback-only OIDC provider for real-network browser acceptance tests.

This is test infrastructure, not an identity provider for deployment. It
performs actual signed code/PKCE exchanges and refresh over HTTP; browser tests
never intercept or fabricate the application's authentication responses.
"""

import base64
import hashlib
import html
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

CLIENT_ID = "browser-test-client"
CLIENT_SECRET = "local-test-client-secret"
PROVIDER_CLAIM_MARKER = "browser-provider-claim-must-stay-server-side"
PROFILES = {"viewer": ["browser-viewers"], "analyst": ["browser-analysts"], "no-role": []}
FAULTS = {"none", "state", "nonce", "pkce", "issuer", "audience", "algorithm", "time"}


@dataclass(slots=True)
class TestClock:
    """Advance only on an explicit driver command, never on a wall-clock sleep."""

    __test__ = False
    seconds: int = 0

    def now(self) -> datetime:
        return datetime(2026, 8, 13, 12, tzinfo=UTC) + timedelta(seconds=self.seconds)

    def set_seconds(self, value: object) -> None:
        if type(value) is not int or not 0 <= value <= 1_000_000:
            raise HTTPException(status_code=400, detail="invalid test clock")
        self.seconds = value


@dataclass(frozen=True, slots=True)
class Grant:
    profile: str
    nonce: str
    challenge: str
    fault: str


@dataclass(slots=True)
class BrowserOidcProvider:
    issuer: str
    redirect_uri: str
    clock: TestClock = field(default_factory=TestClock)
    app: FastAPI = field(init=False, repr=False)
    fault: str = "none"
    refresh_denied: bool = False
    revocation_denied: bool = False
    refresh_profile: str | None = None
    active_kid: str = "browser-key-a"
    sequence: int = 0
    counters: dict[str, int] = field(
        default_factory=lambda: {
            "discovery": 0,
            "jwks": 0,
            "exchange": 0,
            "refresh": 0,
            "pkceVerified": 0,
            "revocations": 0,
        }
    )
    keys: dict[str, rsa.RSAPrivateKey] = field(
        default_factory=lambda: {
            kid: rsa.generate_private_key(public_exponent=65537, key_size=2048)
            for kid in ("browser-key-a", "browser-key-b")
        },
        repr=False,
    )
    codes: dict[str, Grant] = field(default_factory=dict, repr=False)
    refresh_tokens: dict[str, Grant] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.app = FastAPI()
        self.app.add_api_route("/.well-known/openid-configuration", self.discovery)
        self.app.add_api_route("/jwks", self.jwks)
        self.app.add_api_route(
            "/authorize", self.authorize, methods=["GET", "POST"], response_model=None
        )
        self.app.add_api_route("/token", self.token, methods=["POST"])
        self.app.add_api_route("/revoke", self.revoke, methods=["POST"])
        self.app.add_api_route("/__test/control", self.control, methods=["POST"])
        self.app.add_api_route("/__test/state", self.state)

    async def discovery(self) -> dict[str, object]:
        self.counters["discovery"] += 1
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "revocation_endpoint": f"{self.issuer}/revoke",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        }

    async def jwks(self) -> dict[str, object]:
        self.counters["jwks"] += 1
        key = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(self.keys[self.active_kid].public_key())
        )
        key.update({"kid": self.active_kid, "use": "sig", "alg": "RS256"})
        return {"keys": [key]}

    async def authorize(self, request: Request) -> HTMLResponse | RedirectResponse:
        values = (
            dict(request.query_params) if request.method == "GET" else dict(await request.form())
        )
        required = (
            "client_id",
            "redirect_uri",
            "response_type",
            "state",
            "nonce",
            "code_challenge",
            "code_challenge_method",
            "scope",
        )
        if (
            any(not isinstance(values.get(key), str) or not values[key] for key in required)
            or values["client_id"] != CLIENT_ID
            or values["redirect_uri"] != self.redirect_uri
            or values["response_type"] != "code"
            or values["code_challenge_method"] != "S256"
            or "openid" not in str(values["scope"]).split()
        ):
            raise HTTPException(status_code=400, detail="invalid authorization request")
        if request.method == "GET":
            hidden = "".join(
                f'<input type="hidden" name="{key}" '
                f'value="{html.escape(str(values[key]), quote=True)}">'
                for key in required
            )
            return HTMLResponse(
                '<!doctype html><html lang="en"><title>Local OIDC provider</title>'
                '<h1>Local OIDC provider</h1><form method="post" action="/authorize">'
                f'{hidden}<label>Test identity <select name="profile">'
                '<option value="analyst">Analyst</option><option value="viewer">Viewer</option>'
                '<option value="no-role">No application role</option></select></label>'
                '<button type="submit">Continue</button></form></html>',
            )
        profile = str(values.get("profile", ""))
        if profile not in PROFILES:
            raise HTTPException(status_code=400, detail="unknown test identity")
        code = self._next("code")
        challenge = str(values["code_challenge"])
        if self.fault == "pkce":
            challenge += "-tampered"
        self.codes[code] = Grant(profile, str(values["nonce"]), challenge, self.fault)
        state = "invalid-state" if self.fault == "state" else str(values["state"])
        return RedirectResponse(
            f"{self.redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=303
        )

    def _client_verified(self, request: Request) -> bool:
        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        return request.headers.get("authorization") == expected

    async def token(self, request: Request) -> JSONResponse:
        if not self._client_verified(request):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        values = dict(await request.form())
        if values.get("grant_type") == "authorization_code":
            self.counters["exchange"] += 1
            code = str(values.get("code", ""))
            grant = self.codes.pop(code, None)
            verifier = str(values.get("code_verifier", ""))
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if (
                grant is None
                or challenge != grant.challenge
                or values.get("redirect_uri") != self.redirect_uri
            ):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            self.counters["pkceVerified"] += 1
            return JSONResponse(self._tokens(grant, code=code))
        if values.get("grant_type") == "refresh_token":
            self.counters["refresh"] += 1
            grant = self.refresh_tokens.pop(str(values.get("refresh_token", "")), None)
            if grant is None or self.refresh_denied:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(self._tokens(grant, code=None))
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    def _tokens(self, grant: Grant, *, code: str | None) -> dict[str, object]:
        access = self._next("access")
        refresh = self._next("refresh")
        self.refresh_tokens[refresh] = grant
        now = int(self.clock.now().timestamp())
        claims: dict[str, object] = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": f"browser-user-{grant.profile}",
            "iat": now,
            "exp": now + 900,
            "nonce": grant.nonce,
            "groups": PROFILES[
                (self.refresh_profile or grant.profile) if code is None else grant.profile
            ],
            "provider_claim_marker": PROVIDER_CLAIM_MARKER,
            "jti": self._next("jti"),
            "name": f"Mutable display {self.sequence}",
            "at_hash": self._half_hash(access),
        }
        if code is not None:
            claims["c_hash"] = self._half_hash(code)
        if grant.fault == "nonce":
            claims["nonce"] = "wrong-nonce"
        if grant.fault == "issuer":
            claims["iss"] = "http://localhost:1/untrusted"
        if grant.fault == "audience":
            claims["aud"] = "another-client"
        if grant.fault == "time":
            claims["exp"] = now - 1
        algorithm = "RS512" if grant.fault == "algorithm" else "RS256"
        return {
            "token_type": "Bearer",
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 120,
            "refresh_expires_in": 600,
            "id_token": jwt.encode(
                claims,
                self.keys[self.active_kid],
                algorithm=algorithm,
                headers={"kid": self.active_kid},
            ),
        }

    @staticmethod
    def _half_hash(value: str) -> str:
        digest = hashlib.sha256(value.encode()).digest()
        return base64.urlsafe_b64encode(digest[: len(digest) // 2]).rstrip(b"=").decode()

    def _next(self, kind: str) -> str:
        self.sequence += 1
        return f"browser-provider-{kind}-{self.sequence}"

    async def revoke(self, request: Request) -> JSONResponse:
        if not self._client_verified(request):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        values = dict(await request.form())
        self.counters["revocations"] += 1
        self.refresh_tokens.pop(str(values.get("token", "")), None)
        return JSONResponse({}, status_code=503 if self.revocation_denied else 200)

    async def control(self, request: Request) -> dict[str, object]:
        values = await request.json()
        if not isinstance(values, dict) or set(values) - {
            "seconds",
            "fault",
            "rotate",
            "refreshDenied",
            "revocationDenied",
            "refreshProfile",
        }:
            raise HTTPException(status_code=400, detail="invalid provider test control")
        if "seconds" in values:
            self.clock.set_seconds(values["seconds"])
        if "fault" in values:
            if not isinstance(values["fault"], str) or values["fault"] not in FAULTS:
                raise HTTPException(status_code=400, detail="unknown test fault")
            self.fault = values["fault"]
        if values.get("rotate") is True:
            self.active_kid = (
                "browser-key-b" if self.active_kid == "browser-key-a" else "browser-key-a"
            )
        if "refreshDenied" in values:
            self.refresh_denied = values["refreshDenied"] is True
        if "revocationDenied" in values:
            self.revocation_denied = values["revocationDenied"] is True
        if "refreshProfile" in values:
            profile = values["refreshProfile"]
            if profile is not None and (not isinstance(profile, str) or profile not in PROFILES):
                raise HTTPException(status_code=400, detail="unknown refresh profile")
            self.refresh_profile = profile
        return await self.state()

    async def state(self) -> dict[str, object]:
        # No provider credentials, nonce, state, verifier, or subject is exposed
        # even to the Node test driver. Only protocol counters are observable.
        return {"seconds": self.clock.seconds, **self.counters}

import asyncio
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx2
import jwt
import pytest

from browser_auth_provider import CLIENT_ID, CLIENT_SECRET, BrowserOidcProvider


def _authorization(provider: BrowserOidcProvider, *, profile: str = "analyst") -> dict[str, str]:
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(b"test-verifier").digest()).rstrip(b"=").decode()
    )
    return {
        "client_id": CLIENT_ID,
        "redirect_uri": provider.redirect_uri,
        "response_type": "code",
        "state": "test-state",
        "nonce": "test-nonce",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "openid offline_access",
        "profile": profile,
    }


def _client(provider: BrowserOidcProvider) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(provider.app),
        base_url=provider.issuer,
        auth=(CLIENT_ID, CLIENT_SECRET),
    )


async def _code(client: httpx2.AsyncClient, provider: BrowserOidcProvider) -> str:
    authorization = await client.post("/authorize", data=_authorization(provider))
    assert authorization.status_code == 303
    return parse_qs(urlsplit(authorization.headers["location"]).query)["code"][0]


def test_browser_provider_requires_client_auth_pkce_and_single_use_code() -> None:
    async def run() -> None:
        provider = BrowserOidcProvider(
            "http://localhost", "https://127.0.0.1/api/auth/oidc/callback"
        )
        async with _client(provider) as client:
            code = await _code(client, provider)
            fields = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": provider.redirect_uri,
                "code_verifier": "test-verifier",
            }
            assert (
                await client.post("/token", data=fields, auth=("wrong", "wrong"))
            ).status_code == 401
            assert (
                await client.post("/token", data={**fields, "code_verifier": "wrong"})
            ).status_code == 400
            assert (await client.post("/token", data=fields)).status_code == 400
            code = await _code(client, provider)
            valid = await client.post("/token", data={**fields, "code": code})
            assert valid.status_code == 200
            assert provider.counters["pkceVerified"] == 1
            assert (await client.post("/token", data={**fields, "code": code})).status_code == 400

    asyncio.run(run())


def test_browser_provider_rotates_signed_keys_and_refresh_credentials_without_identity_change() -> (
    None
):
    async def run() -> None:
        provider = BrowserOidcProvider(
            "http://localhost", "https://127.0.0.1/api/auth/oidc/callback"
        )
        async with _client(provider) as client:
            code = await _code(client, provider)
            initial = (
                await client.post(
                    "/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": provider.redirect_uri,
                        "code_verifier": "test-verifier",
                    },
                )
            ).json()
            old_claims = jwt.decode(
                initial["id_token"],
                provider.keys[provider.active_kid].public_key(),
                algorithms=["RS256"],
                audience=CLIENT_ID,
                options={"verify_exp": False},
            )
            await client.post("/__test/control", json={"rotate": True, "seconds": 120})
            refresh_fields = {
                "grant_type": "refresh_token",
                "refresh_token": initial["refresh_token"],
            }
            refreshed = (await client.post("/token", data=refresh_fields)).json()
            claims = jwt.decode(
                refreshed["id_token"],
                provider.keys[provider.active_kid].public_key(),
                algorithms=["RS256"],
                audience=CLIENT_ID,
                options={"verify_exp": False},
            )
            assert claims["sub"] == old_claims["sub"]
            assert claims["jti"] != old_claims["jti"]
            assert claims["name"] != old_claims["name"]
            assert refreshed["access_token"] != initial["access_token"]
            assert refreshed["refresh_token"] != initial["refresh_token"]
            assert (await client.post("/token", data=refresh_fields)).status_code == 400
            assert (await client.get("/jwks")).json()["keys"][0]["kid"] == provider.active_kid
            state = (await client.get("/__test/state")).json()
            assert set(state) == {
                "seconds",
                "discovery",
                "jwks",
                "exchange",
                "refresh",
                "pkceVerified",
                "revocations",
            }
            assert initial["access_token"] not in str(state)
            assert refreshed["refresh_token"] not in str(state)

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault", ["nonce", "issuer", "audience", "algorithm", "time", "pkce", "state"]
)
def test_browser_provider_fault_controls_are_deterministic(fault: str) -> None:
    async def run() -> None:
        provider = BrowserOidcProvider(
            "http://localhost", "https://127.0.0.1/api/auth/oidc/callback"
        )
        async with _client(provider) as client:
            assert (await client.post("/__test/control", json={"fault": fault})).status_code == 200
            authorization = await client.post("/authorize", data=_authorization(provider))
            query = parse_qs(urlsplit(authorization.headers["location"]).query)
            assert query["state"][0] == ("invalid-state" if fault == "state" else "test-state")
            tokens = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": query["code"][0],
                    "redirect_uri": provider.redirect_uri,
                    "code_verifier": "test-verifier",
                },
            )
            assert tokens.status_code == (400 if fault == "pkce" else 200)

    asyncio.run(run())

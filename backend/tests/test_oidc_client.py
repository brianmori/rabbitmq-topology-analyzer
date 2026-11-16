import asyncio
import base64
from urllib.parse import parse_qs

import httpx2
import pytest

from auth.oidc_client import OidcRequestAdapter
from test_oidc import _settings


def test_authlib_adapter_authenticates_every_provider_post_without_sharing_tokens() -> None:
    async def run() -> None:
        requests: list[httpx2.Request] = []

        def handle(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            return httpx2.Response(200, json={"access_token": "opaque-provider-token"})

        settings = _settings(client_id="client+: %é中", client_secret="secret+:% spaceé")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            adapter = OidcRequestAdapter(settings, client)
            forms = [
                ("token", {"grant_type": "authorization_code", "code": "one+:% é"}),
                ("token", {"grant_type": "refresh_token", "refresh_token": "refresh+:% é"}),
                ("revoke", {"token": "refresh+:% é", "token_type_hint": "refresh_token"}),
                ("revoke", {"token": "access+:% é", "token_type_hint": "access_token"}),
            ]
            for path, form in forms:
                await adapter.post_form(f"https://provider.test/{path}?tenant=one", form)

            assert len(requests) == len(forms)
            for request, (path, form) in zip(requests, forms, strict=True):
                scheme, encoded = request.headers["Authorization"].split(" ", 1)
                assert scheme == "Basic"
                # Literal expected wire bytes are independent of Authlib and
                # the application's form encoder, including double-escape traps.
                assert base64.b64decode(encoded, validate=True) == (
                    b"client%2B%3A+%25%C3%A9%E4%B8%AD:secret%2B%3A%25+space%C3%A9"
                )
                assert str(request.url) == f"https://provider.test/{path}?tenant=one"
                assert request.method == "POST"
                assert request.headers["Content-Type"].startswith(
                    "application/x-www-form-urlencoded"
                )
                assert request.headers["Accept"] == "application/json"
                assert parse_qs(request.content.decode("ascii")) == {
                    key: [value] for key, value in form.items()
                }
                assert set(request.extensions["timeout"].values()) == {5}

    asyncio.run(run())


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_authlib_adapter_never_follows_credential_post_redirects(status: int) -> None:
    async def run() -> None:
        requests: list[httpx2.Request] = []

        def handle(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            return httpx2.Response(status, headers={"Location": "https://other.test/token"})

        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handle), follow_redirects=True
        ) as client:
            adapter = OidcRequestAdapter(_settings(), client)
            response = await adapter.post_form("https://provider.test/token", {"code": "secret"})

            assert response.status_code == status
            assert len(requests) == 1
            assert requests[0].url.host == "provider.test"

    asyncio.run(run())

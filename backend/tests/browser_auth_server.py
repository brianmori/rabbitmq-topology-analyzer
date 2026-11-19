"""Isolated single-worker servers used only by Playwright authentication tests.

The real main.app handles every production route. This executable adds an
outer observation/clock harness, injects the existing auth clock seams, and
generates disposable TLS and verifier fixtures. None of these controls is
included in the application wheel or selected through production environment.
"""

# Environment isolation must precede importing main and its frontend mount.
# ruff: noqa: PLC0415

import argparse
import ipaddress
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import parse_qs

import uvicorn
from argon2 import PasswordHasher
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException, Request

from browser_auth_provider import CLIENT_ID, CLIENT_SECRET, BrowserOidcProvider, TestClock
from public_origin_cases import PUBLIC_ORIGIN_CASES

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
PROVIDER_ORIGIN = "http://localhost:4175"
OIDC_ORIGIN = "https://127.0.0.1:4176"
BASIC_PASSWORD = "local-browser-test-password"


def _certificate(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost browser test")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "tls-certificate.pem"
    key_path = directory / "tls-private-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


def _environment(mode: str, port: int, directory: Path) -> None:
    # Developer/provider secrets must never leak into a test deployment or
    # contradict its selected mode. All fixtures below are local test values.
    for key in tuple(os.environ):
        if key.startswith("RABBITMQ_"):
            os.environ.pop(key)
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    origin = f"{'http' if mode == 'none' else 'https'}://127.0.0.1:{port}"
    os.environ.update(
        {
            "RABBITMQ_AUTH_MODE": mode,
            "RABBITMQ_DEPLOYMENT_MODE": "development",
            "RABBITMQ_FRONTEND_DIST_DIR": str(REPOSITORY_ROOT / "frontend/dist"),
            "RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR": str(
                REPOSITORY_ROOT / "frontend/e2e/fixtures/static"
            ),
            "RABBITMQ_ANALYSIS_WORKERS": "1",
            "RABBITMQ_ANALYSIS_QUEUE_CAPACITY": "1",
            "RABBITMQ_ANALYSIS_RESULTS_DIR": str(directory / "results"),
            "RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES": "10485760",
        }
    )
    if mode == "oidc":
        secret = directory / "client-secret.txt"
        mapping = directory / "roles.json"
        secret.write_text(CLIENT_SECRET, encoding="utf-8")
        mapping.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "values": {"browser-viewers": ["viewer"], "browser-analysts": ["analyst"]},
                }
            ),
            encoding="utf-8",
        )
        os.environ.update(
            {
                # Exercise browser serialization in every real unsafe OIDC
                # journey: Chromium drops the configured port's leading zero.
                "RABBITMQ_PUBLIC_ORIGIN": f"https://127.0.0.1:0{port}",
                "RABBITMQ_OIDC_ISSUER": PROVIDER_ORIGIN,
                "RABBITMQ_OIDC_CLIENT_ID": CLIENT_ID,
                "RABBITMQ_OIDC_CLIENT_SECRET_FILE": str(secret),
                "RABBITMQ_OIDC_ROLE_CLAIM": "groups",
                "RABBITMQ_OIDC_ROLE_MAPPING_FILE": str(mapping),
                "RABBITMQ_OIDC_REDIRECT_URI": f"{origin}/api/auth/oidc/callback",
                "RABBITMQ_OIDC_SESSION_LIFETIME_SECONDS": "300",
                "RABBITMQ_OIDC_CLOCK_SKEW_SECONDS": "0",
                "RABBITMQ_OIDC_REFRESH_LEEWAY_SECONDS": "1",
                "RABBITMQ_OIDC_COOKIE_NAME": "__Host-browser_session",
                "RABBITMQ_OIDC_COOKIE_SECURE": "true",
            }
        )
    elif mode == "basic":
        users = directory / "users.json"
        verifier = PasswordHasher(
            time_cost=3, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16
        )
        password_hash = verifier.hash(BASIC_PASSWORD, salt=b"browser-test-salt")
        users.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "users": [
                        {
                            "username": profile,
                            "passwordHashes": [password_hash],
                            "roles": [] if profile == "no-role" else [profile],
                        }
                        for profile in ("viewer", "analyst", "no-role")
                    ],
                }
            ),
            encoding="utf-8",
        )
        os.environ.update(
            {
                "RABBITMQ_PUBLIC_ORIGIN": origin,
                "RABBITMQ_BASIC_USERS_FILE": str(users),
                "RABBITMQ_BASIC_REALM": "Local browser tests",
            }
        )


class ObservedApplication:
    """Observe actual ASGI body reads without altering auth or multipart flow."""

    def __init__(self, downstream: ASGIApp, clock: TestClock, directory: Path) -> None:
        self.downstream = downstream
        self.clock = clock
        self.directory = directory
        self.probes: dict[str, dict[str, int]] = {}
        self.controls = FastAPI()
        self.controls.add_api_route("/__test/state", self.state)
        self.controls.add_api_route("/__test/public-origins", self.public_origins)
        self.controls.add_api_route("/__test/clock", self.set_clock, methods=["POST"])

    async def public_origins(self) -> list[dict[str, str]]:
        """Expose real config normalization for an independent browser comparison."""
        from auth.config import _load_public_origin
        from core.deployment import DeploymentMode

        origins = []
        for configured, _expected in PUBLIC_ORIGIN_CASES:
            with patch.dict(os.environ, {"RABBITMQ_PUBLIC_ORIGIN": configured}):
                canonical = _load_public_origin(DeploymentMode.DEVELOPMENT)
            origins.append({"configured": configured, "canonical": canonical})
        return origins

    async def set_clock(self, request: Request) -> dict[str, object]:
        payload = await request.json()
        if not isinstance(payload, dict) or set(payload) != {"seconds"}:
            raise HTTPException(status_code=400, detail="invalid clock command")
        self.clock.set_seconds(payload["seconds"])
        return {"seconds": self.clock.seconds}

    async def state(self) -> dict[str, object]:
        from auth.runtime import get_auth_runtime
        from core import state
        from jobs.runtime import get_job_scheduler

        scheduler = get_job_scheduler()
        with state._store_lock:
            uploads = len(state._session_store)
        return {
            "seconds": self.clock.seconds,
            "authSessions": get_auth_runtime().sessions.session_count,
            "uploads": uploads,
            "runs": scheduler.run_count(),
            "activeProcesses": scheduler.active_process_count(),
            "resultFiles": len(tuple((self.directory / "results").glob("*.json"))),
            "probes": self.probes,
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/__test/"):
            await self.controls(scope, receive, send)
            return
        probe_name = next(
            (value.decode() for key, value in scope.get("headers", []) if key == b"x-test-probe"),
            None,
        )
        if scope["type"] == "http" and scope.get("path") == "/api/definitions/upload":
            # Cross-origin simple form POSTs cannot add a probe header without
            # a CORS preflight. Observe their query label without parsing a body.
            query = parse_qs(scope.get("query_string", b"").decode())
            probe_name = probe_name or query.get("__test_probe", [None])[0]
        if scope["type"] != "http" or probe_name is None:
            await self.downstream(scope, receive, send)
            return
        probe = self.probes.setdefault(
            probe_name, {"bodyReads": 0, "status": 0, "unauthorizedResponses": 0}
        )

        async def observed_receive() -> Message:
            probe["bodyReads"] += 1
            return await receive()

        async def observed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                probe["status"] = message["status"]
                if message["status"] == 401:
                    probe["unauthorizedResponses"] += 1
            await send(message)

        await self.downstream(scope, observed_receive, observed_send)


def _application(clock: TestClock, directory: Path) -> ObservedApplication:
    # Import after isolated environment setup: main mounts the production
    # frontend at import time. Spawned SDK children retain the normal SDK seam.
    sys.path.insert(0, str(BACKEND_ROOT / "src"))
    import main
    from auth.config import load_auth_settings
    from auth.oidc import OidcProvider
    from auth.runtime import configure_auth_runtime
    from auth.sessions import AuthSessionRepository

    async def configure_test_authentication() -> object:
        settings = load_auth_settings()
        oidc = settings.oidc
        if oidc is None:
            return await configure_auth_runtime(settings)
        sessions = AuthSessionRepository(
            session_lifetime=timedelta(seconds=oidc.session_lifetime_seconds), clock=clock.now
        )
        return await configure_auth_runtime(
            settings, sessions=sessions, oidc_provider=OidcProvider(oidc, clock=clock.now)
        )

    main.configure_auth_runtime = configure_test_authentication  # type: ignore[attr-defined, assignment]
    return ObservedApplication(main.app, clock, directory)


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("none", "oidc", "basic", "provider"), required=True)
    parser.add_argument("--port", type=int, required=True)
    arguments = parser.parse_args()
    if arguments.mode == "provider":
        provider = BrowserOidcProvider(PROVIDER_ORIGIN, f"{OIDC_ORIGIN}/api/auth/oidc/callback")
        uvicorn.run(
            provider.app, host="127.0.0.1", port=arguments.port, workers=1, access_log=False
        )
        return
    with TemporaryDirectory(prefix=f"rtv-browser-{arguments.mode}-") as temporary:
        directory = Path(temporary)
        _environment(arguments.mode, arguments.port, directory)
        app = _application(TestClock(), directory)
        certificate = key = None
        if arguments.mode != "none":
            certificate, key = _certificate(directory)
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=arguments.port,
            workers=1,
            access_log=False,
            ssl_certfile=str(certificate) if certificate is not None else None,
            ssl_keyfile=str(key) if key is not None else None,
        )


if __name__ == "__main__":
    run()

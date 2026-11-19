import asyncio
import base64
import json
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING, cast

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from auth.basic import BasicAuthenticator
from auth.config import load_auth_settings
from core.deployment import DeploymentMode
from core.security import AppRole, AuthMode
from main import app
from public_origin_cases import PUBLIC_ORIGIN_CASES

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from starlette.types import Scope

_VALID_SALT = base64.b64encode(b"s" * 16).decode("ascii").rstrip("=")
_VALID_OUTPUT = base64.b64encode(b"o" * 32).decode("ascii").rstrip("=")
_VALID_HASH = f"$argon2id$v=19$m=65536,t=3,p=1${_VALID_SALT}${_VALID_OUTPUT}"


@pytest.fixture(autouse=True)
def isolated_auth_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in tuple(__import__("os").environ):
        if name.startswith(("RABBITMQ_OIDC_", "RABBITMQ_BASIC_")) or name in {
            "RABBITMQ_AUTH_MODE",
            "RABBITMQ_PUBLIC_ORIGIN",
            "RABBITMQ_DEPLOYMENT_MODE",
        }:
            monkeypatch.delenv(name, raising=False)
    yield


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _configure_oidc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    public_origin: str = "https://topology.example",
) -> tuple[Path, Path]:
    secret_file = tmp_path / "oidc-client-secret"
    secret_file.write_text("s3cr3t-value\n", encoding="utf-8")
    mapping_file = _write_json(
        tmp_path / "oidc-roles.json",
        {
            "schemaVersion": 1,
            "values": {
                "topology-viewers": ["viewer"],
                "topology-analysts": ["analyst"],
            },
        },
    )
    configured = {
        "RABBITMQ_AUTH_MODE": "oidc",
        "RABBITMQ_PUBLIC_ORIGIN": public_origin,
        "RABBITMQ_OIDC_ISSUER": "https://identity.example/tenant",
        "RABBITMQ_OIDC_CLIENT_ID": "topology-client",
        "RABBITMQ_OIDC_CLIENT_SECRET_FILE": str(secret_file),
        "RABBITMQ_OIDC_REDIRECT_URI": f"{public_origin}/api/auth/oidc/callback",
        "RABBITMQ_OIDC_ROLE_CLAIM": "groups",
        "RABBITMQ_OIDC_ROLE_MAPPING_FILE": str(mapping_file),
    }
    for name, value in configured.items():
        monkeypatch.setenv(name, value)
    return secret_file, mapping_file


def _configure_basic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    users: list[object] | None = None,
    public_origin: str = "https://topology.example",
) -> Path:
    users_file = _write_json(
        tmp_path / "basic-users.json",
        {
            "schemaVersion": 1,
            "users": (
                [
                    {
                        "username": "brian",
                        "passwordHashes": [_VALID_HASH],
                        "roles": ["analyst"],
                    }
                ]
                if users is None
                else users
            ),
        },
    )
    monkeypatch.setenv("RABBITMQ_AUTH_MODE", "basic")
    monkeypatch.setenv("RABBITMQ_PUBLIC_ORIGIN", public_origin)
    monkeypatch.setenv("RABBITMQ_BASIC_USERS_FILE", str(users_file))
    return users_file


def test_development_defaults_to_none_with_immutable_typed_settings() -> None:
    settings = load_auth_settings(deployment_mode=DeploymentMode.DEVELOPMENT)

    assert settings.mode is AuthMode.NONE
    assert settings.public_origin is None
    assert settings.oidc is None
    assert settings.basic is None
    with pytest.raises(FrozenInstanceError):
        settings.public_origin = "https://unexpected.example"  # type: ignore[misc]


def test_production_requires_explicit_closed_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="explicitly configured"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)

    monkeypatch.setenv("RABBITMQ_AUTH_MODE", "jwt")
    with pytest.raises(ValueError, match="none, oidc, basic"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)

    monkeypatch.setenv("RABBITMQ_AUTH_MODE", "none")
    assert load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION).mode is AuthMode.NONE


@pytest.mark.parametrize(
    ("mode", "contradictory_name"),
    [
        ("none", "RABBITMQ_OIDC_ISSUER"),
        ("none", "RABBITMQ_BASIC_USERS_FILE"),
        ("oidc", "RABBITMQ_BASIC_USERS_FILE"),
        ("basic", "RABBITMQ_OIDC_ISSUER"),
    ],
)
def test_mode_rejects_any_contradictory_provider_variable(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    contradictory_name: str,
) -> None:
    monkeypatch.setenv("RABBITMQ_AUTH_MODE", mode)
    monkeypatch.setenv(contradictory_name, "configured")

    with pytest.raises(ValueError, match="contradicts"):
        load_auth_settings(deployment_mode=DeploymentMode.DEVELOPMENT)


def test_selected_provider_rejects_unknown_environment_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_oidc(monkeypatch, tmp_path)
    monkeypatch.setenv("RABBITMQ_OIDC_CLIENT_SECRT_FILE", "typo")

    with pytest.raises(ValueError, match="unknown OIDC"):
        load_auth_settings(deployment_mode=DeploymentMode.DEVELOPMENT)


@pytest.mark.parametrize(
    "origin",
    [
        "http://topology.example",
        "https://user@topology.example",
        "https://topology.example/application",
        "https://topology.example?query=true",
        "https://topology.example/#fragment",
        "https://topology.example?",
        "https://topology.example#",
        "https://topology.example\\",
        "https://topology.example\n",
        "https://topology.example:",
        "https://topology.example:65536",
        "https://[::1",
        "https://[::1]suffix",
        "https://[fe80::1%25eth0]",
        "https://topology%2fexample",
    ],
)
def test_authenticated_public_origin_rejects_unsafe_production_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    origin: str,
) -> None:
    _configure_basic(monkeypatch, tmp_path, public_origin=origin)

    with pytest.raises(ValueError, match="RABBITMQ_PUBLIC_ORIGIN"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


@pytest.mark.parametrize("origin", ["http://localhost:8000", "http://127.0.0.1", "http://[::1]"])
def test_development_accepts_only_loopback_plaintext_origins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    origin: str,
) -> None:
    _configure_basic(monkeypatch, tmp_path, public_origin=origin)

    settings = load_auth_settings(deployment_mode=DeploymentMode.DEVELOPMENT)

    assert settings.public_origin == origin


@pytest.mark.parametrize(("configured", "canonical"), PUBLIC_ORIGIN_CASES)
def test_oidc_public_origin_and_callback_use_browser_serialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured: str,
    canonical: str,
) -> None:
    _configure_oidc(monkeypatch, tmp_path, public_origin=configured)
    callback = f"{canonical}/api/auth/oidc/callback"
    monkeypatch.setenv("RABBITMQ_OIDC_REDIRECT_URI", callback)

    settings = load_auth_settings(
        deployment_mode=(
            DeploymentMode.PRODUCTION
            if canonical.startswith("https:")
            else DeploymentMode.DEVELOPMENT
        )
    )

    assert settings.public_origin == canonical
    assert settings.oidc is not None
    assert settings.oidc.redirect_uri == callback


def test_oidc_noncanonical_callback_fails_with_canonical_configuration_guidance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_oidc(monkeypatch, tmp_path, public_origin="https://TOPOLOGY.EXAMPLE:443")

    with pytest.raises(ValueError) as error:
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)

    assert str(error.value) == (
        "RABBITMQ_OIDC_REDIRECT_URI must exactly match "
        "https://topology.example/api/auth/oidc/callback (using the canonical public origin)"
    )


def test_oidc_loads_defaults_secret_and_deeply_immutable_role_mapping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_file, mapping_file = _configure_oidc(monkeypatch, tmp_path)

    settings = load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)

    assert settings.mode is AuthMode.OIDC
    assert settings.basic is None
    assert settings.public_origin == "https://topology.example"
    oidc = settings.oidc
    assert oidc is not None
    assert oidc.issuer == "https://identity.example/tenant"
    assert oidc.discovery_url == (
        "https://identity.example/tenant/.well-known/openid-configuration"
    )
    assert oidc.client_secret_file == secret_file
    assert oidc.client_secret == "s3cr3t-value"
    assert oidc.role_mapping_file == mapping_file
    assert oidc.scopes == ("openid", "offline_access")
    assert oidc.accepted_algorithms == ("RS256",)
    assert oidc.clock_skew_seconds == 60
    assert oidc.http_timeout_seconds == 5
    assert oidc.refresh_leeway_seconds == 60
    assert oidc.session_lifetime_seconds == 28_800
    assert oidc.session_limit == 1_024
    assert oidc.login_ttl_seconds == 600
    assert oidc.login_limit == 256
    assert oidc.jwks_cache_seconds == 300
    assert oidc.cookie_name == "rabbitmq_topology_session"
    assert oidc.cookie_secure is True
    assert oidc.login_cookie_name == "__Host-rabbitmq_topology_session_login"
    assert oidc.role_mapping["topology-analysts"] == frozenset({AppRole.ANALYST})
    assert "s3cr3t-value" not in repr(oidc)
    with pytest.raises(TypeError):
        oidc.role_mapping["new"] = frozenset({AppRole.VIEWER})  # type: ignore[index]


def test_existing_host_prefix_is_preserved_for_the_login_cookie(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_oidc(monkeypatch, tmp_path)
    monkeypatch.setenv("RABBITMQ_OIDC_COOKIE_NAME", "__Host-topology")

    settings = load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)

    assert settings.oidc is not None
    assert settings.oidc.login_cookie_name == "__Host-topology_login"


@pytest.mark.parametrize(
    "missing_name",
    [
        "RABBITMQ_OIDC_ISSUER",
        "RABBITMQ_OIDC_CLIENT_ID",
        "RABBITMQ_OIDC_CLIENT_SECRET_FILE",
        "RABBITMQ_OIDC_REDIRECT_URI",
        "RABBITMQ_OIDC_ROLE_CLAIM",
        "RABBITMQ_OIDC_ROLE_MAPPING_FILE",
    ],
)
def test_oidc_rejects_missing_required_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing_name: str,
) -> None:
    _configure_oidc(monkeypatch, tmp_path)
    monkeypatch.delenv(missing_name)

    with pytest.raises(ValueError, match=missing_name):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://attacker.example/api/auth/oidc/callback",
        "http://topology.example/api/auth/oidc/callback",
        "https://topology.example:8443/api/auth/oidc/callback",
        "https://topology.example.attacker.test/api/auth/oidc/callback",
        "https://topology.example/api/auth/oidc/callback/",
        "https://topology.example/api/auth/oidc/callback?",
        "https://topology.example/api/auth/oidc/callback#",
    ],
)
def test_oidc_requires_exact_confined_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    redirect_uri: str,
) -> None:
    _configure_oidc(monkeypatch, tmp_path)
    monkeypatch.setenv("RABBITMQ_OIDC_REDIRECT_URI", redirect_uri)

    with pytest.raises(ValueError, match="exactly match"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RABBITMQ_OIDC_CLOCK_SKEW_SECONDS", "301"),
        ("RABBITMQ_OIDC_HTTP_TIMEOUT_SECONDS", "0"),
        ("RABBITMQ_OIDC_REFRESH_LEEWAY_SECONDS", "-1"),
        ("RABBITMQ_OIDC_SESSION_LIFETIME_SECONDS", "299"),
        ("RABBITMQ_OIDC_SESSION_LIFETIME_SECONDS", "86401"),
        ("RABBITMQ_OIDC_SESSION_LIMIT", "0"),
        ("RABBITMQ_OIDC_LOGIN_TTL_SECONDS", "59"),
        ("RABBITMQ_OIDC_LOGIN_TTL_SECONDS", "901"),
        ("RABBITMQ_OIDC_LOGIN_LIMIT", "0"),
        ("RABBITMQ_OIDC_JWKS_CACHE_SECONDS", "29"),
        ("RABBITMQ_OIDC_JWKS_CACHE_SECONDS", "3601"),
    ],
)
def test_oidc_rejects_values_outside_decided_bounds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    _configure_oidc(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("RABBITMQ_OIDC_SCOPES", "openid offline_access openid", "duplicates"),
        ("RABBITMQ_OIDC_SCOPES", "openid profile", "offline_access"),
        ("RABBITMQ_OIDC_ACCEPTED_ALGORITHMS", "HS256", "unsupported"),
        ("RABBITMQ_OIDC_ACCEPTED_ALGORITHMS", "RS256,RS256", "duplicates"),
        ("RABBITMQ_OIDC_COOKIE_NAME", "invalid;cookie", "cookie name"),
        ("RABBITMQ_OIDC_COOKIE_SECURE", "sometimes", "true or false"),
    ],
)
def test_oidc_rejects_invalid_closed_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    _configure_oidc(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


def test_oidc_plaintext_and_insecure_cookie_require_development_loopback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    origin = "http://localhost:8000"
    _configure_oidc(monkeypatch, tmp_path, public_origin=origin)
    monkeypatch.setenv("RABBITMQ_OIDC_ISSUER", "http://127.0.0.1:9000/issuer")
    monkeypatch.setenv("RABBITMQ_OIDC_COOKIE_SECURE", "false")

    settings = load_auth_settings(deployment_mode=DeploymentMode.DEVELOPMENT)

    assert settings.oidc is not None
    assert settings.oidc.cookie_secure is False

    with pytest.raises(ValueError, match="HTTPS"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


@pytest.mark.parametrize("contents", ["", "\x00secret", "first\nsecond", "secret\n\n"])
def test_oidc_rejects_invalid_client_secret_file_contents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contents: str,
) -> None:
    secret_file, _mapping_file = _configure_oidc(monkeypatch, tmp_path)
    secret_file.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match="one non-empty client secret"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


@pytest.mark.parametrize(
    "payload",
    [
        {"schemaVersion": 2, "values": {}},
        {"schemaVersion": 1, "values": {}, "unknown": True},
        {"schemaVersion": 1, "values": []},
        {"schemaVersion": 1, "values": {"": ["viewer"]}},
        {"schemaVersion": 1, "values": {"group": "viewer"}},
        {"schemaVersion": 1, "values": {"group": ["admin"]}},
    ],
)
def test_oidc_role_mapping_rejects_invalid_strict_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: object,
) -> None:
    _secret_file, mapping_file = _configure_oidc(monkeypatch, tmp_path)
    _write_json(mapping_file, payload)

    with pytest.raises(ValueError):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


def test_oidc_role_mapping_rejects_duplicate_json_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _secret_file, mapping_file = _configure_oidc(monkeypatch, tmp_path)
    mapping_file.write_text(
        '{"schemaVersion":1,"values":{"group":["viewer"],"group":["analyst"]}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="strict JSON"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


def test_production_requires_absolute_oidc_secret_and_mapping_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_oidc(monkeypatch, tmp_path)
    monkeypatch.setenv("RABBITMQ_OIDC_CLIENT_SECRET_FILE", "relative-secret")

    with pytest.raises(ValueError, match="absolute path"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


def test_basic_loads_strict_users_and_hides_hashes_from_repr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    users_file = _configure_basic(
        monkeypatch,
        tmp_path,
        users=[
            {
                "username": "viewer@example",
                "passwordHashes": [_VALID_HASH],
                "roles": ["viewer"],
            },
            {
                "username": "authenticated-no-role",
                "passwordHashes": [_VALID_HASH],
                "roles": [],
            },
        ],
    )

    settings = load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)

    assert settings.mode is AuthMode.BASIC
    assert settings.oidc is None
    basic = settings.basic
    assert basic is not None
    assert basic.users_file == users_file
    assert basic.realm == "RabbitMQ Topology Analyzer"
    assert basic.verification_limit == 2
    assert basic.users["viewer@example"].roles == frozenset({AppRole.VIEWER})
    assert basic.users["authenticated-no-role"].roles == frozenset()
    assert _VALID_HASH not in repr(basic.users["viewer@example"])
    with pytest.raises(TypeError):
        basic.users["new"] = basic.users["viewer@example"]  # type: ignore[index]


def test_basic_allows_an_empty_user_catalogue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_basic(monkeypatch, tmp_path, users=[])

    settings = load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)

    assert settings.basic is not None
    assert settings.basic.users == {}


def test_basic_verification_limit_can_be_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_basic(monkeypatch, tmp_path)
    monkeypatch.setenv("RABBITMQ_BASIC_VERIFICATION_LIMIT", "3")

    settings = load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)

    assert settings.basic is not None
    assert settings.basic.verification_limit == 3


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "invalid", ""])
def test_basic_verification_limit_rejects_nonpositive_or_invalid_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    _configure_basic(monkeypatch, tmp_path)
    monkeypatch.setenv("RABBITMQ_BASIC_VERIFICATION_LIMIT", value)

    with pytest.raises(ValueError, match="RABBITMQ_BASIC_VERIFICATION_LIMIT"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


@pytest.mark.parametrize(
    "realm",
    ["", 'quoted"realm', "backslash\\realm", "line\nbreak", "non-ascii-ä"],
)
def test_basic_rejects_unsafe_challenge_realms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    realm: str,
) -> None:
    _configure_basic(monkeypatch, tmp_path)
    monkeypatch.setenv("RABBITMQ_BASIC_REALM", realm)

    with pytest.raises(ValueError, match="RABBITMQ_BASIC_REALM"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


@pytest.mark.parametrize(
    "user",
    [
        {"username": "invalid space", "passwordHashes": [_VALID_HASH], "roles": ["viewer"]},
        {"username": "brian", "passwordHashes": [], "roles": ["viewer"]},
        {"username": "brian", "passwordHashes": [_VALID_HASH], "roles": ["admin"]},
        {
            "username": "brian",
            "passwordHashes": [_VALID_HASH],
            "roles": ["viewer", "viewer"],
        },
        {
            "username": "brian",
            "passwordHashes": [_VALID_HASH],
            "roles": ["viewer"],
            "unknown": True,
        },
    ],
)
def test_basic_rejects_invalid_user_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: object,
) -> None:
    _configure_basic(monkeypatch, tmp_path, users=[user])

    with pytest.raises(ValueError):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


def test_basic_rejects_duplicate_users_and_duplicate_json_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user = {"username": "brian", "passwordHashes": [_VALID_HASH], "roles": ["analyst"]}
    users_file = _configure_basic(monkeypatch, tmp_path, users=[user, user])

    with pytest.raises(ValueError, match="duplicate username"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)

    users_file.write_text(
        '{"schemaVersion":1,"users":[],"users":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="strict JSON"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


@pytest.mark.parametrize(
    "password_hash",
    [
        _VALID_HASH.replace("argon2id", "argon2i"),
        _VALID_HASH.replace("v=19", "v=16"),
        _VALID_HASH.replace("m=65536", "m=65535"),
        _VALID_HASH.replace("t=3", "t=2"),
        _VALID_HASH.replace("p=1", "p=0"),
        f"$argon2id$v=19$m=65536,t=3,p=1$c2hvcnQ${_VALID_OUTPUT}",
        f"$argon2id$v=19$m=65536,t=3,p=1${_VALID_SALT}$c2hvcnQ",
    ],
)
def test_basic_rejects_hashes_below_argon2id_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    password_hash: str,
) -> None:
    _configure_basic(
        monkeypatch,
        tmp_path,
        users=[
            {
                "username": "brian",
                "passwordHashes": [password_hash],
                "roles": ["analyst"],
            }
        ],
    )

    with pytest.raises(ValueError, match="Argon2id"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)


@pytest.mark.parametrize(
    "parameters",
    [
        "t=3,m=65536,p=1",
        "m=065536,t=3,p=1",
        "m=65536,t=03,p=1",
        "m=65536,t=3,p=01",
        "m=٦٥٥٣٦,t=3,p=1",
        "m=65536,t=٣,p=1",
        "m=65536,t=3,p=\u0661",
        "m=65536,t=3,p=8193",
        "m=65536,t=3,p=16777216",
        "m=4294967296,t=3,p=1",
        "m=65536,t=4294967296,p=1",
        "m=65536,t=3,p=4294967296",
        "m=+65536,t=3,p=1",
        "m=65536,t=3,p=1,m=65536",
    ],
)
def test_basic_rejects_parameters_the_native_verifier_cannot_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    parameters: str,
) -> None:
    password_hash = _VALID_HASH.replace("m=65536,t=3,p=1", parameters)
    _configure_basic(
        monkeypatch,
        tmp_path,
        users=[{"username": "brian", "passwordHashes": [password_hash], "roles": ["analyst"]}],
    )

    with pytest.raises(ValueError, match="Argon2id") as failure:
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)

    assert password_hash not in str(failure.value)


def test_unicode_hash_parameters_fail_application_startup_before_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    password_hash = _VALID_HASH.replace("m=65536", "m=٦٥٥٣٦")
    _configure_basic(
        monkeypatch,
        tmp_path,
        users=[{"username": "brian", "passwordHashes": [password_hash], "roles": ["analyst"]}],
    )

    with pytest.raises(ValueError, match="Argon2id") as failure, TestClient(app):
        pytest.fail("invalid Basic configuration reached application readiness")

    assert password_hash not in str(failure.value)


@pytest.mark.parametrize(
    ("memory", "iterations", "parallelism"),
    [
        (65536, 3, 1),
        (65537, 3, 1),
        (65537, 3, 2),
        (65544, 3, 4),
        (65536, 4, 1),
        (131072, 3, 2),
        (65536, 3, 8),
    ],
)
def test_basic_accepts_and_authenticates_real_boundary_hashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory: int,
    iterations: int,
    parallelism: int,
) -> None:
    hasher = PasswordHasher(
        memory_cost=memory,
        time_cost=iterations,
        parallelism=parallelism,
        salt_len=16,
        hash_len=32,
    )
    password = "real-boundary-password"
    password_hash = hasher.hash(password)
    _configure_basic(
        monkeypatch,
        tmp_path,
        users=[{"username": "brian", "passwordHashes": [password_hash], "roles": ["analyst"]}],
    )
    settings = load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION).basic
    assert settings is not None

    async def scenario() -> None:
        verifier = BasicAuthenticator(settings)
        try:
            correct = base64.b64encode(f"brian:{password}".encode()).decode()
            wrong = base64.b64encode(b"brian:wrong-password").decode()
            assert await verifier(f"Basic {correct}", cast("Scope", {})) is not None
            assert await verifier(f"Basic {wrong}", cast("Scope", {})) is None
        finally:
            await verifier.close()

    asyncio.run(scenario())


def test_production_requires_absolute_basic_users_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_basic(monkeypatch, tmp_path)
    monkeypatch.setenv("RABBITMQ_BASIC_USERS_FILE", "relative-users.json")

    with pytest.raises(ValueError, match="absolute path"):
        load_auth_settings(deployment_mode=DeploymentMode.PRODUCTION)

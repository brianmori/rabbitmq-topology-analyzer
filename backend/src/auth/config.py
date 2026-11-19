"""Fail-fast, immutable authentication-provider configuration."""

import base64
import binascii
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl

from core.deployment import DeploymentMode, load_deployment_mode
from core.security import AppRole, AuthMode

if TYPE_CHECKING:
    from collections.abc import Mapping

_DEFAULT_OIDC_SCOPES = "openid offline_access"
_DEFAULT_OIDC_ACCEPTED_ALGORITHMS = "RS256"
_DEFAULT_OIDC_CLOCK_SKEW_SECONDS = 60
_DEFAULT_OIDC_HTTP_TIMEOUT_SECONDS = 5
_DEFAULT_OIDC_REFRESH_LEEWAY_SECONDS = 60
_DEFAULT_OIDC_SESSION_LIFETIME_SECONDS = 28_800
_DEFAULT_OIDC_SESSION_LIMIT = 1_024
_DEFAULT_OIDC_LOGIN_TTL_SECONDS = 600
_DEFAULT_OIDC_LOGIN_LIMIT = 256
_DEFAULT_OIDC_JWKS_CACHE_SECONDS = 300
_DEFAULT_OIDC_COOKIE_NAME = "rabbitmq_topology_session"
_DEFAULT_BASIC_REALM = "RabbitMQ Topology Analyzer"
_DEFAULT_BASIC_VERIFICATION_LIMIT = 2
_ASCII_SPACE = 0x20
_ASCII_DELETE = 0x7F
_ASCII_TILDE = 0x7E
_ARGON2_PHC_PART_COUNT = 6
_ARGON2_MINIMUM_MEMORY_KIB = 65_536
_ARGON2_MINIMUM_ITERATIONS = 3
_ARGON2_MINIMUM_SALT_BYTES = 16
_ARGON2_MINIMUM_OUTPUT_BYTES = 32
# Argon2 v19 limits (RFC 9106 section 3.1), rather than operational cost caps.
_ARGON2_MAXIMUM_UINT32 = (1 << 32) - 1
_ARGON2_MAXIMUM_LANES = (1 << 24) - 1
_ARGON2_MINIMUM_MEMORY_PER_LANE_KIB = 8

_OIDC_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
    }
)
_COOKIE_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_BASIC_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
_PHC_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/]+$")
_ARGON2_PARAMETERS_PATTERN = re.compile(r"m=([1-9][0-9]*),t=([1-9][0-9]*),p=([1-9][0-9]*)")

_OIDC_ENVIRONMENT_NAMES = frozenset(
    {
        "RABBITMQ_OIDC_ISSUER",
        "RABBITMQ_OIDC_DISCOVERY_URL",
        "RABBITMQ_OIDC_CLIENT_ID",
        "RABBITMQ_OIDC_CLIENT_SECRET_FILE",
        "RABBITMQ_OIDC_REDIRECT_URI",
        "RABBITMQ_OIDC_SCOPES",
        "RABBITMQ_OIDC_ROLE_CLAIM",
        "RABBITMQ_OIDC_ROLE_MAPPING_FILE",
        "RABBITMQ_OIDC_ACCEPTED_ALGORITHMS",
        "RABBITMQ_OIDC_CLOCK_SKEW_SECONDS",
        "RABBITMQ_OIDC_HTTP_TIMEOUT_SECONDS",
        "RABBITMQ_OIDC_REFRESH_LEEWAY_SECONDS",
        "RABBITMQ_OIDC_SESSION_LIFETIME_SECONDS",
        "RABBITMQ_OIDC_SESSION_LIMIT",
        "RABBITMQ_OIDC_LOGIN_TTL_SECONDS",
        "RABBITMQ_OIDC_LOGIN_LIMIT",
        "RABBITMQ_OIDC_JWKS_CACHE_SECONDS",
        "RABBITMQ_OIDC_COOKIE_NAME",
        "RABBITMQ_OIDC_COOKIE_SECURE",
    }
)
_BASIC_ENVIRONMENT_NAMES = frozenset(
    {
        "RABBITMQ_BASIC_USERS_FILE",
        "RABBITMQ_BASIC_REALM",
        "RABBITMQ_BASIC_VERIFICATION_LIMIT",
    }
)
_AUTH_ENVIRONMENT_NAMES = frozenset({"RABBITMQ_AUTH_MODE"})


class _DuplicateJsonKeyError(ValueError):
    """Signal that a strict provider JSON file repeats an object key."""


@dataclass(frozen=True, slots=True)
class OidcSettings:
    """Effective configuration for the server-owned OIDC flow."""

    issuer: str
    discovery_url: str
    client_id: str
    client_secret_file: Path
    client_secret: str = field(repr=False)
    redirect_uri: str
    scopes: tuple[str, ...]
    role_claim: str
    role_mapping_file: Path
    role_mapping: Mapping[str, frozenset[AppRole]]
    accepted_algorithms: tuple[str, ...]
    clock_skew_seconds: int
    http_timeout_seconds: int
    refresh_leeway_seconds: int
    session_lifetime_seconds: int
    session_limit: int
    login_ttl_seconds: int
    login_limit: int
    jwks_cache_seconds: int
    cookie_name: str
    cookie_secure: bool

    @property
    def login_cookie_name(self) -> str:
        """Derive the distinct browser-binding cookie with a production host prefix."""
        prefix = (
            "__Host-" if self.cookie_secure and not self.cookie_name.startswith("__Host-") else ""
        )
        return f"{prefix}{self.cookie_name}_login"


@dataclass(frozen=True, slots=True)
class BasicUser:
    """One configured HTTP Basic identity and its application roles."""

    username: str
    password_hashes: tuple[str, ...] = field(repr=False)
    roles: frozenset[AppRole]


@dataclass(frozen=True, slots=True)
class BasicSettings:
    """Effective configuration for the mounted HTTP Basic verifier file."""

    users_file: Path
    realm: str
    users: Mapping[str, BasicUser]
    verification_limit: int = _DEFAULT_BASIC_VERIFICATION_LIMIT


@dataclass(frozen=True, slots=True)
class AuthSettings:
    """Exactly one selected authentication mode and its typed provider settings."""

    mode: AuthMode
    deployment_mode: DeploymentMode
    public_origin: str | None
    oidc: OidcSettings | None
    basic: BasicSettings | None


def _configured_names(prefix: str) -> frozenset[str]:
    """Return every configured environment name in one provider namespace."""
    return frozenset(name for name in os.environ if name.startswith(prefix))


def _required_environment(name: str) -> str:
    """Read one required non-empty environment value."""
    value = os.environ.get(name)
    if value is None or value == "":
        raise ValueError(f"{name} must be configured")
    if "\x00" in value:
        raise ValueError(f"{name} contains an invalid NUL character")
    return value


def _required_non_blank_environment(name: str) -> str:
    """Read one required environment value whose content cannot be whitespace."""
    value = _required_environment(name)
    if value.strip() == "":
        raise ValueError(f"{name} must be non-empty")
    return value


def _bounded_environment_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer constrained to one inclusive interval."""
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _positive_environment_integer(name: str, default: int) -> int:
    """Read a positive integer without imposing an undocumented upper bound."""
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _environment_boolean(name: str, default: bool) -> bool:
    """Read a strict true-or-false environment setting."""
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{name} must be either true or false")


def _is_loopback_host(hostname: str) -> bool:
    """Return whether a URL host is the explicit local-development exception."""
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_transport_url(
    name: str,
    value: str,
    deployment_mode: DeploymentMode,
    *,
    allow_query: bool,
) -> str:
    """Validate an absolute provider URL and its HTTPS/loopback transport."""
    if any(
        ord(character) <= _ASCII_SPACE or ord(character) == _ASCII_DELETE for character in value
    ):
        raise ValueError(f"{name} must be an absolute provider URL")
    parsed = urlsplit(value)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} must contain a valid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.query and not allow_query)
        or parsed.netloc.endswith(":")
    ):
        raise ValueError(f"{name} must be an absolute provider URL without credentials or fragment")
    if parsed.scheme == "https":
        return value
    if deployment_mode is DeploymentMode.DEVELOPMENT and _is_loopback_host(parsed.hostname):
        return value
    raise ValueError(f"{name} must use HTTPS except for development loopback HTTP")


def _load_public_origin(deployment_mode: DeploymentMode) -> str:
    """Load the trusted origin in browser-compatible ASCII serialization."""
    name = "RABBITMQ_PUBLIC_ORIGIN"
    value = _required_environment(name)
    invalid_origin = (
        f"{name} must be one valid HTTP(S) origin without credentials, path, query, "
        "fragment, backslashes, or whitespace"
    )
    if any(
        ord(character) <= _ASCII_SPACE or ord(character) == _ASCII_DELETE for character in value
    ) or any(character in value for character in "\\?#"):
        raise ValueError(invalid_origin)
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(invalid_origin) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
    ):
        raise ValueError(invalid_origin)
    if parsed.scheme == "http" and not (
        deployment_mode is DeploymentMode.DEVELOPMENT and _is_loopback_host(parsed.hostname)
    ):
        raise ValueError(f"{name} must use HTTPS except for development loopback HTTP")
    try:
        # Pydantic's URL serializer lowercases DNS names, removes default ports,
        # and serializes IDNA/IP hosts like browser URL.origin. Raw shape checks
        # above prevent its forgiving URL parser from repairing unsafe input.
        return str(AnyHttpUrl(value)).removesuffix("/")
    except ValueError:
        raise ValueError(invalid_origin) from None


def _provider_file_path(name: str, deployment_mode: DeploymentMode) -> Path:
    """Resolve one configured provider file and enforce production absoluteness."""
    path = Path(_required_environment(name))
    if deployment_mode is DeploymentMode.PRODUCTION and not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path in production")
    return path


def _read_utf8_file(name: str, path: Path) -> str:
    """Read one provider file without reflecting its contents into an error."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{name} must reference a readable UTF-8 file") from exc


def _read_client_secret(path: Path) -> str:
    """Read exactly one non-empty OIDC client secret from its mounted file."""
    value = _read_utf8_file("RABBITMQ_OIDC_CLIENT_SECRET_FILE", path)
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith(("\r", "\n")):
        value = value[:-1]
    if value == "" or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(
            "RABBITMQ_OIDC_CLIENT_SECRET_FILE must contain one non-empty client secret"
        )
    return value


def _strict_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build an object while rejecting duplicate JSON member names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError("provider JSON contains a duplicate object key")
        result[key] = value
    return result


def _invalid_json_constant(_value: str) -> object:
    """Reject non-standard NaN and infinity values in provider JSON."""
    raise ValueError("provider JSON contains a non-standard number")


def _load_strict_json(name: str, path: Path) -> dict[str, object]:
    """Load one UTF-8 JSON object with duplicate-key rejection."""
    raw_value = _read_utf8_file(name, path)
    try:
        value = json.loads(
            raw_value,
            object_pairs_hook=_strict_json_pairs,
            parse_constant=_invalid_json_constant,
        )
    except (json.JSONDecodeError, _DuplicateJsonKeyError, ValueError) as exc:
        raise ValueError(f"{name} must contain strict JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return cast("dict[str, object]", value)


def _require_object_fields(
    value: Mapping[str, object],
    *,
    required: frozenset[str],
    description: str,
) -> None:
    """Require one exact JSON object schema."""
    names = frozenset(value)
    unknown = sorted(names - required)
    missing = sorted(required - names)
    if unknown:
        raise ValueError(f"{description} contains unknown fields: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"{description} is missing fields: {', '.join(missing)}")


def _schema_version(value: object, description: str) -> None:
    """Require the exact provider-file schema version one."""
    if type(value) is not int or value != 1:
        raise ValueError(f"{description} schemaVersion must be 1")


def _roles(value: object, description: str, *, reject_duplicates: bool) -> frozenset[AppRole]:
    """Parse one JSON array containing only the two application roles."""
    if not isinstance(value, list):
        raise ValueError(f"{description} roles must be an array")
    parsed: list[AppRole] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{description} contains an invalid application role")
        try:
            parsed.append(AppRole(item))
        except ValueError as exc:
            raise ValueError(f"{description} contains an unknown application role") from exc
    if reject_duplicates and len(parsed) != len(set(parsed)):
        raise ValueError(f"{description} contains duplicate roles")
    return frozenset(parsed)


def _load_role_mapping(path: Path) -> Mapping[str, frozenset[AppRole]]:
    """Load the strict OIDC claim-value to application-role mapping."""
    name = "RABBITMQ_OIDC_ROLE_MAPPING_FILE"
    payload = _load_strict_json(name, path)
    required = frozenset({"schemaVersion", "values"})
    _require_object_fields(payload, required=required, description="OIDC role mapping")
    _schema_version(payload["schemaVersion"], "OIDC role mapping")
    raw_values = payload["values"]
    if not isinstance(raw_values, dict):
        raise ValueError("OIDC role mapping values must be an object")
    mapping: dict[str, frozenset[AppRole]] = {}
    for claim_value, raw_roles in raw_values.items():
        if not isinstance(claim_value, str) or claim_value == "":
            raise ValueError("OIDC role mapping keys must be non-empty strings")
        mapping[claim_value] = _roles(
            raw_roles,
            f"OIDC role mapping value {claim_value!r}",
            reject_duplicates=False,
        )
    return MappingProxyType(mapping)


def _split_scopes(raw_value: str) -> tuple[str, ...]:
    """Parse the required unique OIDC scope set while retaining configured order."""
    scopes = tuple(raw_value.split())
    if not scopes:
        raise ValueError("RABBITMQ_OIDC_SCOPES must contain scopes")
    if len(scopes) != len(set(scopes)):
        raise ValueError("RABBITMQ_OIDC_SCOPES must not contain duplicates")
    if not {"openid", "offline_access"}.issubset(scopes):
        raise ValueError("RABBITMQ_OIDC_SCOPES must contain openid and offline_access")
    return scopes


def _split_algorithms(raw_value: str) -> tuple[str, ...]:
    """Parse and validate the asymmetric ID-token algorithm allowlist."""
    algorithms = tuple(part.strip() for part in raw_value.split(","))
    if not algorithms or any(not algorithm for algorithm in algorithms):
        raise ValueError("RABBITMQ_OIDC_ACCEPTED_ALGORITHMS must contain algorithms")
    if len(algorithms) != len(set(algorithms)):
        raise ValueError("RABBITMQ_OIDC_ACCEPTED_ALGORITHMS must not contain duplicates")
    if any(algorithm not in _OIDC_ALGORITHMS for algorithm in algorithms):
        raise ValueError("RABBITMQ_OIDC_ACCEPTED_ALGORITHMS contains an unsupported algorithm")
    return algorithms


def _validate_cookie_name(name: str, *, secure: bool) -> str:
    """Validate the RFC cookie token and security-prefix invariants."""
    if _COOKIE_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("RABBITMQ_OIDC_COOKIE_NAME must be a valid cookie name")
    if not secure and (name.startswith("__Secure-") or name.startswith("__Host-")):
        raise ValueError("RABBITMQ_OIDC_COOKIE_NAME prefix requires a Secure cookie")
    return name


def _load_oidc_settings(
    deployment_mode: DeploymentMode,
    public_origin: str,
) -> OidcSettings:
    """Load the complete selected OIDC provider configuration."""
    issuer = _validate_transport_url(
        "RABBITMQ_OIDC_ISSUER",
        _required_environment("RABBITMQ_OIDC_ISSUER"),
        deployment_mode,
        allow_query=False,
    )
    discovery_url = _validate_transport_url(
        "RABBITMQ_OIDC_DISCOVERY_URL",
        os.environ.get(
            "RABBITMQ_OIDC_DISCOVERY_URL",
            f"{issuer.rstrip('/')}/.well-known/openid-configuration",
        ),
        deployment_mode,
        allow_query=True,
    )
    client_id = _required_non_blank_environment("RABBITMQ_OIDC_CLIENT_ID")
    client_secret_file = _provider_file_path(
        "RABBITMQ_OIDC_CLIENT_SECRET_FILE",
        deployment_mode,
    )
    redirect_uri = _required_environment("RABBITMQ_OIDC_REDIRECT_URI")
    expected_redirect_uri = f"{public_origin}/api/auth/oidc/callback"
    if redirect_uri != expected_redirect_uri:
        raise ValueError(
            "RABBITMQ_OIDC_REDIRECT_URI must exactly match "
            f"{expected_redirect_uri} (using the canonical public origin)"
        )
    role_claim = _required_non_blank_environment("RABBITMQ_OIDC_ROLE_CLAIM")
    role_mapping_file = _provider_file_path(
        "RABBITMQ_OIDC_ROLE_MAPPING_FILE",
        deployment_mode,
    )
    cookie_secure = _environment_boolean("RABBITMQ_OIDC_COOKIE_SECURE", True)
    if not cookie_secure:
        parsed_origin = urlsplit(public_origin)
        if not (
            deployment_mode is DeploymentMode.DEVELOPMENT
            and parsed_origin.scheme == "http"
            and parsed_origin.hostname is not None
            and _is_loopback_host(parsed_origin.hostname)
        ):
            raise ValueError(
                "RABBITMQ_OIDC_COOKIE_SECURE=false requires a development loopback HTTP origin"
            )

    return OidcSettings(
        issuer=issuer,
        discovery_url=discovery_url,
        client_id=client_id,
        client_secret_file=client_secret_file,
        client_secret=_read_client_secret(client_secret_file),
        redirect_uri=redirect_uri,
        scopes=_split_scopes(os.environ.get("RABBITMQ_OIDC_SCOPES", _DEFAULT_OIDC_SCOPES)),
        role_claim=role_claim,
        role_mapping_file=role_mapping_file,
        role_mapping=_load_role_mapping(role_mapping_file),
        accepted_algorithms=_split_algorithms(
            os.environ.get(
                "RABBITMQ_OIDC_ACCEPTED_ALGORITHMS",
                _DEFAULT_OIDC_ACCEPTED_ALGORITHMS,
            )
        ),
        clock_skew_seconds=_bounded_environment_integer(
            "RABBITMQ_OIDC_CLOCK_SKEW_SECONDS",
            _DEFAULT_OIDC_CLOCK_SKEW_SECONDS,
            0,
            300,
        ),
        http_timeout_seconds=_bounded_environment_integer(
            "RABBITMQ_OIDC_HTTP_TIMEOUT_SECONDS",
            _DEFAULT_OIDC_HTTP_TIMEOUT_SECONDS,
            1,
            30,
        ),
        refresh_leeway_seconds=_bounded_environment_integer(
            "RABBITMQ_OIDC_REFRESH_LEEWAY_SECONDS",
            _DEFAULT_OIDC_REFRESH_LEEWAY_SECONDS,
            0,
            300,
        ),
        session_lifetime_seconds=_bounded_environment_integer(
            "RABBITMQ_OIDC_SESSION_LIFETIME_SECONDS",
            _DEFAULT_OIDC_SESSION_LIFETIME_SECONDS,
            300,
            86_400,
        ),
        session_limit=_positive_environment_integer(
            "RABBITMQ_OIDC_SESSION_LIMIT",
            _DEFAULT_OIDC_SESSION_LIMIT,
        ),
        login_ttl_seconds=_bounded_environment_integer(
            "RABBITMQ_OIDC_LOGIN_TTL_SECONDS",
            _DEFAULT_OIDC_LOGIN_TTL_SECONDS,
            60,
            900,
        ),
        login_limit=_positive_environment_integer(
            "RABBITMQ_OIDC_LOGIN_LIMIT",
            _DEFAULT_OIDC_LOGIN_LIMIT,
        ),
        jwks_cache_seconds=_bounded_environment_integer(
            "RABBITMQ_OIDC_JWKS_CACHE_SECONDS",
            _DEFAULT_OIDC_JWKS_CACHE_SECONDS,
            30,
            3_600,
        ),
        cookie_name=_validate_cookie_name(
            os.environ.get("RABBITMQ_OIDC_COOKIE_NAME", _DEFAULT_OIDC_COOKIE_NAME),
            secure=cookie_secure,
        ),
        cookie_secure=cookie_secure,
    )


def _decode_phc_base64(value: str) -> bytes:
    """Decode one canonical unpadded PHC Base64 segment."""
    if _PHC_BASE64_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid PHC Base64")
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid PHC Base64") from exc
    if base64.b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise ValueError("non-canonical PHC Base64")
    return decoded


def _validate_argon2id_hash(value: str) -> str:
    """Validate the decided minimum Argon2id PHC parameters without verifying it."""
    parts = value.split("$")
    if (
        len(parts) != _ARGON2_PHC_PART_COUNT
        or parts[0] != ""
        or parts[1] != "argon2id"
        or parts[2] != "v=19"
    ):
        raise ValueError("Basic password hash must be an Argon2id v19 PHC string")
    try:
        # Native PHC decoding requires ordered ASCII decimals without leading
        # zeroes; a permissive Python parameter extractor does not enforce this.
        match = _ARGON2_PARAMETERS_PATTERN.fullmatch(parts[3])
        if match is None:
            raise ValueError
        memory, iterations, parallelism = (int(number) for number in match.groups())
        salt = _decode_phc_base64(parts[4])
        output = _decode_phc_base64(parts[5])
    except ValueError as exc:
        raise ValueError("Basic password hash has an invalid Argon2id PHC encoding") from exc
    if memory < _ARGON2_MINIMUM_MEMORY_KIB or iterations < _ARGON2_MINIMUM_ITERATIONS:
        raise ValueError("Basic Argon2id password hash is below the required work factors")
    if (
        memory > _ARGON2_MAXIMUM_UINT32
        or iterations > _ARGON2_MAXIMUM_UINT32
        or parallelism > _ARGON2_MAXIMUM_LANES
        or memory < _ARGON2_MINIMUM_MEMORY_PER_LANE_KIB * parallelism
    ):
        raise ValueError("Basic Argon2id password hash has unsupported parameters")
    if len(salt) < _ARGON2_MINIMUM_SALT_BYTES or len(output) < _ARGON2_MINIMUM_OUTPUT_BYTES:
        raise ValueError("Basic Argon2id password hash is below the required salt or output size")
    return value


def _load_basic_users(path: Path) -> Mapping[str, BasicUser]:
    """Load the strict versioned HTTP Basic user secret."""
    name = "RABBITMQ_BASIC_USERS_FILE"
    payload = _load_strict_json(name, path)
    required = frozenset({"schemaVersion", "users"})
    _require_object_fields(payload, required=required, description="Basic users file")
    _schema_version(payload["schemaVersion"], "Basic users file")
    raw_users = payload["users"]
    if not isinstance(raw_users, list):
        raise ValueError("Basic users file users must be an array")

    users: dict[str, BasicUser] = {}
    user_fields = frozenset({"username", "passwordHashes", "roles"})
    for index, raw_user in enumerate(raw_users):
        description = f"Basic user at index {index}"
        if not isinstance(raw_user, dict):
            raise ValueError(f"{description} must be an object")
        _require_object_fields(raw_user, required=user_fields, description=description)
        username = raw_user["username"]
        if not isinstance(username, str) or _BASIC_USERNAME_PATTERN.fullmatch(username) is None:
            raise ValueError(f"{description} has an invalid username")
        if username in users:
            raise ValueError("Basic users file contains a duplicate username")
        raw_hashes = raw_user["passwordHashes"]
        if not isinstance(raw_hashes, list) or not raw_hashes:
            raise ValueError(f"{description} passwordHashes must be a non-empty array")
        if any(not isinstance(password_hash, str) for password_hash in raw_hashes):
            raise ValueError(f"{description} contains a non-string password hash")
        hashes = tuple(_validate_argon2id_hash(password_hash) for password_hash in raw_hashes)
        if len(hashes) != len(set(hashes)):
            raise ValueError(f"{description} contains duplicate password hashes")
        users[username] = BasicUser(
            username=username,
            password_hashes=hashes,
            roles=_roles(raw_user["roles"], description, reject_duplicates=True),
        )
    return MappingProxyType(users)


def _validate_basic_realm(value: str) -> str:
    """Validate a challenge-safe printable ASCII Basic realm."""
    if (
        value.strip() == ""
        or any(not _ASCII_SPACE <= ord(character) <= _ASCII_TILDE for character in value)
        or '"' in value
        or "\\" in value
    ):
        raise ValueError(
            "RABBITMQ_BASIC_REALM must be non-empty printable ASCII without quotes or backslashes"
        )
    return value


def _load_basic_settings(deployment_mode: DeploymentMode) -> BasicSettings:
    """Load the complete selected HTTP Basic provider configuration."""
    users_file = _provider_file_path("RABBITMQ_BASIC_USERS_FILE", deployment_mode)
    return BasicSettings(
        users_file=users_file,
        realm=_validate_basic_realm(os.environ.get("RABBITMQ_BASIC_REALM", _DEFAULT_BASIC_REALM)),
        users=_load_basic_users(users_file),
        verification_limit=_positive_environment_integer(
            "RABBITMQ_BASIC_VERIFICATION_LIMIT", _DEFAULT_BASIC_VERIFICATION_LIMIT
        ),
    )


def _reject_provider_environment(mode: AuthMode) -> None:
    """Reject contradictory and unknown provider-specific environment names."""
    configured_auth = _configured_names("RABBITMQ_AUTH_")
    configured_oidc = _configured_names("RABBITMQ_OIDC_")
    configured_basic = _configured_names("RABBITMQ_BASIC_")
    unknown_auth = sorted(configured_auth - _AUTH_ENVIRONMENT_NAMES)
    if unknown_auth:
        raise ValueError(f"unknown authentication environment settings: {', '.join(unknown_auth)}")
    if mode is not AuthMode.OIDC and configured_oidc:
        raise ValueError("RABBITMQ_OIDC_* configuration contradicts the selected auth mode")
    if mode is not AuthMode.BASIC and configured_basic:
        raise ValueError("RABBITMQ_BASIC_* configuration contradicts the selected auth mode")
    if mode is AuthMode.OIDC:
        unknown = sorted(configured_oidc - _OIDC_ENVIRONMENT_NAMES)
        if unknown:
            raise ValueError(f"unknown OIDC environment settings: {', '.join(unknown)}")
    if mode is AuthMode.BASIC:
        unknown = sorted(configured_basic - _BASIC_ENVIRONMENT_NAMES)
        if unknown:
            raise ValueError(f"unknown Basic environment settings: {', '.join(unknown)}")


def load_auth_settings(
    *,
    deployment_mode: DeploymentMode | None = None,
) -> AuthSettings:
    """Load exactly one provider contract and fail before application readiness."""
    effective_deployment_mode = deployment_mode or load_deployment_mode()
    raw_mode = os.environ.get("RABBITMQ_AUTH_MODE")
    if raw_mode is None:
        if effective_deployment_mode is DeploymentMode.PRODUCTION:
            raise ValueError("RABBITMQ_AUTH_MODE must be explicitly configured in production")
        mode = AuthMode.NONE
    else:
        try:
            mode = AuthMode(raw_mode)
        except ValueError as exc:
            choices = ", ".join(candidate.value for candidate in AuthMode)
            raise ValueError(f"RABBITMQ_AUTH_MODE must be one of: {choices}") from exc

    _reject_provider_environment(mode)
    if mode is AuthMode.NONE:
        return AuthSettings(
            mode=mode,
            deployment_mode=effective_deployment_mode,
            public_origin=None,
            oidc=None,
            basic=None,
        )

    public_origin = _load_public_origin(effective_deployment_mode)
    if mode is AuthMode.OIDC:
        return AuthSettings(
            mode=mode,
            deployment_mode=effective_deployment_mode,
            public_origin=public_origin,
            oidc=_load_oidc_settings(effective_deployment_mode, public_origin),
            basic=None,
        )
    return AuthSettings(
        mode=mode,
        deployment_mode=effective_deployment_mode,
        public_origin=public_origin,
        oidc=None,
        basic=_load_basic_settings(effective_deployment_mode),
    )

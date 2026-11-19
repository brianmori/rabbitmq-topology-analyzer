"""HTTP Basic credential parsing and Argon2id verification."""

import asyncio
import base64
import binascii
import re
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import BoundedSemaphore
from typing import TYPE_CHECKING

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from auth.middleware import AuthenticatedRequest, AuthenticationCapacityUnavailable
from core.security import AuthMode, Principal

if TYPE_CHECKING:
    from collections.abc import Sequence
    from concurrent.futures import Future

    from starlette.types import Scope

    from auth.config import BasicSettings, BasicUser

_BASIC_SCHEME = "basic"
_BASIC_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
_PRINCIPAL_NAMESPACE = b"rabbitmq-topology-analyzer:basic:v1"
_LENGTH_BYTES = 4

# This fixed record meets the deployment minimum (Argon2id v19, 64 MiB,
# three iterations, one lane, 32-byte salt/output). Its expected password is
# irrelevant: an unknown username is rejected even if verification succeeds.
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$"
    "Zml4ZWQtZHVtbXktc2FsdC0zMi1ieXRlcy1sb25nISE$"
    "sHg8D33P0ADQoBYpxnRnmcnbfoMLt5se67lbWrGoL1Y"
)


def basic_challenge(realm: str) -> str:
    """Return the configured RFC 7617 challenge value.

    ``BasicSettings`` validates the realm before this helper receives it.
    """
    return f'Basic realm="{realm}", charset="UTF-8"'


def _length_delimited_digest(namespace: bytes, components: Sequence[bytes]) -> str:
    """Hash unambiguous identity components inside one provider namespace."""
    digest = sha256()
    for component in (namespace, *components):
        digest.update(len(component).to_bytes(_LENGTH_BYTES, byteorder="big"))
        digest.update(component)
    return digest.hexdigest()


def _principal_id(username: str) -> str:
    """Return a stable Basic-only identifier without retaining the username."""
    digest = _length_delimited_digest(_PRINCIPAL_NAMESPACE, (username.encode("ascii"),))
    return f"basic:sha256:{digest}"


def _decode_authorization(value: str) -> tuple[str, str] | None:
    """Strictly decode one complete Basic Authorization header value."""
    scheme, separator, encoded = value.partition(" ")
    if (
        separator != " "
        or not scheme.isascii()
        or scheme.casefold() != _BASIC_SCHEME
        or not encoded
    ):
        return None
    try:
        encoded_bytes = encoded.encode("ascii")
        decoded_bytes = base64.b64decode(encoded_bytes, validate=True)
    except UnicodeEncodeError, binascii.Error, ValueError:
        return None
    if base64.b64encode(decoded_bytes) != encoded_bytes:
        return None
    try:
        decoded = decoded_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    username, colon, password = decoded.partition(":")
    if colon != ":" or _BASIC_USERNAME_PATTERN.fullmatch(username) is None:
        return None
    return username, password


class BasicAuthenticator:
    """Verify mounted HTTP Basic users without retaining request passwords."""

    def __init__(
        self,
        settings: BasicSettings,
        *,
        password_hasher: PasswordHasher | None = None,
    ) -> None:
        """Bind one immutable, startup-validated Basic configuration."""
        self._settings = settings
        self._password_hasher = password_hasher or PasswordHasher()
        self._closed = False
        self._verification_slots = BoundedSemaphore(settings.verification_limit)
        self._executor = ThreadPoolExecutor(
            max_workers=settings.verification_limit,
            thread_name_prefix="basic-auth",
        )

    @property
    def challenge(self) -> str:
        """Return the safe challenge corresponding to this verifier."""
        return basic_challenge(self._settings.realm)

    async def __call__(
        self,
        credential: str,
        scope: Scope,
    ) -> AuthenticatedRequest | None:
        """Verify one header value without blocking the ASGI event loop.

        The pre-body middleware supplies exactly one Authorization header value.
        Invalid credentials return ``None`` for the safe ``401`` response.
        Saturation rejects immediately, before submitting or queueing a hash.
        """
        decoded = _decode_authorization(credential)
        if decoded is None:
            return None
        username, password = decoded
        user = self._settings.users.get(username)
        verified = await self._verify_with_capacity(
            user.password_hashes if user is not None else (_DUMMY_PASSWORD_HASH,), password
        )
        if user is None or not verified:
            return None
        return self._authentication(user)

    async def close(self) -> None:
        """Stop admission and drain the dedicated verifier threads at shutdown."""
        self._closed = True
        await asyncio.to_thread(self._executor.shutdown, wait=True)

    async def _verify_with_capacity(self, hashes: Sequence[str], password: str) -> bool:
        """Hold capacity until the actual worker finishes, even after cancellation."""
        if self._closed or not self._verification_slots.acquire(blocking=False):
            raise AuthenticationCapacityUnavailable
        try:
            work = self._executor.submit(self._verify_password, hashes, password)
        except RuntimeError as exc:
            self._verification_slots.release()
            raise AuthenticationCapacityUnavailable from exc
        # Use the concurrent future, not the HTTP task, as the capacity lifetime.
        # Cancelling an asyncio waiter cannot stop an already running Argon2 call.
        work.add_done_callback(self._release_verification_slot)
        pending = asyncio.wrap_future(work)
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            pending.add_done_callback(_observe_abandoned_verification)
            raise

    def _release_verification_slot(self, _work: Future[bool]) -> None:
        """Release one slot from the worker's completion callback."""
        self._verification_slots.release()

    def _verify_password(self, password_hashes: Sequence[str], password: str) -> bool:
        """Check configured overlap hashes in deployment order."""
        for password_hash in password_hashes:
            try:
                self._password_hasher.verify(password_hash, password)
            except VerifyMismatchError:
                continue
            except InvalidHashError, VerificationError, UnicodeEncodeError:
                return False
            return True
        return False

    @staticmethod
    def _authentication(user: BasicUser) -> AuthenticatedRequest:
        """Project a verified user into provider-neutral request state."""
        return AuthenticatedRequest(
            principal=Principal(
                principal_id=_principal_id(user.username),
                roles=user.roles,
                auth_mode=AuthMode.BASIC,
            ),
        )


def _observe_abandoned_verification(pending: asyncio.Future[bool]) -> None:
    """Retrieve a worker error when its HTTP waiter has already disappeared."""
    if not pending.cancelled():
        pending.exception()

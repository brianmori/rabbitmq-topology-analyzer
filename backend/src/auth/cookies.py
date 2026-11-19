"""Strict opaque-cookie extraction shared by OIDC middleware and callbacks."""

from http.cookies import CookieError, SimpleCookie
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import Scope


def get_cookie_value(scope: Scope, cookie_name: str) -> str | None:
    """Read one named cookie while rejecting ambiguous headers or duplicate values."""
    values = [
        value.decode("latin-1")
        for name, value in scope.get("headers", ())
        if name.lower() == b"cookie"
    ]
    if len(values) != 1:
        return None
    raw_cookie = values[0]
    named_parts = [
        part
        for part in raw_cookie.split(";")
        if part.strip().partition("=")[0].strip() == cookie_name
    ]
    if len(named_parts) != 1:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie)
    except CookieError:
        return None
    morsel = cookie.get(cookie_name)
    if morsel is None or not morsel.value:
        return None
    return str(morsel.value)

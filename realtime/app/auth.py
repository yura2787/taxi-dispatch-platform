"""Authentication of WebSocket clients: a token in, a Principal out.

The handshake that carries the token is final; only the token check changes.
Today it is DevAuthenticator (unsigned dev tokens, only with DEV_AUTH=1); stage 2
replaces it with a JWT authenticator behind the same interface.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app import config


class Role(StrEnum):
    DRIVER = "driver"
    PASSENGER = "passenger"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: int
    role: Role


class AuthError(Exception):
    """The token is invalid, or tokens of this kind are not accepted."""


class Authenticator(Protocol):
    def authenticate(self, token: str) -> Principal: ...


# dev:<role>:<user_id>, e.g. dev:driver:42. [0-9], not \d: \d also matches other
# scripts' digits ("٤٢"), which int() would happily accept. No leading zeros, so one
# user has exactly one token; at most 18 digits, so the id fits a 64-bit integer.
_DEV_TOKEN = re.compile(rf"dev:({'|'.join(Role)}):([1-9][0-9]{{0,17}})")


class DevAuthenticator:
    """Accepts unsigned dev tokens: whoever has the format can be any user.

    Works only while DEV_AUTH is on. The flag is read on every call, not once at
    import, so turning it off takes effect without rebuilding the authenticator.
    """

    def authenticate(self, token: str) -> Principal:
        if not config.DEV_AUTH:
            raise AuthError("dev authentication is disabled")
        # fullmatch, not match with "$": "$" also matches before a trailing newline.
        match = _DEV_TOKEN.fullmatch(token)
        if match is None:
            raise AuthError("malformed dev token")
        role, user_id = match.groups()
        return Principal(user_id=int(user_id), role=Role(role))


def get_authenticator() -> Authenticator:
    return DevAuthenticator()

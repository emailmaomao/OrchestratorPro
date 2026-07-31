"""Identities, roles, and what each role may do.

Three roles, ordered. More would be a permission system, and a permission
system nobody can hold in their head is one that gets a wildcard added to it
the first time somebody is locked out at an awkward moment.

``VIEWER``
    Reads. Runs, tasks, builds, events, configuration, metrics.
``OPERATOR``
    Everything a viewer can do, plus starting, cancelling, resuming, approving,
    and building. The role an ordinary user of the system has.
``ADMIN``
    Everything, plus managing users and keys and reading the audit trail.

Two rules the model exists to enforce:

* **Roles are ordered, and the check is ``>=``.** An endpoint declares the
  least role that may reach it. Enumerating permissions per endpoint drifts;
  an ordering cannot.
* **A principal is never a bare string.** Whatever authenticated — a session,
  an API key — arrives as a :class:`Principal` carrying its role and how it was
  established, so an audit entry can say *which key* rather than *some admin*.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from orchestrator.core.events import OrchestratorError

__all__ = [
    "API_KEY_PREFIX",
    "AuthError",
    "ApiKey",
    "Credentials",
    "Forbidden",
    "Principal",
    "Role",
    "Unauthorized",
    "User",
    "hash_password",
    "verify_password",
]

#: Marks an API key so a leaked one is recognizable in a log or a scanner.
API_KEY_PREFIX: Final = "opk_"

#: scrypt parameters. The defaults from RFC 7914 for interactive logins: about
#: 100ms and 16 MiB per verification on ordinary hardware, which is a real cost
#: to an attacker with a stolen database and no cost to a person logging in.
_SCRYPT_N: Final = 2**14
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_SCRYPT_LEN: Final = 32
_SALT_BYTES: Final = 16

#: A username is an identifier, not free text: it appears in audit entries, in
#: URLs, and in logs.
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$")


class AuthError(OrchestratorError):
    """Something about an identity or a permission is wrong."""

    code = "auth"
    retryable = False


class Unauthorized(AuthError):
    """The request did not establish who it is from."""

    code = "unauthorized"
    retryable = False


class Forbidden(AuthError):
    """The request established who it is from, and they may not do this."""

    code = "forbidden"
    retryable = False


class Role(StrEnum):
    """What a principal may do."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        """Where this role sits in the ordering."""
        return {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}[self]

    def can(self, required: Role) -> bool:
        """Whether this role satisfies a requirement."""
        return self.rank >= required.rank

    @classmethod
    def parse(cls, value: str) -> Role:
        """Read a role name.

        Raises:
            AuthError: If it is not a role. Defaulting to the weakest role would
                be safe; defaulting to *any* role for a typo is how an account
                ends up with permissions nobody granted.
        """
        try:
            return cls(value)
        except ValueError:
            raise AuthError(
                f"{value!r} is not a role; expected one of: "
                f"{', '.join(r.value for r in cls)}",
                detail={"value": value},
            ) from None


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash a password for storage.

    Returns a self-describing string — ``scrypt$n$r$p$salt$hash`` — so the
    parameters travel with the hash. Raising the cost later must not invalidate
    every existing password, and it will not: each hash records what it used.

    Raises:
        AuthError: If the password is too short to be worth hashing.
    """
    if len(password) < 12:
        # Long enough that a wordlist is not the cheapest attack. Shorter
        # minimums exist to make people feel consulted.
        raise AuthError(
            "a password must be at least 12 characters",
            detail={"length": len(password)},
        )

    used_salt = salt or os.urandom(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=used_salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_LEN,
    )
    return "$".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            used_salt.hex(),
            derived.hex(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash.

    Constant-time, and returns ``False`` rather than raising on a malformed
    stored value: a corrupt row should fail a login, not crash the endpoint and
    tell the caller which accounts have corrupt rows.
    """
    try:
        scheme, n, r, p, salt_hex, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(expected)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), expected)


def generate_api_key() -> tuple[str, str]:
    """Mint an API key.

    Returns:
        The secret to hand to the caller once, and the digest to store. The
        secret is never stored: a database that can present a key is a database
        whose theft hands over every integration at once.
    """
    secret = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return secret, hash_api_key(secret)


def hash_api_key(secret: str) -> str:
    """Return the stored form of an API key.

    A plain SHA-256, not scrypt. An API key is 256 bits of randomness, so there
    is no dictionary to defend against, and a key is checked on every request —
    a memory-hard hash there would be a self-inflicted rate limit.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class User:
    """An account."""

    username: str
    role: Role
    password_hash: str = ""
    display_name: str = ""
    active: bool = True
    created_at: str = ""
    last_login_at: str = ""

    def __post_init__(self) -> None:
        if not _USERNAME.match(self.username):
            raise AuthError(
                f"{self.username!r} is not a usable username; use 2-64 characters "
                "of lowercase letters, digits, dot, dash, or underscore",
                detail={"username": self.username},
            )

    @property
    def name(self) -> str:
        """What to show a person."""
        return self.display_name or self.username

    def check(self, password: str) -> bool:
        """Whether a password matches, and the account may log in."""
        if not self.active or not self.password_hash:
            # Still hash, so a disabled account and a wrong password take the
            # same time. Otherwise the endpoint answers "does this user exist".
            verify_password(password, self.password_hash or _DUMMY_HASH)
            return False
        return verify_password(password, self.password_hash)

    def to_public(self) -> dict[str, Any]:
        """Render without anything secret."""
        return {
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role.value,
            "active": self.active,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }


@dataclass(frozen=True, slots=True)
class ApiKey:
    """A long-lived credential for something that is not a person."""

    id: str
    name: str
    username: str
    role: Role
    key_hash: str = ""
    created_at: str = ""
    last_used_at: str = ""
    expires_at: str = ""
    active: bool = True

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """Whether the key has passed its expiry."""
        if not self.expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(self.expires_at)
        except ValueError:
            # An unreadable expiry is treated as expired. The alternative is a
            # key that never expires because its date was written wrong.
            return True
        return (now or datetime.now(UTC)) >= expiry

    @property
    def usable(self) -> bool:
        """Whether the key may authenticate a request."""
        return self.active and not self.is_expired()

    def to_public(self) -> dict[str, Any]:
        """Render without the digest."""
        return {
            "id": self.id,
            "name": self.name,
            "username": self.username,
            "role": self.role.value,
            "active": self.active,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "expires_at": self.expires_at,
            "expired": self.is_expired(),
        }


@dataclass(frozen=True, slots=True)
class Principal:
    """Who a request is from, and how that was established."""

    username: str
    role: Role
    method: str = "token"
    key_id: str = ""
    session_id: str = ""
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """How this principal appears in an audit entry.

        Names the key rather than the account when a key was used: "the CI key"
        is actionable in a way that "an operator" is not.
        """
        if self.method == "api_key" and self.key_id:
            return f"{self.username} (key {self.key_id})"
        return self.username

    def require(self, role: Role) -> None:
        """Check this principal satisfies a role requirement.

        Raises:
            Forbidden: If it does not.
        """
        if not self.role.can(role):
            raise Forbidden(
                f"this action requires the {role.value} role; "
                f"{self.username} is {self.role.value}",
                detail={"required": role.value, "actual": self.role.value},
            )

    def to_public(self) -> dict[str, Any]:
        """Render for a response."""
        return {
            "username": self.username,
            "role": self.role.value,
            "method": self.method,
            "key_id": self.key_id or None,
        }


@dataclass(frozen=True, slots=True)
class Credentials:
    """A username and password, as presented at a login."""

    username: str
    password: str

    def __post_init__(self) -> None:
        if not self.username or not self.password:
            raise Unauthorized("a username and a password are both required")


#: Hashed against when an account does not exist or has no password, so that a
#: login takes the same time either way and cannot be used to enumerate users.
_DUMMY_HASH: Final = (
    "scrypt$16384$8$1$"
    "00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)

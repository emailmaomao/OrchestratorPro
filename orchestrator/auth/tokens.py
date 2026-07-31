"""JSON Web Tokens, signed with HMAC-SHA256.

Implemented on the standard library rather than pulled in as a dependency, and
that decision deserves its reasoning stated plainly, because "we wrote our own
crypto" is usually the last line of an incident report.

What makes it defensible here:

* **One algorithm, no negotiation.** ``HS256`` and nothing else. The header's
  ``alg`` is *checked against* the expected value, never used to select a
  verifier. The famous JWT vulnerabilities — ``alg: none``, and RS256 verified
  as HS256 with the public key as the secret — are both algorithm confusion,
  and neither is expressible here.
* **HMAC is not hand-rolled.** :mod:`hmac` and :mod:`hashlib` do the primitive;
  this module does base64url and a claim check.
* **Comparison is constant-time.** :func:`hmac.compare_digest`, not ``==``.
* **Every claim is verified.** Expiry, not-before, issuer, audience, and type.
  A token that decodes is not a token that is valid.

Two token kinds, deliberately different:

``access``
    Short-lived, carries the role, and is checked on every request without a
    database read. Its lifetime is the window in which a revoked user is still
    served, which is why it is minutes rather than days.
``refresh``
    Long-lived, carries a session identifier, and is exchanged for an access
    token. It is checked against the store, so revoking a session works
    immediately.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Final

from orchestrator.auth.models import AuthError, Role, Unauthorized

__all__ = [
    "ACCESS_TTL_S",
    "REFRESH_TTL_S",
    "TokenError",
    "TokenPair",
    "TokenService",
    "decode_unverified",
]

#: The only algorithm this module speaks.
ALGORITHM: Final = "HS256"

#: How long an access token is good for. Short: it is not checked against the
#: store, so this is the window in which a revoked account still works.
ACCESS_TTL_S: Final = 900

#: How long a refresh token is good for. Long, because it *is* checked against
#: the store and can be revoked the moment it needs to be.
REFRESH_TTL_S: Final = 30 * 24 * 3600

#: Tolerated clock difference between whoever signed and whoever verifies.
LEEWAY_S: Final = 30

_ISSUER: Final = "orchestratorpro"


class TokenError(AuthError):
    """A token could not be issued or is not valid."""

    code = "token"
    retryable = False


def _b64encode(raw: bytes) -> str:
    """base64url without padding, as JWT requires."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    """Reverse :func:`_b64encode`, restoring the padding it stripped."""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def decode_unverified(token: str) -> dict[str, Any]:
    """Read a token's claims **without checking the signature**.

    For logging and diagnostics only. Never for a decision — the name is long
    and unpleasant on purpose.

    Raises:
        TokenError: If the token is not well-formed.
    """
    try:
        _, payload, _ = token.split(".")
        claims = json.loads(_b64decode(payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError(f"the token is malformed: {exc}") from exc
    if not isinstance(claims, dict):
        raise TokenError("the token payload is not an object")
    return claims


@dataclass(frozen=True, slots=True)
class TokenPair:
    """What a successful login returns."""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"  # noqa: S105 - the OAuth scheme name, not a secret
    expires_in: int = ACCESS_TTL_S
    session_id: str = ""

    def to_public(self) -> dict[str, Any]:
        """Render for a response."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
        }


class TokenService:
    """Issues and verifies tokens for one installation."""

    __slots__ = ("_access_ttl", "_audience", "_clock", "_refresh_ttl", "_secret")

    def __init__(
        self,
        secret: str | bytes,
        *,
        access_ttl_s: int = ACCESS_TTL_S,
        refresh_ttl_s: int = REFRESH_TTL_S,
        audience: str = "orchestratorpro",
        clock: Any = time.time,
    ) -> None:
        """Create the service.

        Args:
            secret: The signing key. Must be long enough to be worth having.
            access_ttl_s: Access-token lifetime.
            refresh_ttl_s: Refresh-token lifetime.
            audience: The ``aud`` claim, checked on verification.
            clock: Returns the current Unix time; injected for tests.

        Raises:
            TokenError: If the secret is too short. A short HMAC key is the one
                mistake that makes everything else here pointless.
        """
        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(raw) < 32:
            raise TokenError(
                f"the signing secret must be at least 32 bytes, got {len(raw)}; "
                "generate one with `secrets.token_urlsafe(48)`",
                detail={"length": len(raw)},
            )
        self._secret = raw
        self._access_ttl = access_ttl_s
        self._refresh_ttl = refresh_ttl_s
        self._audience = audience
        self._clock = clock

    @staticmethod
    def generate_secret() -> str:
        """Mint a signing secret."""
        return secrets.token_urlsafe(48)

    def _sign(self, header: dict[str, Any], claims: dict[str, Any]) -> str:
        """Encode and sign one token."""
        head = _b64encode(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
        body = _b64encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        signing_input = f"{head}.{body}".encode("ascii")
        signature = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        return f"{head}.{body}.{_b64encode(signature)}"

    def issue(
        self,
        username: str,
        role: Role,
        *,
        kind: str = "access",
        ttl_s: int | None = None,
        session_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Issue one token.

        Args:
            username: The subject.
            role: Carried in the token, so an access token needs no store read.
            kind: ``access`` or ``refresh``. Checked on verification, so a
                refresh token cannot be presented as an access token.
            ttl_s: Lifetime override.
            session_id: Ties a refresh token to a revocable session.
            extra: Additional claims. Cannot overwrite a registered one.

        Returns:
            The encoded token.
        """
        now = int(self._clock())
        lifetime = ttl_s if ttl_s is not None else (
            self._access_ttl if kind == "access" else self._refresh_ttl
        )
        claims: dict[str, Any] = {
            "iss": _ISSUER,
            "aud": self._audience,
            "sub": username,
            "role": role.value,
            "typ": kind,
            "iat": now,
            "nbf": now,
            "exp": now + lifetime,
            "jti": secrets.token_urlsafe(12),
        }
        if session_id:
            claims["sid"] = session_id
        for key, value in (extra or {}).items():
            # Registered claims are not negotiable: an `extra` that could set
            # `role` would turn a debug field into a privilege escalation.
            if key in claims:
                raise TokenError(
                    f"extra claim {key!r} would overwrite a registered claim",
                    detail={"claim": key},
                )
            claims[key] = value

        return self._sign({"alg": ALGORITHM, "typ": "JWT"}, claims)

    def issue_pair(self, username: str, role: Role, *, session_id: str) -> TokenPair:
        """Issue an access and refresh token for a new session."""
        return TokenPair(
            access_token=self.issue(username, role, kind="access", session_id=session_id),
            refresh_token=self.issue(username, role, kind="refresh", session_id=session_id),
            expires_in=self._access_ttl,
            session_id=session_id,
        )

    def verify(self, token: str, *, kind: str = "access") -> dict[str, Any]:
        """Verify a token and return its claims.

        Args:
            token: The encoded token.
            kind: The kind it must be.

        Returns:
            The verified claims.

        Raises:
            Unauthorized: If the token is malformed, unsigned, signed with the
                wrong key, expired, not yet valid, for another issuer or
                audience, or of the wrong kind.
        """
        parts = token.split(".")
        if len(parts) != 3:
            raise Unauthorized("the token is malformed")

        head, body, signature = parts
        try:
            header = json.loads(_b64decode(head))
            claims = json.loads(_b64decode(body))
            presented = _b64decode(signature)
        except (ValueError, json.JSONDecodeError):
            raise Unauthorized("the token is malformed") from None

        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise Unauthorized("the token is malformed")

        # The header's algorithm is *checked*, never used to choose a verifier.
        # That single line is what makes `alg: none` and RS256/HS256 confusion
        # inexpressible here.
        if header.get("alg") != ALGORITHM:
            raise Unauthorized(
                f"the token is signed with {header.get('alg')!r}; this server "
                f"accepts only {ALGORITHM}"
            )

        expected = hmac.new(
            self._secret, f"{head}.{body}".encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, presented):
            raise Unauthorized("the token signature is not valid")

        now = int(self._clock())
        if claims.get("iss") != _ISSUER:
            raise Unauthorized("the token was issued by something else")
        if claims.get("aud") != self._audience:
            raise Unauthorized("the token is for a different audience")
        if claims.get("typ") != kind:
            raise Unauthorized(
                f"this is a {claims.get('typ')!r} token; a {kind!r} token is required"
            )

        expiry = claims.get("exp")
        if not isinstance(expiry, int) or now > expiry + LEEWAY_S:
            raise Unauthorized("the token has expired")

        not_before = claims.get("nbf")
        if isinstance(not_before, int) and now + LEEWAY_S < not_before:
            raise Unauthorized("the token is not valid yet")

        if not claims.get("sub"):
            raise Unauthorized("the token names no subject")
        try:
            Role.parse(str(claims.get("role", "")))
        except AuthError:
            raise Unauthorized("the token carries no usable role") from None

        return claims

    def role_of(self, claims: dict[str, Any]) -> Role:
        """Read the verified role out of a token's claims."""
        return Role.parse(str(claims["role"]))

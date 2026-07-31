"""Tests for token issuing and verification.

Hand-rolled crypto earns hostile tests. Most of these describe an attack rather
than a feature: the algorithm-confusion family, a tampered payload, a replayed
refresh token presented as an access token, and an expired token with a
plausible signature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from orchestrator.auth.models import Role, Unauthorized
from orchestrator.auth.tokens import (
    ALGORITHM,
    TokenError,
    TokenService,
    decode_unverified,
)

from tests.auth.conftest import SECRET


class FakeClock:
    """A clock that only moves when told."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def unsigned(claims: dict) -> str:
    """Build a token with a valid shape and no real signature."""
    def encode(payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(claims)}."


class TestConstruction:
    """The service itself."""

    def test_a_short_secret_is_refused(self) -> None:
        """The one mistake that makes everything else here pointless."""
        with pytest.raises(TokenError, match="at least 32 bytes"):
            TokenService("too short")

    def test_a_generated_secret_is_accepted(self) -> None:
        TokenService(TokenService.generate_secret())

    def test_generated_secrets_differ(self) -> None:
        assert TokenService.generate_secret() != TokenService.generate_secret()


class TestIssuing:
    """What a token carries."""

    def test_a_token_has_three_parts(self, tokens: TokenService) -> None:
        assert tokens.issue("alice", Role.OPERATOR).count(".") == 2

    def test_the_claims_are_present(self, tokens: TokenService) -> None:
        claims = decode_unverified(tokens.issue("alice", Role.OPERATOR))

        assert claims["sub"] == "alice"
        assert claims["role"] == "operator"
        assert claims["typ"] == "access"
        assert claims["iss"] == "orchestratorpro"
        assert claims["exp"] > claims["iat"]

    def test_every_token_is_unique(self, tokens: TokenService) -> None:
        """The jti makes two tokens for the same subject distinguishable."""
        one = decode_unverified(tokens.issue("alice", Role.VIEWER))
        two = decode_unverified(tokens.issue("alice", Role.VIEWER))

        assert one["jti"] != two["jti"]

    def test_the_algorithm_is_declared(self, tokens: TokenService) -> None:
        head = tokens.issue("alice", Role.VIEWER).split(".")[0]
        padded = head + "=" * (-len(head) % 4)
        assert json.loads(base64.urlsafe_b64decode(padded))["alg"] == ALGORITHM

    def test_a_session_identifier_is_carried(self, tokens: TokenService) -> None:
        claims = decode_unverified(
            tokens.issue("alice", Role.VIEWER, kind="refresh", session_id="sess_1")
        )
        assert claims["sid"] == "sess_1"

    def test_extra_claims_are_carried(self, tokens: TokenService) -> None:
        claims = decode_unverified(tokens.issue("a", Role.VIEWER, extra={"tenant": "x"}))
        assert claims["tenant"] == "x"

    def test_an_extra_claim_cannot_overwrite_a_registered_one(
        self, tokens: TokenService
    ) -> None:
        """Otherwise a debug field becomes a privilege escalation."""
        with pytest.raises(TokenError, match="registered claim"):
            tokens.issue("a", Role.VIEWER, extra={"role": "admin"})

    def test_a_pair_carries_both_kinds(self, tokens: TokenService) -> None:
        pair = tokens.issue_pair("alice", Role.ADMIN, session_id="s1")

        assert decode_unverified(pair.access_token)["typ"] == "access"
        assert decode_unverified(pair.refresh_token)["typ"] == "refresh"
        assert pair.token_type == "Bearer"


class TestVerification:
    """The happy path."""

    def test_a_fresh_token_verifies(self, tokens: TokenService) -> None:
        claims = tokens.verify(tokens.issue("alice", Role.OPERATOR))

        assert claims["sub"] == "alice"
        assert tokens.role_of(claims) is Role.OPERATOR

    def test_a_refresh_token_verifies_as_one(self, tokens: TokenService) -> None:
        token = tokens.issue("a", Role.VIEWER, kind="refresh", session_id="s")
        assert tokens.verify(token, kind="refresh")["sid"] == "s"

    def test_a_little_clock_skew_is_tolerated(self) -> None:
        clock = FakeClock()
        service = TokenService(SECRET, clock=clock)
        token = service.issue("a", Role.VIEWER, ttl_s=10)

        clock.advance(20)
        service.verify(token)


class TestAttacks:
    """The reasons this module is not a weekend project."""

    def test_an_unsigned_token_is_refused(self, tokens: TokenService) -> None:
        """`alg: none` — the first JWT vulnerability anybody learns."""
        forged = unsigned(
            {
                "iss": "orchestratorpro",
                "aud": "orchestratorpro",
                "sub": "attacker",
                "role": "admin",
                "typ": "access",
                "exp": 9_999_999_999,
            }
        )
        with pytest.raises(Unauthorized, match="signed with"):
            tokens.verify(forged)

    def test_the_header_never_chooses_the_verifier(self, tokens: TokenService) -> None:
        """The algorithm is checked against the expected value, never used."""
        token = tokens.issue("alice", Role.VIEWER)
        head, body, signature = token.split(".")
        swapped = json.dumps({"alg": "RS256", "typ": "JWT"}, sort_keys=True, separators=(",", ":"))
        rehead = base64.urlsafe_b64encode(swapped.encode()).rstrip(b"=").decode()

        with pytest.raises(Unauthorized, match="RS256"):
            tokens.verify(f"{rehead}.{body}.{signature}")

    def test_a_tampered_payload_is_refused(self, tokens: TokenService) -> None:
        head, body, signature = tokens.issue("alice", Role.VIEWER).split(".")
        padded = body + "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        claims["role"] = "admin"
        forged = base64.urlsafe_b64encode(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()

        with pytest.raises(Unauthorized, match="signature"):
            tokens.verify(f"{head}.{forged}.{signature}")

    def test_a_token_from_another_installation_is_refused(self) -> None:
        theirs = TokenService("a-completely-different-secret-of-sufficient-length")
        ours = TokenService(SECRET)

        with pytest.raises(Unauthorized, match="signature"):
            ours.verify(theirs.issue("alice", Role.ADMIN))

    def test_a_refresh_token_cannot_be_used_as_an_access_token(
        self, tokens: TokenService
    ) -> None:
        """Long-lived credentials must not reach the short-lived path."""
        refresh = tokens.issue("a", Role.ADMIN, kind="refresh", session_id="s")

        with pytest.raises(Unauthorized, match="access"):
            tokens.verify(refresh, kind="access")

    def test_an_access_token_cannot_be_refreshed(self, tokens: TokenService) -> None:
        with pytest.raises(Unauthorized, match="refresh"):
            tokens.verify(tokens.issue("a", Role.ADMIN), kind="refresh")

    def test_an_expired_token_is_refused(self) -> None:
        clock = FakeClock()
        service = TokenService(SECRET, clock=clock)
        token = service.issue("a", Role.VIEWER, ttl_s=60)

        clock.advance(3600)
        with pytest.raises(Unauthorized, match="expired"):
            service.verify(token)

    def test_a_token_from_the_future_is_refused(self) -> None:
        clock = FakeClock()
        issuer = TokenService(SECRET, clock=clock)
        token = issuer.issue("a", Role.VIEWER)

        past = TokenService(SECRET, clock=FakeClock(clock.now - 7200))
        with pytest.raises(Unauthorized, match="not valid yet"):
            past.verify(token)

    def test_a_token_for_another_audience_is_refused(self) -> None:
        theirs = TokenService(SECRET, audience="something-else")
        ours = TokenService(SECRET)

        with pytest.raises(Unauthorized, match="audience"):
            ours.verify(theirs.issue("a", Role.ADMIN))

    @pytest.mark.parametrize(
        "malformed", ["", "not-a-token", "a.b", "a.b.c.d", "...", "a..c"]
    )
    def test_malformed_tokens_are_refused(self, tokens: TokenService, malformed: str) -> None:
        with pytest.raises(Unauthorized):
            tokens.verify(malformed)

    def test_a_token_with_no_subject_is_refused(self, tokens: TokenService) -> None:
        head, body, _ = tokens.issue("a", Role.VIEWER).split(".")
        padded = body + "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        claims.pop("sub")

        forged_body = base64.urlsafe_b64encode(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()
        signature = base64.urlsafe_b64encode(
            hmac.new(
                SECRET.encode(), f"{head}.{forged_body}".encode(), hashlib.sha256
            ).digest()
        ).rstrip(b"=").decode()

        with pytest.raises(Unauthorized, match="no subject"):
            tokens.verify(f"{head}.{forged_body}.{signature}")

    def test_a_token_with_an_unknown_role_is_refused(self, tokens: TokenService) -> None:
        head, body, _ = tokens.issue("a", Role.VIEWER).split(".")
        padded = body + "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        claims["role"] = "superuser"

        forged_body = base64.urlsafe_b64encode(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()
        signature = base64.urlsafe_b64encode(
            hmac.new(
                SECRET.encode(), f"{head}.{forged_body}".encode(), hashlib.sha256
            ).digest()
        ).rstrip(b"=").decode()

        with pytest.raises(Unauthorized, match="usable role"):
            tokens.verify(f"{head}.{forged_body}.{signature}")


class TestDecodeUnverified:
    """The diagnostic reader."""

    def test_it_reads_claims(self, tokens: TokenService) -> None:
        assert decode_unverified(tokens.issue("a", Role.VIEWER))["sub"] == "a"

    def test_it_reads_a_forged_token_too(self, tokens: TokenService) -> None:
        """Which is exactly why it is never used for a decision."""
        assert decode_unverified(unsigned({"sub": "attacker"}))["sub"] == "attacker"

    def test_it_refuses_a_malformed_token(self) -> None:
        with pytest.raises(TokenError, match="malformed"):
            decode_unverified("nonsense")

    def test_it_is_never_called_by_verify(self) -> None:
        """A guard against a refactor that quietly makes it load-bearing."""
        import inspect

        from orchestrator.auth import tokens as module

        source = inspect.getsource(module.TokenService.verify)
        assert "decode_unverified" not in source

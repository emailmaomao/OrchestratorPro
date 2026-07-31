"""Persistence for accounts, keys, sessions, and the audit trail.

Four tables, owned here rather than added to the core schema. Accounts are not
part of the run record: an operator must be able to back up, restore, or clear
one without touching the other, and a `DELETE FROM users` should not be able to
reach the event log.

The audit trail is append-only and separate from the run log for the same
reason it exists at all — it records who did something, which is a different
question from what happened to a run, and the two have different retention
needs.

**A fresh installation has no accounts and no way in.** That is deliberate:
a default administrator with a known password is the single most reliably
exploited thing a self-hosted system can ship. :meth:`AuthStore.bootstrap`
creates the first administrator, refuses to run twice, and returns the password
it generated exactly once.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from orchestrator.auth.models import (
    ApiKey,
    AuthError,
    Role,
    User,
    generate_api_key,
    hash_api_key,
    hash_password,
)
from orchestrator.core.logging import get_logger
from orchestrator.core.storage import Database

__all__ = ["AuditEntry", "AuthStore", "Session"]

_log = get_logger(__name__)

#: One statement per entry. A single blob would need a parser to split, because
#: a trigger body contains semicolons — and a splitter that gets it subtly wrong
#: drops a trigger and leaves the table it was protecting editable. That is not
#: a hypothetical: it happened here, and the test that caught it is
#: ``test_it_cannot_be_edited``.
_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        username      TEXT PRIMARY KEY,
        display_name  TEXT NOT NULL DEFAULT '',
        role          TEXT NOT NULL,
        password_hash TEXT NOT NULL DEFAULT '',
        active        INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT NOT NULL,
        last_login_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_keys (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        username     TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
        role         TEXT NOT NULL,
        key_hash     TEXT NOT NULL UNIQUE,
        active       INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT NOT NULL,
        last_used_at TEXT NOT NULL DEFAULT '',
        expires_at   TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(username)",
    "CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)",
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id           TEXT PRIMARY KEY,
        username     TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
        created_at   TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        revoked_at   TEXT NOT NULL DEFAULT '',
        user_agent   TEXT NOT NULL DEFAULT '',
        address      TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username)",
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        at      TEXT NOT NULL,
        actor   TEXT NOT NULL,
        role    TEXT NOT NULL DEFAULT '',
        action  TEXT NOT NULL,
        target  TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT 'ok',
        address TEXT NOT NULL DEFAULT '',
        detail  TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor)",
    # The audit trail answers "who did this". A trail that can be edited answers
    # "who did this, unless they would rather it said otherwise".
    """
    CREATE TRIGGER IF NOT EXISTS audit_log_no_update
    BEFORE UPDATE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'audit_log is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
    BEFORE DELETE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'audit_log is append-only');
    END
    """,
)


def _now() -> str:
    """The current instant, as stored."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Session:
    """A login that can be revoked."""

    id: str
    username: str
    created_at: str
    expires_at: str
    revoked_at: str = ""
    user_agent: str = ""
    address: str = ""

    @property
    def active(self) -> bool:
        """Whether this session may still be refreshed."""
        if self.revoked_at:
            return False
        try:
            return datetime.now(UTC) < datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False

    def to_public(self) -> dict[str, Any]:
        """Render for a response."""
        return {
            "id": self.id,
            "username": self.username,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "active": self.active,
            "user_agent": self.user_agent[:200],
            "address": self.address,
        }


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One thing somebody did."""

    at: str
    actor: str
    action: str
    role: str = ""
    target: str = ""
    outcome: str = "ok"
    address: str = ""
    detail: str = ""
    id: int = 0

    def to_public(self) -> dict[str, Any]:
        """Render for a response."""
        return {
            "id": self.id,
            "at": self.at,
            "actor": self.actor,
            "role": self.role,
            "action": self.action,
            "target": self.target,
            "outcome": self.outcome,
            "address": self.address,
            "detail": self.detail,
        }


class AuthStore:
    """Accounts, keys, sessions, and the audit trail."""

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        """Bind the store to a database, creating its tables if needed."""
        self._db = db
        with db.transaction() as conn:
            for statement in _DDL:
                conn.execute(statement)

    @property
    def db(self) -> Database:
        """The underlying database."""
        return self._db

    # ----------------------------------------------------------------- users

    def create_user(
        self,
        username: str,
        password: str,
        role: Role,
        *,
        display_name: str = "",
    ) -> User:
        """Create an account.

        Raises:
            AuthError: If the username is taken or the password is too short.
        """
        user = User(
            username=username,
            role=role,
            password_hash=hash_password(password),
            display_name=display_name,
            created_at=_now(),
        )
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT INTO users (username, display_name, role, password_hash, "
                    "active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                    (
                        user.username,
                        user.display_name,
                        user.role.value,
                        user.password_hash,
                        user.created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthError(
                f"a user named {username!r} already exists",
                detail={"username": username},
            ) from exc
        _log.info("user created", username=username, role=role.value)
        return user

    def get_user(self, username: str) -> User | None:
        """Return an account, or ``None``."""
        row = self._db.query_one("SELECT * FROM users WHERE username = ?", (username,))
        return _row_to_user(row) if row is not None else None

    def list_users(self) -> tuple[User, ...]:
        """Every account, in name order."""
        rows = self._db.query("SELECT * FROM users ORDER BY username ASC")
        return tuple(_row_to_user(row) for row in rows)

    def count_users(self) -> int:
        """How many accounts exist."""
        row = self._db.query_one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def set_password(self, username: str, password: str) -> None:
        """Replace an account's password.

        Every session is revoked: a password change that leaves the old
        sessions alive does not do what the person changing it believes.
        """
        hashed = hash_password(password)
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hashed, username),
            )
            if not cursor.rowcount:
                raise AuthError(f"there is no user named {username!r}")
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE username = ? AND revoked_at = ''",
                (_now(), username),
            )
        _log.info("password changed", username=username)

    def set_role(self, username: str, role: Role) -> None:
        """Change an account's role.

        Sessions are revoked too: an access token carries its role, so a
        demotion that left them alive would keep working until they expired.
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE users SET role = ? WHERE username = ?", (role.value, username)
            )
            if not cursor.rowcount:
                raise AuthError(f"there is no user named {username!r}")
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE username = ? AND revoked_at = ''",
                (_now(), username),
            )
            conn.execute(
                "UPDATE api_keys SET role = ? WHERE username = ?", (role.value, username)
            )

    def set_active(self, username: str, active: bool) -> None:
        """Enable or disable an account, revoking its sessions when disabling."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE users SET active = ? WHERE username = ?",
                (1 if active else 0, username),
            )
            if not cursor.rowcount:
                raise AuthError(f"there is no user named {username!r}")
            if not active:
                conn.execute(
                    "UPDATE sessions SET revoked_at = ? WHERE username = ? "
                    "AND revoked_at = ''",
                    (_now(), username),
                )
                conn.execute("UPDATE api_keys SET active = 0 WHERE username = ?", (username,))

    def delete_user(self, username: str) -> None:
        """Remove an account and everything that belonged to it.

        Raises:
            AuthError: If it is the last administrator. A system with no
                administrator cannot be administered, and the recovery is
                editing the database by hand.
        """
        user = self.get_user(username)
        if user is None:
            raise AuthError(f"there is no user named {username!r}")
        if user.role is Role.ADMIN and self._admin_count() <= 1:
            raise AuthError(
                "this is the only administrator; promote another account first",
                detail={"username": username},
            )
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM api_keys WHERE username = ?", (username,))
            conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
            conn.execute("DELETE FROM users WHERE username = ?", (username,))

    def record_login(self, username: str) -> None:
        """Note that an account logged in."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE username = ?", (_now(), username)
            )

    def _admin_count(self) -> int:
        """How many active administrators there are."""
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM users WHERE role = ? AND active = 1",
            (Role.ADMIN.value,),
        )
        return int(row["n"]) if row else 0

    def bootstrap(self, username: str = "admin") -> tuple[User, str]:
        """Create the first administrator, with a generated password.

        Returns:
            The account and its password, shown once and never stored in
            readable form.

        Raises:
            AuthError: If any account already exists. Running this twice would
                be a way to add an administrator without being one.
        """
        if self.count_users():
            raise AuthError(
                "this installation already has accounts; bootstrap only creates "
                "the first one",
                detail={"users": self.count_users()},
            )
        password = secrets.token_urlsafe(18)
        user = self.create_user(username, password, Role.ADMIN, display_name="Administrator")
        self.audit(actor="system", action="auth.bootstrap", target=username, role="admin")
        return user, password

    # ------------------------------------------------------------- api keys

    def create_api_key(
        self,
        username: str,
        name: str,
        *,
        role: Role | None = None,
        expires_at: str = "",
    ) -> tuple[ApiKey, str]:
        """Mint an API key for an account.

        Args:
            username: The account it acts as.
            name: What it is for, shown in listings and audit entries.
            role: The key's role. Defaults to the account's, and may not exceed
                it — a key that outranks its owner is a privilege escalation
                wearing a convenience.
            expires_at: ISO timestamp, or empty for no expiry.

        Returns:
            The key record and the secret, which is returned exactly once.

        Raises:
            AuthError: If the account is unknown, or the role exceeds it.
        """
        user = self.get_user(username)
        if user is None:
            raise AuthError(f"there is no user named {username!r}")

        effective = role or user.role
        if effective.rank > user.role.rank:
            raise AuthError(
                f"a key for {username} cannot have the {effective.value} role; "
                f"the account is {user.role.value}",
                detail={"requested": effective.value, "account": user.role.value},
            )

        secret, digest = generate_api_key()
        key = ApiKey(
            id=f"key_{secrets.token_hex(8)}",
            name=name,
            username=username,
            role=effective,
            key_hash=digest,
            created_at=_now(),
            expires_at=expires_at,
        )
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO api_keys (id, name, username, role, key_hash, active, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    key.id,
                    key.name,
                    key.username,
                    key.role.value,
                    key.key_hash,
                    key.created_at,
                    key.expires_at,
                ),
            )
        _log.info("api key created", key_id=key.id, username=username)
        return key, secret

    def find_api_key(self, secret: str) -> ApiKey | None:
        """Return the key a secret names, or ``None``.

        Looked up by digest, so the plaintext is never compared against
        anything stored and a database dump does not yield working keys.
        """
        row = self._db.query_one(
            "SELECT * FROM api_keys WHERE key_hash = ?", (hash_api_key(secret),)
        )
        return _row_to_key(row) if row is not None else None

    def list_api_keys(self, username: str | None = None) -> tuple[ApiKey, ...]:
        """Every key, optionally for one account."""
        if username:
            rows = self._db.query(
                "SELECT * FROM api_keys WHERE username = ? ORDER BY created_at DESC",
                (username,),
            )
        else:
            rows = self._db.query("SELECT * FROM api_keys ORDER BY created_at DESC")
        return tuple(_row_to_key(row) for row in rows)

    def touch_api_key(self, key_id: str) -> None:
        """Record that a key was used."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?", (_now(), key_id)
            )

    def revoke_api_key(self, key_id: str) -> bool:
        """Disable a key. Returns whether one was disabled."""
        with self._db.transaction() as conn:
            cursor = conn.execute("UPDATE api_keys SET active = 0 WHERE id = ?", (key_id,))
            return bool(cursor.rowcount)

    # ------------------------------------------------------------- sessions

    def create_session(
        self, username: str, *, ttl_s: int, user_agent: str = "", address: str = ""
    ) -> Session:
        """Open a session a refresh token can be checked against."""
        session = Session(
            id=f"sess_{secrets.token_urlsafe(16)}",
            username=username,
            created_at=_now(),
            expires_at=datetime.fromtimestamp(
                datetime.now(UTC).timestamp() + ttl_s, tz=UTC
            ).isoformat(),
            user_agent=user_agent[:400],
            address=address,
        )
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions (id, username, created_at, expires_at, "
                "user_agent, address) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    session.username,
                    session.created_at,
                    session.expires_at,
                    session.user_agent,
                    session.address,
                ),
            )
        return session

    def get_session(self, session_id: str) -> Session | None:
        """Return a session, or ``None``."""
        row = self._db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return _row_to_session(row) if row is not None else None

    def list_sessions(self, username: str) -> tuple[Session, ...]:
        """Every session for an account, newest first."""
        rows = self._db.query(
            "SELECT * FROM sessions WHERE username = ? ORDER BY created_at DESC",
            (username,),
        )
        return tuple(_row_to_session(row) for row in rows)

    def revoke_session(self, session_id: str) -> bool:
        """Revoke one session. Returns whether one was revoked."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at = ''",
                (_now(), session_id),
            )
            return bool(cursor.rowcount)

    def revoke_sessions_of(self, username: str) -> int:
        """Revoke every session for an account."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE username = ? AND revoked_at = ''",
                (_now(), username),
            )
            return max(0, cursor.rowcount)

    def prune_sessions(self, *, before: str = "") -> int:
        """Delete sessions that expired or were revoked before a cutoff."""
        cutoff = before or _now()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE expires_at < ? OR "
                "(revoked_at != '' AND revoked_at < ?)",
                (cutoff, cutoff),
            )
            return max(0, cursor.rowcount)

    # ---------------------------------------------------------------- audit

    def audit(
        self,
        *,
        actor: str,
        action: str,
        target: str = "",
        role: str = "",
        outcome: str = "ok",
        address: str = "",
        detail: str = "",
    ) -> None:
        """Record one thing somebody did.

        Never raises. An audit write that could fail a request would be a way to
        perform an unlogged action by making the log fail.
        """
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT INTO audit_log (at, actor, role, action, target, outcome, "
                    "address, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (_now(), actor, role, action, target, outcome, address, detail[:2000]),
                )
        except Exception:  # noqa: BLE001 - an audit write must never fail an action
            # Deliberately broad. The storage layer wraps sqlite errors in its
            # own type, and a narrower catch here would mean a closed database
            # could fail the very action the entry was meant to record.
            _log.exception("the audit entry could not be written", action=action)

    def read_audit(
        self,
        *,
        limit: int = 200,
        actor: str = "",
        action: str = "",
        since: str = "",
    ) -> tuple[AuditEntry, ...]:
        """Read the audit trail, newest first."""
        clauses: list[str] = []
        params: list[Any] = []
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if since:
            clauses.append("at >= ?")
            params.append(since)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 5000)))
        rows = self._db.query(
            # noqa justification: `where` is assembled only from the fixed
            # literals above ("actor = ?", "action = ?", "at >= ?"). No
            # caller value is ever interpolated — every one is a bound
            # parameter, and the limit is clamped before binding.
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
            tuple(params),
        )
        return tuple(
            AuditEntry(
                id=int(row["id"]),
                at=str(row["at"]),
                actor=str(row["actor"]),
                role=str(row["role"]),
                action=str(row["action"]),
                target=str(row["target"]),
                outcome=str(row["outcome"]),
                address=str(row["address"]),
                detail=str(row["detail"]),
            )
            for row in rows
        )

    def count_audit(self) -> int:
        """How many audit entries there are."""
        row = self._db.query_one("SELECT COUNT(*) AS n FROM audit_log")
        return int(row["n"]) if row else 0


def _row_to_user(row: sqlite3.Row) -> User:
    """Rebuild a user from its row."""
    return User(
        username=str(row["username"]),
        role=Role.parse(str(row["role"])),
        password_hash=str(row["password_hash"]),
        display_name=str(row["display_name"]),
        active=bool(row["active"]),
        created_at=str(row["created_at"]),
        last_login_at=str(row["last_login_at"]),
    )


def _row_to_key(row: sqlite3.Row) -> ApiKey:
    """Rebuild an API key from its row."""
    return ApiKey(
        id=str(row["id"]),
        name=str(row["name"]),
        username=str(row["username"]),
        role=Role.parse(str(row["role"])),
        key_hash=str(row["key_hash"]),
        active=bool(row["active"]),
        created_at=str(row["created_at"]),
        last_used_at=str(row["last_used_at"]),
        expires_at=str(row["expires_at"]),
    )


def _row_to_session(row: sqlite3.Row) -> Session:
    """Rebuild a session from its row."""
    return Session(
        id=str(row["id"]),
        username=str(row["username"]),
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
        revoked_at=str(row["revoked_at"]),
        user_agent=str(row["user_agent"]),
        address=str(row["address"]),
    )

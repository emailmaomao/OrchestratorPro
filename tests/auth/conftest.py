"""Shared fixtures for authentication tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.api.state import AppState
from orchestrator.auth.models import Credentials, Principal, Role
from orchestrator.auth.service import AuthService
from orchestrator.auth.store import AuthStore
from orchestrator.auth.tokens import TokenService
from orchestrator.core.storage import Database

#: A password long enough for the policy, used everywhere a real one is needed.
PASSWORD = "correct-horse-battery-staple"

#: A fixed secret. Long enough to be accepted, and obviously not for real use.
SECRET = "test-secret-that-is-comfortably-long-enough-for-hmac-sha256"


@pytest.fixture
def store() -> AuthStore:
    """An auth store over a migrated in-memory database."""
    database = Database.in_memory()
    database.migrate()
    return AuthStore(database)


@pytest.fixture
def tokens() -> TokenService:
    """A token service with a fixed secret."""
    return TokenService(SECRET)


@pytest.fixture
def service(store: AuthStore, tokens: TokenService) -> AuthService:
    """An auth service with no accounts yet."""
    return AuthService(store, tokens)


@pytest.fixture
def admin(service: AuthService) -> Principal:
    """An administrator principal, with an account behind it."""
    service.store.create_user("admin", PASSWORD, Role.ADMIN)
    return Principal(username="admin", role=Role.ADMIN)


def principal(username: str = "someone", role: Role = Role.OPERATOR) -> Principal:
    """A principal, without creating an account."""
    return Principal(username=username, role=role)


def populated(service: AuthService) -> AuthService:
    """Give a service one account per role."""
    service.store.create_user("admin", PASSWORD, Role.ADMIN)
    service.store.create_user("operator", PASSWORD, Role.OPERATOR)
    service.store.create_user("viewer", PASSWORD, Role.VIEWER)
    return service


def make_app(*, with_accounts: bool = True) -> object:
    """An application with authentication wired in."""
    state = AppState.in_memory()
    auth = AuthService(AuthStore(state.database), TokenService(SECRET))
    if with_accounts:
        populated(auth)
    state.auth = auth
    return create_app(state=state)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client for an application that has accounts."""
    with TestClient(make_app()) as test_client:
        yield test_client


@pytest.fixture
def open_client() -> Iterator[TestClient]:
    """A client for an application with no accounts at all."""
    with TestClient(make_app(with_accounts=False)) as test_client:
        yield test_client


def login(client: TestClient, username: str, password: str = PASSWORD) -> dict[str, str]:
    """Log in and return an Authorization header."""
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"authorization": f"Bearer {response.json()['access_token']}"}


def tokens_of(client: TestClient, username: str, password: str = PASSWORD) -> dict[str, str]:
    """Log in and return the whole token pair."""
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def credentials(username: str = "admin", password: str = PASSWORD) -> Credentials:
    """A credentials value."""
    return Credentials(username=username, password=password)

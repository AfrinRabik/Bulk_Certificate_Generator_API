import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app


@pytest.fixture
def settings(tmp_path):
    """Isolated SQLite database + storage folder for every test."""
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=tmp_path / "storage",
        max_recipients=50,
    )


@pytest.fixture
def make_client(settings):
    """Factory so a test can plug in a custom (e.g. failing) generator."""
    clients = []

    def _make(generator=None):
        app = create_app(settings, generator=generator)
        client = TestClient(app)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.app.state.engine.dispose()


@pytest.fixture
def client(make_client):
    return make_client()


def job_payload(recipients=None, **overrides):
    payload = {
        "event_name": "Python Bootcamp 2026",
        "issued_by": "Acme Academy",
        "issue_date": "2026-10-08",
        "recipients": recipients
        if recipients is not None
        else [
            {"name": "Asha Raman", "email": "asha@example.com"},
            {"name": "Ben Carter", "email": "ben@example.com", "achievement": "With distinction"},
            {"name": "Chitra Devi"},
        ],
    }
    payload.update(overrides)
    return payload

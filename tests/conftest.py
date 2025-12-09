import os
import pytest
from fastapi.testclient import TestClient

# Set required env vars before importing app
os.environ.setdefault("SPOTIFY_CLIENT_ID", "test_client_id")
os.environ.setdefault("SPOTIFY_CLIENT_SECRET", "test_client_secret")
os.environ.setdefault("SECRET_KEY", "test_secret_key")
os.environ.setdefault("BASE_URL", "http://localhost:8000")

from app.main import app, sessions


@pytest.fixture
def client():
    """Create a test client."""
    with TestClient(app) as client:
        yield client


@pytest.fixture(autouse=True)
def clear_sessions():
    """Clear sessions between tests."""
    sessions.clear()
    yield
    sessions.clear()

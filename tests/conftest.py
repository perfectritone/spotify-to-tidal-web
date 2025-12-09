import os
import pytest
from unittest.mock import MagicMock
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


@pytest.fixture
def authenticated_session(client):
    """Create a session with both Spotify and Tidal authenticated."""
    session_id = "test_session_123"
    mock_tidal = MagicMock()
    mock_tidal.check_login.return_value = True

    sessions[session_id] = {
        "spotify_token": "test_spotify_token",
        "spotify_user": "test_user",
        "tidal_session": mock_tidal,
        "tidal_user": "tidal_user",
    }

    # Set the session cookie on the client
    client.cookies.set("session_id", session_id)
    return sessions[session_id]


@pytest.fixture
def authenticated_session_spotify_only(client):
    """Create a session with only Spotify authenticated."""
    session_id = "test_session_spotify"
    sessions[session_id] = {
        "spotify_token": "test_spotify_token",
        "spotify_user": "test_user",
    }
    client.cookies.set("session_id", session_id)
    return sessions[session_id]


@pytest.fixture
def authenticated_session_tidal_only(client):
    """Create a session with only Tidal authenticated."""
    session_id = "test_session_tidal"
    mock_tidal = MagicMock()
    sessions[session_id] = {
        "tidal_session": mock_tidal,
        "tidal_user": "tidal_user",
    }
    client.cookies.set("session_id", session_id)
    return sessions[session_id]

import os
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

# Set required env vars before importing app
os.environ.setdefault("SPOTIFY_CLIENT_ID", "test_client_id")
os.environ.setdefault("SPOTIFY_CLIENT_SECRET", "test_client_secret")
os.environ.setdefault("SECRET_KEY", "test_secret_key_for_testing_only")
os.environ.setdefault("BASE_URL", "http://localhost:8000")

from app.main import app, pending_tidal_auth, serializer, sessions


@pytest.fixture
def client():
    """Create a test client."""
    with TestClient(app) as client:
        yield client


@pytest.fixture(autouse=True)
def clear_sessions():
    """Clear pending auth between tests."""
    pending_tidal_auth.clear()
    sessions.clear()
    yield
    pending_tidal_auth.clear()
    sessions.clear()


@pytest.fixture
def authenticated_session(client):
    """Create a session with both Spotify and Tidal authenticated via cookies."""
    # Set Spotify session cookie
    spotify_data = {
        "token": "test_spotify_token",
        "refresh_token": "test_refresh_token",
        "user": "test_user",
    }
    spotify_cookie = serializer.dumps(spotify_data)
    client.cookies.set("spotify_session", spotify_cookie)

    # Set Tidal session cookie (mock will handle the actual session)
    tidal_data = {
        "token_type": "Bearer",
        "access_token": "test_tidal_token",
        "refresh_token": "test_tidal_refresh",
        "expiry_time": None,
        "user_id": "tidal_user",
    }
    tidal_cookie = serializer.dumps(tidal_data)
    client.cookies.set("tidal_session", tidal_cookie)

    return {"spotify": spotify_data, "tidal": tidal_data}


@pytest.fixture
def authenticated_session_spotify_only(client):
    """Create a session with only Spotify authenticated."""
    spotify_data = {
        "token": "test_spotify_token",
        "refresh_token": "test_refresh_token",
        "user": "test_user",
    }
    spotify_cookie = serializer.dumps(spotify_data)
    client.cookies.set("spotify_session", spotify_cookie)
    return spotify_data


@pytest.fixture
def authenticated_session_tidal_only(client):
    """Create a session with only Tidal authenticated."""
    tidal_data = {
        "token_type": "Bearer",
        "access_token": "test_tidal_token",
        "refresh_token": "test_tidal_refresh",
        "expiry_time": None,
        "user_id": "tidal_user",
    }
    tidal_cookie = serializer.dumps(tidal_data)
    client.cookies.set("tidal_session", tidal_cookie)
    return tidal_data

"""Tests for the web app."""

import pytest


class TestHomePage:
    """Tests for the home page."""

    def test_home_page_renders(self, client):
        """Home page should render successfully."""
        response = client.get("/")
        assert response.status_code == 200
        assert "Spotify to Tidal" in response.text

    def test_home_shows_connect_buttons_when_not_logged_in(self, client):
        """Should show connect buttons when not authenticated."""
        response = client.get("/")
        assert "Connect Spotify" in response.text
        assert "Connect Tidal" in response.text

    def test_home_shows_not_connected_status(self, client):
        """Should show disconnected status for both services."""
        response = client.get("/")
        assert "Not connected" in response.text


class TestSpotifyAuth:
    """Tests for Spotify OAuth flow."""

    def test_spotify_auth_redirects(self, client):
        """Should redirect to Spotify authorization."""
        response = client.get("/auth/spotify", follow_redirects=False)
        assert response.status_code == 307
        assert "accounts.spotify.com/authorize" in response.headers["location"]

    def test_spotify_auth_includes_required_params(self, client):
        """Redirect URL should include required OAuth params."""
        response = client.get("/auth/spotify", follow_redirects=False)
        location = response.headers["location"]
        assert "client_id=" in location
        assert "response_type=code" in location
        assert "scope=" in location

    def test_spotify_callback_without_code_redirects_with_error(self, client):
        """Callback without code should redirect with error."""
        response = client.get("/auth/spotify/callback", follow_redirects=False)
        assert response.status_code == 307
        assert "error=no_code" in response.headers["location"]

    def test_spotify_callback_with_error_redirects(self, client):
        """Callback with error should redirect with that error."""
        response = client.get("/auth/spotify/callback?error=access_denied", follow_redirects=False)
        assert response.status_code == 307
        assert "error=access_denied" in response.headers["location"]


class TestTidalAuth:
    """Tests for Tidal OAuth flow."""

    def test_tidal_auth_redirects_to_device_page(self, client):
        """Should redirect to device auth page."""
        response = client.get("/auth/tidal", follow_redirects=False)
        assert response.status_code == 307
        assert "/auth/tidal/device" in response.headers["location"]

    def test_tidal_device_page_without_pending_auth_redirects_home(self, client):
        """Device page without pending auth should redirect to home."""
        response = client.get("/auth/tidal/device", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/"

    def test_tidal_check_without_pending_returns_error(self, client):
        """Check endpoint without pending auth should return error."""
        response = client.get("/auth/tidal/check")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"


class TestLogout:
    """Tests for logout functionality."""

    def test_logout_redirects_to_home(self, client):
        """Logout should redirect to home page."""
        response = client.get("/logout", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/"

    def test_logout_clears_session_cookie(self, client):
        """Logout should clear the session cookie."""
        response = client.get("/logout", follow_redirects=False)
        # Cookie should be deleted (max-age=0 or expires in past)
        assert "session_id" in response.headers.get("set-cookie", "")


class TestSyncEndpoint:
    """Tests for the sync endpoint."""

    def test_sync_without_auth_returns_error(self, client):
        """Sync without authentication should return error."""
        response = client.post("/sync", data={"playlists": "on"})
        assert response.status_code == 400

    def test_sync_requires_both_services(self, client):
        """Sync should require both Spotify and Tidal auth."""
        response = client.post("/sync", data={"playlists": "on"})
        # Should fail because neither service is connected
        assert response.status_code == 400

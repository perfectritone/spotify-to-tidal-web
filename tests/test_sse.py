"""Tests for SSE streaming."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestSyncStreaming:
    """Tests for the sync streaming generator."""

    @pytest.mark.asyncio
    async def test_streaming_yields_start_events(self):
        """Should yield start events for each enabled sync type."""
        from app.sync import run_sync_streaming

        # Mock all the library functions
        with patch('app.sync.get_playlists_from_spotify', new_callable=AsyncMock) as mock_playlists, \
             patch('app.sync.get_tidal_playlists_wrapper') as mock_tidal_playlists, \
             patch('app.sync.sync_playlist', new_callable=AsyncMock), \
             patch('app.sync.repeat_on_request_error', new_callable=AsyncMock) as mock_repeat, \
             patch('app.sync.get_all_favorites', new_callable=AsyncMock) as mock_favorites, \
             patch('app.sync.populate_track_match_cache'), \
             patch('app.sync.search_new_tracks_on_tidal', new_callable=AsyncMock), \
             patch('app.sync.get_albums_from_spotify', new_callable=AsyncMock) as mock_albums, \
             patch('app.sync.get_artists_from_spotify', new_callable=AsyncMock) as mock_artists, \
             patch('app.sync.REQUEST_DELAY', 0):  # No delay for tests

            mock_playlists.return_value = []
            mock_tidal_playlists.return_value = {}
            mock_repeat.return_value = []
            mock_favorites.return_value = []
            mock_albums.return_value = []
            mock_artists.return_value = []

            mock_tidal = MagicMock()
            mock_tidal.user.favorites.albums.return_value = []
            mock_tidal.user.favorites.artists.return_value = []

            events = []
            async for event in run_sync_streaming(
                spotify_token="test_token",
                tidal_session=mock_tidal,
                sync_playlists=True,
                do_sync_favorites=True,
                do_sync_albums=True,
                do_sync_artists=True,
            ):
                events.append(event)

            # Should have start, done for each type + complete
            event_types = [json.loads(e["data"])["type"] for e in events]

            # Check we have start events
            assert event_types.count("start") == 4
            assert event_types.count("done") == 4
            assert event_types.count("complete") == 1

    @pytest.mark.asyncio
    async def test_streaming_yields_progress_for_playlists(self):
        """Should yield progress events for playlists."""
        from app.sync import run_sync_streaming

        with patch('app.sync.get_playlists_from_spotify', new_callable=AsyncMock) as mock_playlists, \
             patch('app.sync.get_tidal_playlists_wrapper') as mock_tidal_playlists, \
             patch('app.sync.sync_playlist', new_callable=AsyncMock), \
             patch('app.sync.REQUEST_DELAY', 0):

            # Return 3 playlists
            mock_playlists.return_value = [
                {"name": "Playlist 1"},
                {"name": "Playlist 2"},
                {"name": "Playlist 3"},
            ]
            mock_tidal_playlists.return_value = {}

            mock_tidal = MagicMock()

            events = []
            async for event in run_sync_streaming(
                spotify_token="test_token",
                tidal_session=mock_tidal,
                sync_playlists=True,
                do_sync_favorites=False,
                do_sync_albums=False,
                do_sync_artists=False,
            ):
                events.append(event)

            # Parse events
            parsed = [json.loads(e["data"]) for e in events]

            # Should have: start, progress x3, done, complete
            assert parsed[0]["type"] == "start"
            assert parsed[0]["task"] == "playlists"

            # 3 progress events
            progress_events = [e for e in parsed if e["type"] == "progress"]
            assert len(progress_events) == 3
            assert progress_events[0]["percent"] == 33
            assert progress_events[1]["percent"] == 66
            assert progress_events[2]["percent"] == 100

            # Done and complete
            assert parsed[-2]["type"] == "done"
            assert parsed[-1]["type"] == "complete"

    @pytest.mark.asyncio
    async def test_streaming_event_format(self):
        """Events should have correct SSE format."""
        from app.sync import run_sync_streaming

        with patch('app.sync.get_playlists_from_spotify', new_callable=AsyncMock) as mock_playlists, \
             patch('app.sync.get_tidal_playlists_wrapper') as mock_tidal_playlists, \
             patch('app.sync.REQUEST_DELAY', 0):

            mock_playlists.return_value = []
            mock_tidal_playlists.return_value = {}

            mock_tidal = MagicMock()

            async for event in run_sync_streaming(
                spotify_token="test_token",
                tidal_session=mock_tidal,
                sync_playlists=True,
                do_sync_favorites=False,
                do_sync_albums=False,
                do_sync_artists=False,
            ):
                # Each event should be a dict with 'event' and 'data' keys
                assert "event" in event
                assert "data" in event
                assert event["event"] == "message"
                # Data should be valid JSON
                data = json.loads(event["data"])
                assert "type" in data
                break  # Just check first event

    @pytest.mark.asyncio
    async def test_streaming_handles_errors(self):
        """Should yield error events when sync fails."""
        from app.sync import run_sync_streaming

        with patch('app.sync.get_playlists_from_spotify', new_callable=AsyncMock) as mock_playlists, \
             patch('app.sync.REQUEST_DELAY', 0):

            mock_playlists.side_effect = Exception("API Error")

            mock_tidal = MagicMock()

            events = []
            async for event in run_sync_streaming(
                spotify_token="test_token",
                tidal_session=mock_tidal,
                sync_playlists=True,
                do_sync_favorites=False,
                do_sync_albums=False,
                do_sync_artists=False,
            ):
                events.append(event)

            parsed = [json.loads(e["data"]) for e in events]

            # Should have start, error, complete
            assert parsed[0]["type"] == "start"
            assert parsed[1]["type"] == "error"
            assert "API Error" in parsed[1]["error"]
            assert parsed[2]["type"] == "complete"


class TestSSEEndpoint:
    """Tests for the /sync/stream SSE endpoint."""

    def test_sync_stream_requires_auth(self, client):
        """Should return 400 when not authenticated."""
        response = client.get("/sync/stream?playlists=true")
        assert response.status_code == 400

    def test_sync_stream_requires_spotify(self, client, authenticated_session_tidal_only):
        """Should return 400 when Spotify not connected."""
        response = client.get("/sync/stream?playlists=true")
        assert response.status_code == 400
        assert "Spotify" in response.json()["detail"]

    def test_sync_stream_requires_tidal(self, client, authenticated_session_spotify_only):
        """Should return 400 when Tidal not connected."""
        response = client.get("/sync/stream?playlists=true")
        assert response.status_code == 400
        assert "Tidal" in response.json()["detail"]

    def test_sync_stream_returns_event_stream(self, client, authenticated_session):
        """Should return text/event-stream content type."""
        with patch('app.sync.get_playlists_from_spotify', new_callable=AsyncMock) as mock_playlists, \
             patch('app.sync.get_tidal_playlists_wrapper') as mock_tidal_playlists, \
             patch('app.sync.REQUEST_DELAY', 0):

            mock_playlists.return_value = []
            mock_tidal_playlists.return_value = {}

            response = client.get("/sync/stream?playlists=true")
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]

    def test_sync_stream_yields_events(self, client, authenticated_session):
        """Should yield SSE events."""
        with patch('app.sync.get_playlists_from_spotify', new_callable=AsyncMock) as mock_playlists, \
             patch('app.sync.get_tidal_playlists_wrapper') as mock_tidal_playlists, \
             patch('app.sync.REQUEST_DELAY', 0):

            mock_playlists.return_value = [{"name": "Test Playlist"}]
            mock_tidal_playlists.return_value = {}

            with patch('app.sync.sync_playlist', new_callable=AsyncMock):
                response = client.get("/sync/stream?playlists=true")

                # Response should contain SSE formatted data
                content = response.text
                assert "data:" in content

                # Parse out the data lines
                lines = content.strip().split("\n")
                data_lines = [l for l in lines if l.startswith("data:")]

                assert len(data_lines) > 0

                # First event should be start
                first_data = json.loads(data_lines[0].replace("data: ", "").replace("data:", ""))
                assert first_data["type"] == "start"
                assert first_data["task"] == "playlists"

"""
Sync logic - thin wrapper around spotify_to_tidal library.
"""

import asyncio
import json
from typing import Any, AsyncGenerator

import spotipy
import tidalapi

from spotify_to_tidal.sync import (
    sync_playlist,
    sync_favorites as lib_sync_favorites,
    sync_albums as lib_sync_albums,
    sync_artists as lib_sync_artists,
    get_playlists_from_spotify,
    get_tidal_playlists_wrapper,
)


def make_event(data: dict) -> str:
    """Create SSE event string with retry hint for proper streaming."""
    # Include retry hint to help with buffering issues
    return f"retry: 100\ndata: {json.dumps(data)}\n\n"


async def run_sync_streaming(
    spotify_token: str,
    tidal_session: tidalapi.Session,
    sync_playlists: bool = True,
    do_sync_albums: bool = True,
    do_sync_artists: bool = True,
    do_sync_favorites: bool = True,
) -> AsyncGenerator[str, None]:
    """Run the full sync process, yielding progress events."""
    spotify = spotipy.Spotify(auth=spotify_token)
    config = {}
    result = {}

    # Playlists - we can show per-playlist progress
    if sync_playlists:
        yield make_event({"type": "start", "task": "playlists", "label": "Playlists"})
        await asyncio.sleep(0)  # Yield control to event loop
        try:
            playlists = await get_playlists_from_spotify(spotify, config)
            tidal_playlists = get_tidal_playlists_wrapper(tidal_session)
            total = len(playlists)

            for i, spotify_playlist in enumerate(playlists):
                yield make_event({
                    "type": "progress",
                    "task": "playlists",
                    "current": i + 1,
                    "total": total,
                    "percent": int((i + 1) / total * 100),
                    "item": spotify_playlist['name']
                })
                await asyncio.sleep(0)  # Yield control
                tidal_playlist = tidal_playlists.get(spotify_playlist['name'])
                await sync_playlist(spotify, tidal_session, spotify_playlist, tidal_playlist, config)

            result['playlists'] = {'synced': total, 'not_found': []}
            yield make_event({"type": "done", "task": "playlists", "result": result['playlists']})
            await asyncio.sleep(0)
        except Exception as e:
            result['playlists'] = {'error': str(e)}
            yield make_event({"type": "error", "task": "playlists", "error": str(e)})
            await asyncio.sleep(0)

    # Favorites
    if do_sync_favorites:
        yield make_event({"type": "start", "task": "favorites", "label": "Liked Songs"})
        await asyncio.sleep(0)
        try:
            await lib_sync_favorites(spotify, tidal_session, config)
            result['favorites'] = {'added': 0, 'not_found': []}
            yield make_event({"type": "done", "task": "favorites", "result": result['favorites']})
            await asyncio.sleep(0)
        except Exception as e:
            result['favorites'] = {'error': str(e)}
            yield make_event({"type": "error", "task": "favorites", "error": str(e)})
            await asyncio.sleep(0)

    # Albums
    if do_sync_albums:
        yield make_event({"type": "start", "task": "albums", "label": "Albums"})
        await asyncio.sleep(0)
        try:
            await lib_sync_albums(spotify, tidal_session, config)
            result['albums'] = {'added': 0, 'not_found': []}
            yield make_event({"type": "done", "task": "albums", "result": result['albums']})
            await asyncio.sleep(0)
        except Exception as e:
            result['albums'] = {'error': str(e)}
            yield make_event({"type": "error", "task": "albums", "error": str(e)})
            await asyncio.sleep(0)

    # Artists
    if do_sync_artists:
        yield make_event({"type": "start", "task": "artists", "label": "Artists"})
        await asyncio.sleep(0)
        try:
            await lib_sync_artists(spotify, tidal_session, config)
            result['artists'] = {'added': 0, 'not_found': []}
            yield make_event({"type": "done", "task": "artists", "result": result['artists']})
            await asyncio.sleep(0)
        except Exception as e:
            result['artists'] = {'error': str(e)}
            yield make_event({"type": "error", "task": "artists", "error": str(e)})
            await asyncio.sleep(0)

    # Final complete event
    yield make_event({"type": "complete", "result": result})


async def run_sync(
    spotify_token: str,
    tidal_session: tidalapi.Session,
    sync_playlists: bool = True,
    do_sync_albums: bool = True,
    do_sync_artists: bool = True,
    do_sync_favorites: bool = True,
) -> dict[str, Any]:
    """Run the full sync process (non-streaming version)."""
    result = {}
    async for event_str in run_sync_streaming(
        spotify_token, tidal_session,
        sync_playlists, do_sync_albums, do_sync_artists, do_sync_favorites
    ):
        data = json.loads(event_str.replace("data: ", "").strip())
        if data.get("type") == "complete":
            result = data.get("result", {})
    return result

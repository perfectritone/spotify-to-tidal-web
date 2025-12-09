"""
Sync logic - thin wrapper around spotify_to_tidal library.
"""

from typing import Any

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


async def run_sync(
    spotify_token: str,
    tidal_session: tidalapi.Session,
    sync_playlists: bool = True,
    do_sync_albums: bool = True,
    do_sync_artists: bool = True,
    do_sync_favorites: bool = True,
) -> dict[str, Any]:
    """Run the full sync process."""
    # Create spotipy session from OAuth token
    spotify = spotipy.Spotify(auth=spotify_token)

    # Config dict expected by library functions
    config = {}

    result = {}

    if sync_playlists:
        try:
            playlists = await get_playlists_from_spotify(spotify, config)
            tidal_playlists = get_tidal_playlists_wrapper(tidal_session)

            for spotify_playlist in playlists:
                tidal_playlist = tidal_playlists.get(spotify_playlist['name'])
                await sync_playlist(spotify, tidal_session, spotify_playlist, tidal_playlist, config)

            result['playlists'] = {
                'synced': len(playlists),
                'not_found': [],  # TODO: capture from sync output
            }
        except Exception as e:
            result['playlists'] = {'error': str(e)}

    if do_sync_favorites:
        try:
            await lib_sync_favorites(spotify, tidal_session, config)
            result['favorites'] = {'added': 0, 'not_found': []}  # TODO: capture counts
        except Exception as e:
            result['favorites'] = {'error': str(e)}

    if do_sync_albums:
        try:
            await lib_sync_albums(spotify, tidal_session, config)
            result['albums'] = {'added': 0, 'not_found': []}  # TODO: capture counts
        except Exception as e:
            result['albums'] = {'error': str(e)}

    if do_sync_artists:
        try:
            await lib_sync_artists(spotify, tidal_session, config)
            result['artists'] = {'added': 0, 'not_found': []}  # TODO: capture counts
        except Exception as e:
            result['artists'] = {'error': str(e)}

    return result

"""
Sync logic - with progress streaming for the web app.
"""

import asyncio
import json
import math
from typing import Any, AsyncGenerator, Callable, List

import spotipy
import tidalapi

from spotify_to_tidal.sync import (
    sync_playlist,
    get_playlists_from_spotify,
    check_album_similarity,
    simple,
    normalize,
    track_match_cache,
    populate_track_match_cache,
    search_new_tracks_on_tidal,
)
from spotify_to_tidal.tidalapi_patch import get_all_favorites, get_all_playlists

# Delay between API operations to avoid rate limiting (in seconds)
REQUEST_DELAY = 0.5
# Delay between Spotify API calls to avoid 429 errors
SPOTIFY_DELAY = 0.2

# File where library writes not-found songs
NOT_FOUND_FILE = "songs not found.txt"


def read_and_clear_not_found_file() -> List[str]:
    """Read the not-found songs file and clear it."""
    import os
    try:
        if os.path.exists(NOT_FOUND_FILE):
            with open(NOT_FOUND_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            # Clear the file
            os.remove(NOT_FOUND_FILE)
            return content.strip().split("\n") if content.strip() else []
    except Exception:
        pass
    return []


def format_not_found_report(result: dict) -> str:
    """Format all not-found items into a readable text report."""
    lines = []
    lines.append("=" * 50)
    lines.append("SPOTIFY TO TIDAL - NOT FOUND ITEMS")
    lines.append("=" * 50)
    lines.append("")

    has_items = False

    # Playlist tracks (from the library's file)
    if result.get("playlist_tracks_not_found"):
        has_items = True
        lines.append("-" * 40)
        lines.append("PLAYLIST TRACKS")
        lines.append("-" * 40)
        for line in result["playlist_tracks_not_found"]:
            if line.startswith("="):
                lines.append("")
                lines.append(line)
            elif line.startswith("Playlist:"):
                lines.append(line)
                lines.append("")
            elif line.strip():
                lines.append(f"  • {line}")
        lines.append("")

    # Liked songs
    if result.get("favorites", {}).get("not_found"):
        has_items = True
        lines.append("-" * 40)
        lines.append("LIKED SONGS")
        lines.append("-" * 40)
        for track in result["favorites"]["not_found"]:
            lines.append(f"  • {track}")
        lines.append("")

    # Albums
    if result.get("albums", {}).get("not_found"):
        has_items = True
        lines.append("-" * 40)
        lines.append("ALBUMS")
        lines.append("-" * 40)
        for album in result["albums"]["not_found"]:
            lines.append(f"  • {album}")
        lines.append("")

    # Artists
    if result.get("artists", {}).get("not_found"):
        has_items = True
        lines.append("-" * 40)
        lines.append("ARTISTS")
        lines.append("-" * 40)
        for artist in result["artists"]["not_found"]:
            lines.append(f"  • {artist}")
        lines.append("")

    if not has_items:
        lines.append("No items were missing - everything synced successfully!")

    lines.append("")
    lines.append("=" * 50)

    return "\n".join(lines)


async def fetch_spotify_saved_tracks(spotify: spotipy.Spotify) -> List[dict]:
    """Fetch all saved tracks from Spotify with rate limiting."""
    tracks = []
    results = spotify.current_user_saved_tracks(limit=50)
    tracks.extend([item['track'] for item in results['items'] if item['track'] is not None])

    while results['next']:
        await asyncio.sleep(SPOTIFY_DELAY)
        results = spotify.next(results)
        tracks.extend([item['track'] for item in results['items'] if item['track'] is not None])

    return tracks


async def fetch_spotify_saved_albums(spotify: spotipy.Spotify) -> List[dict]:
    """Fetch all saved albums from Spotify with rate limiting."""
    albums = []
    results = spotify.current_user_saved_albums(limit=50)
    albums.extend([item['album'] for item in results['items']])

    while results['next']:
        await asyncio.sleep(SPOTIFY_DELAY)
        results = spotify.next(results)
        albums.extend([item['album'] for item in results['items']])

    albums.reverse()  # Chronological order
    return albums


async def fetch_spotify_followed_artists(spotify: spotipy.Spotify) -> List[dict]:
    """Fetch all followed artists from Spotify with rate limiting."""
    artists = []
    results = spotify.current_user_followed_artists(limit=50)
    artists.extend(results['artists']['items'])

    while results['artists']['next']:
        await asyncio.sleep(SPOTIFY_DELAY)
        last_id = artists[-1]['id'] if artists else None
        results = spotify.current_user_followed_artists(limit=50, after=last_id)
        artists.extend(results['artists']['items'])

    return artists


async def run_sync_streaming(
    spotify_token: str,
    tidal_session: tidalapi.Session,
    sync_playlists: bool = True,
    do_sync_albums: bool = True,
    do_sync_artists: bool = True,
    do_sync_favorites: bool = True,
) -> AsyncGenerator[dict, None]:
    """Run the full sync process, yielding progress events as dicts."""
    spotify = spotipy.Spotify(auth=spotify_token)
    config = {}
    result = {}

    # Progress callback that yields SSE events
    async def make_progress(task: str, event_type: str, percent: int, item: str = ""):
        pass  # This will be replaced by yielding

    # We need a different approach - collect events in a queue
    event_queue = asyncio.Queue()

    async def progress_callback(task: str, event_type: str, percent: int, item: str = ""):
        await event_queue.put({
            "event": "message",
            "data": json.dumps({
                "type": event_type,
                "task": task,
                "percent": percent,
                "item": item,
            })
        })

    # Playlists - we can show per-playlist progress
    if sync_playlists:
        yield {"event": "message", "data": json.dumps({"type": "start", "task": "playlists", "label": "Playlists"})}
        await asyncio.sleep(REQUEST_DELAY)
        try:
            playlists = await get_playlists_from_spotify(spotify, config)
            # Get Tidal playlists (await directly instead of using wrapper with asyncio.run)
            tidal_playlist_list = await get_all_playlists(tidal_session.user)
            tidal_playlists = {p.name: p for p in tidal_playlist_list}
            total = len(playlists)

            for i, spotify_playlist in enumerate(playlists):
                pct = int((i + 1) / total * 100) if total > 0 else 100
                yield {"event": "message", "data": json.dumps({
                    "type": "progress",
                    "task": "playlists",
                    "current": i + 1,
                    "total": total,
                    "percent": pct,
                    "item": spotify_playlist['name']
                })}
                await asyncio.sleep(REQUEST_DELAY)
                tidal_playlist = tidal_playlists.get(spotify_playlist['name'])
                await sync_playlist(spotify, tidal_session, spotify_playlist, tidal_playlist, config)

            # Read any not-found tracks from the library's file
            playlist_not_found = read_and_clear_not_found_file()
            result['playlists'] = {'synced': total, 'not_found': playlist_not_found}
            result['playlist_tracks_not_found'] = playlist_not_found
            yield {"event": "message", "data": json.dumps({"type": "done", "task": "playlists", "result": result['playlists']})}
            await asyncio.sleep(REQUEST_DELAY)
        except Exception as e:
            result['playlists'] = {'error': str(e)}
            yield {"event": "message", "data": json.dumps({"type": "error", "task": "playlists", "error": str(e)})}
            await asyncio.sleep(REQUEST_DELAY)

    # Favorites - with granular progress
    if do_sync_favorites:
        yield {"event": "message", "data": json.dumps({"type": "start", "task": "favorites", "label": "Liked Songs"})}
        await asyncio.sleep(REQUEST_DELAY)
        try:
            # Use inline progress reporting
            # Step 1: Fetch from Spotify (sequential to avoid rate limits)
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "favorites", "percent": 5, "item": "Loading from Spotify..."})}

            spotify_tracks = await fetch_spotify_saved_tracks(spotify)
            spotify_tracks.reverse()
            total_tracks = len(spotify_tracks)

            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "favorites", "percent": 15, "item": f"Found {total_tracks} tracks"})}
            await asyncio.sleep(REQUEST_DELAY)

            # Step 2: Fetch existing from Tidal
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "favorites", "percent": 20, "item": "Loading Tidal favorites..."})}
            old_tidal_tracks = await get_all_favorites(tidal_session.user.favorites, order='DATE')

            # Step 3: Match existing
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "favorites", "percent": 25, "item": "Matching tracks..."})}
            populate_track_match_cache(spotify_tracks, old_tidal_tracks)

            # Step 4: Search Tidal
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "favorites", "percent": 30, "item": "Searching Tidal..."})}
            await search_new_tracks_on_tidal(tidal_session, spotify_tracks, "Favorites", config)

            # Read not-found tracks from library's file (written by search_new_tracks_on_tidal)
            favorites_not_found = read_and_clear_not_found_file()

            # Step 5: Get new tracks
            existing_favorite_ids = set([track.id for track in old_tidal_tracks])
            new_ids = []
            for spotify_track in spotify_tracks:
                if spotify_track.get('id'):
                    match_id = track_match_cache.get(spotify_track['id'])
                    if match_id and match_id not in existing_favorite_ids:
                        new_ids.append(match_id)

            # Step 6: Add tracks
            added = 0
            total_to_add = len(new_ids)
            if total_to_add > 0:
                for i, tidal_id in enumerate(new_ids):
                    try:
                        tidal_session.user.favorites.add_track(tidal_id)
                        added += 1
                    except Exception:
                        pass
                    pct = 50 + int((i + 1) / total_to_add * 50)
                    yield {"event": "message", "data": json.dumps({"type": "progress", "task": "favorites", "percent": pct, "item": f"Adding {i+1}/{total_to_add}"})}
                    await asyncio.sleep(REQUEST_DELAY)

            # Parse the not-found file content (filter out headers)
            favorites_not_found_clean = [
                line for line in favorites_not_found
                if line and not line.startswith('=') and not line.startswith('Playlist:') and line.strip()
            ]
            result['favorites'] = {'added': added, 'total': total_tracks, 'not_found': favorites_not_found_clean}
            yield {"event": "message", "data": json.dumps({"type": "done", "task": "favorites", "result": result['favorites']})}
            await asyncio.sleep(REQUEST_DELAY)
        except Exception as e:
            result['favorites'] = {'error': str(e)}
            yield {"event": "message", "data": json.dumps({"type": "error", "task": "favorites", "error": str(e)})}
            await asyncio.sleep(REQUEST_DELAY)

    # Albums - with granular progress
    if do_sync_albums:
        yield {"event": "message", "data": json.dumps({"type": "start", "task": "albums", "label": "Albums"})}
        await asyncio.sleep(REQUEST_DELAY)
        try:
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "albums", "percent": 5, "item": "Loading from Spotify..."})}
            spotify_albums = await fetch_spotify_saved_albums(spotify)
            total_albums = len(spotify_albums)

            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "albums", "percent": 15, "item": f"Found {total_albums} albums"})}
            await asyncio.sleep(REQUEST_DELAY)

            # Get existing Tidal albums
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "albums", "percent": 18, "item": "Loading Tidal albums..."})}
            tidal_favorite_albums = set()
            try:
                tidal_albums = tidal_session.user.favorites.albums()
                for album in tidal_albums:
                    tidal_favorite_albums.add(album.id)
            except Exception:
                pass

            # Sync each album
            added = 0
            not_found = []

            for i, spotify_album in enumerate(spotify_albums):
                album_name = spotify_album['name']
                artist_name = spotify_album['artists'][0]['name'] if spotify_album.get('artists') else ''

                pct = 20 + int((i + 1) / total_albums * 80) if total_albums > 0 else 100
                yield {"event": "message", "data": json.dumps({"type": "progress", "task": "albums", "percent": pct, "item": f"{i+1}/{total_albums}: {album_name[:25]}"})}

                query = f"{simple(album_name)} {simple(artist_name)}"
                try:
                    search_results = tidal_session.search(query, models=[tidalapi.album.Album])
                    matched = False

                    for tidal_album in search_results.get('albums', []):
                        if check_album_similarity(spotify_album, tidal_album):
                            if tidal_album.id not in tidal_favorite_albums:
                                try:
                                    tidal_session.user.favorites.add_album(tidal_album.id)
                                    tidal_favorite_albums.add(tidal_album.id)
                                    added += 1
                                except Exception:
                                    pass
                            matched = True
                            break

                    if not matched:
                        not_found.append(f"{artist_name} - {album_name}")
                except Exception:
                    not_found.append(f"{artist_name} - {album_name}")

                await asyncio.sleep(REQUEST_DELAY)

            result['albums'] = {'added': added, 'total': total_albums, 'not_found': not_found}
            yield {"event": "message", "data": json.dumps({"type": "done", "task": "albums", "result": result['albums']})}
            await asyncio.sleep(REQUEST_DELAY)
        except Exception as e:
            result['albums'] = {'error': str(e)}
            yield {"event": "message", "data": json.dumps({"type": "error", "task": "albums", "error": str(e)})}
            await asyncio.sleep(REQUEST_DELAY)

    # Artists - with granular progress
    if do_sync_artists:
        yield {"event": "message", "data": json.dumps({"type": "start", "task": "artists", "label": "Artists"})}
        await asyncio.sleep(REQUEST_DELAY)
        try:
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "artists", "percent": 5, "item": "Loading from Spotify..."})}
            spotify_artists = await fetch_spotify_followed_artists(spotify)
            total_artists = len(spotify_artists)

            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "artists", "percent": 15, "item": f"Found {total_artists} artists"})}
            await asyncio.sleep(REQUEST_DELAY)

            # Get existing Tidal artists
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "artists", "percent": 18, "item": "Loading Tidal artists..."})}
            tidal_favorite_artists = set()
            try:
                tidal_artists = tidal_session.user.favorites.artists()
                for artist in tidal_artists:
                    tidal_favorite_artists.add(artist.id)
            except Exception:
                pass

            # Sync each artist
            added = 0
            not_found = []

            for i, spotify_artist in enumerate(spotify_artists):
                artist_name = spotify_artist['name']

                pct = 20 + int((i + 1) / total_artists * 80) if total_artists > 0 else 100
                yield {"event": "message", "data": json.dumps({"type": "progress", "task": "artists", "percent": pct, "item": f"{i+1}/{total_artists}: {artist_name[:25]}"})}

                query = simple(artist_name)
                try:
                    search_results = tidal_session.search(query, models=[tidalapi.artist.Artist])
                    matched = False

                    for tidal_artist in search_results.get('artists', []):
                        if normalize(simple(tidal_artist.name.lower())) == normalize(simple(artist_name.lower())):
                            if tidal_artist.id not in tidal_favorite_artists:
                                try:
                                    tidal_session.user.favorites.add_artist(tidal_artist.id)
                                    tidal_favorite_artists.add(tidal_artist.id)
                                    added += 1
                                except Exception:
                                    pass
                            matched = True
                            break

                    if not matched:
                        not_found.append(artist_name)
                except Exception:
                    not_found.append(artist_name)

                await asyncio.sleep(REQUEST_DELAY)

            result['artists'] = {'added': added, 'total': total_artists, 'not_found': not_found}
            yield {"event": "message", "data": json.dumps({"type": "done", "task": "artists", "result": result['artists']})}
            await asyncio.sleep(REQUEST_DELAY)
        except Exception as e:
            result['artists'] = {'error': str(e)}
            yield {"event": "message", "data": json.dumps({"type": "error", "task": "artists", "error": str(e)})}
            await asyncio.sleep(REQUEST_DELAY)

    # Generate the not-found report
    report = format_not_found_report(result)
    result['not_found_report'] = report

    # Final complete event
    yield {"event": "message", "data": json.dumps({"type": "complete", "result": result})}


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
    async for event in run_sync_streaming(
        spotify_token, tidal_session,
        sync_playlists, do_sync_albums, do_sync_artists, do_sync_favorites
    ):
        data = json.loads(event["data"])
        if data.get("type") == "complete":
            result = data.get("result", {})
    return result

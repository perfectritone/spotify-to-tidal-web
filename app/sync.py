"""
Sync logic - with progress streaming for the web app.
Memory-optimized: processes items in batches, doesn't hold full objects in memory.
"""

import asyncio
import gc
import json
from typing import Any, AsyncGenerator, List

import spotipy
import tidalapi

from spotify_to_tidal.sync import (
    sync_playlist,
    get_playlists_from_spotify,
    check_album_similarity,
    simple,
    normalize,
)
from spotify_to_tidal.tidalapi_patch import get_all_playlists

# Delay between API operations to avoid rate limiting (in seconds)
REQUEST_DELAY = 0.5
# Delay between Spotify API calls to avoid 429 errors
SPOTIFY_DELAY = 0.2
# Batch size for processing
BATCH_SIZE = 50

# File where library writes not-found songs
NOT_FOUND_FILE = "songs not found.txt"


def read_and_clear_not_found_file() -> List[str]:
    """Read the not-found songs file and clear it."""
    import os
    try:
        if os.path.exists(NOT_FOUND_FILE):
            with open(NOT_FOUND_FILE, "r", encoding="utf-8") as f:
                content = f.read()
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

    if result.get("favorites", {}).get("not_found"):
        has_items = True
        lines.append("-" * 40)
        lines.append("LIKED SONGS")
        lines.append("-" * 40)
        for track in result["favorites"]["not_found"]:
            lines.append(f"  • {track}")
        lines.append("")

    if result.get("albums", {}).get("not_found"):
        has_items = True
        lines.append("-" * 40)
        lines.append("ALBUMS")
        lines.append("-" * 40)
        for album in result["albums"]["not_found"]:
            lines.append(f"  • {album}")
        lines.append("")

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


async def iter_spotify_saved_albums(spotify: spotipy.Spotify):
    """Yield saved albums from Spotify one at a time (memory efficient)."""
    results = spotify.current_user_saved_albums(limit=BATCH_SIZE)
    for item in results['items']:
        album = item['album']
        # Only yield the data we need
        yield {
            'name': album['name'],
            'artists': [{'name': a['name']} for a in album.get('artists', [])],
            'release_date': album.get('release_date'),
            'total_tracks': album.get('total_tracks'),
        }

    while results['next']:
        await asyncio.sleep(SPOTIFY_DELAY)
        results = spotify.next(results)
        for item in results['items']:
            album = item['album']
            yield {
                'name': album['name'],
                'artists': [{'name': a['name']} for a in album.get('artists', [])],
                'release_date': album.get('release_date'),
                'total_tracks': album.get('total_tracks'),
            }


async def iter_spotify_followed_artists(spotify: spotipy.Spotify):
    """Yield followed artists from Spotify one at a time (memory efficient)."""
    results = spotify.current_user_followed_artists(limit=BATCH_SIZE)
    last_id = None
    for artist in results['artists']['items']:
        last_id = artist['id']
        yield {'id': artist['id'], 'name': artist['name']}

    while results['artists']['next']:
        await asyncio.sleep(SPOTIFY_DELAY)
        results = spotify.current_user_followed_artists(limit=BATCH_SIZE, after=last_id)
        for artist in results['artists']['items']:
            last_id = artist['id']
            yield {'id': artist['id'], 'name': artist['name']}


async def iter_spotify_saved_tracks(spotify: spotipy.Spotify):
    """Yield saved tracks from Spotify one at a time (memory efficient)."""
    results = spotify.current_user_saved_tracks(limit=BATCH_SIZE)
    for item in results['items']:
        track = item.get('track')
        if track:
            yield {
                'id': track['id'],
                'name': track['name'],
                'artists': [{'name': a['name']} for a in track.get('artists', [])],
            }

    while results['next']:
        await asyncio.sleep(SPOTIFY_DELAY)
        results = spotify.next(results)
        for item in results['items']:
            track = item.get('track')
            if track:
                yield {
                    'id': track['id'],
                    'name': track['name'],
                    'artists': [{'name': a['name']} for a in track.get('artists', [])],
                }


async def count_spotify_items(spotify: spotipy.Spotify, item_type: str) -> int:
    """Get count of items without loading all data."""
    if item_type == 'tracks':
        results = spotify.current_user_saved_tracks(limit=1)
        return results.get('total', 0)
    elif item_type == 'albums':
        results = spotify.current_user_saved_albums(limit=1)
        return results.get('total', 0)
    elif item_type == 'artists':
        results = spotify.current_user_followed_artists(limit=1)
        return results.get('artists', {}).get('total', 0)
    return 0


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

    # Playlists
    if sync_playlists:
        yield {"event": "message", "data": json.dumps({"type": "start", "task": "playlists", "label": "Playlists"})}
        await asyncio.sleep(REQUEST_DELAY)
        try:
            playlists = await get_playlists_from_spotify(spotify, config)
            tidal_playlist_list = await get_all_playlists(tidal_session.user)
            tidal_playlists = {p.name: p for p in tidal_playlist_list}
            del tidal_playlist_list  # Free memory
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
                try:
                    # Run sync_playlist with a wrapper that sends heartbeat events
                    # The library's sync can take a while, so we need to keep the connection alive
                    await sync_playlist(spotify, tidal_session, spotify_playlist, tidal_playlist, config)
                except Exception as playlist_err:
                    import traceback
                    print(f"Error syncing playlist {spotify_playlist['name']}: {playlist_err}")
                    traceback.print_exc()
                    # Continue with other playlists instead of failing completely

                # Small delay and free memory between playlists
                gc.collect()

            del playlists, tidal_playlists  # Free memory
            gc.collect()

            playlist_not_found = read_and_clear_not_found_file()
            result['playlists'] = {'synced': total, 'not_found': playlist_not_found}
            result['playlist_tracks_not_found'] = playlist_not_found
            yield {"event": "message", "data": json.dumps({"type": "done", "task": "playlists", "result": result['playlists']})}
            await asyncio.sleep(REQUEST_DELAY)
        except Exception as e:
            import traceback
            print(f"Playlist sync error: {e}")
            traceback.print_exc()
            result['playlists'] = {'error': str(e)}
            yield {"event": "message", "data": json.dumps({"type": "error", "task": "playlists", "error": str(e)})}
            await asyncio.sleep(REQUEST_DELAY)

    # Favorites - process in batches
    if do_sync_favorites:
        yield {"event": "message", "data": json.dumps({"type": "start", "task": "favorites", "label": "Liked Songs"})}
        await asyncio.sleep(REQUEST_DELAY)
        try:
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "favorites", "percent": 5, "item": "Counting tracks..."})}
            total_tracks = await count_spotify_items(spotify, 'tracks')

            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "favorites", "percent": 10, "item": f"Found {total_tracks} tracks"})}
            await asyncio.sleep(REQUEST_DELAY)

            # Get existing Tidal favorites (just IDs) - this can be slow
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "favorites", "percent": 12, "item": "Loading Tidal favorites (may take a moment)..."})}
            existing_tidal_ids = set()
            try:
                # Fetch in smaller batches to avoid timeout
                offset = 0
                batch_size = 100
                while True:
                    tidal_batch = tidal_session.user.favorites.tracks(limit=batch_size, offset=offset)
                    if not tidal_batch:
                        break
                    for track in tidal_batch:
                        existing_tidal_ids.add(track.id)
                    if len(tidal_batch) < batch_size:
                        break
                    offset += batch_size
                    # Send progress to keep connection alive
                    yield {"event": "message", "data": json.dumps({"type": "progress", "task": "favorites", "percent": 15, "item": f"Loaded {len(existing_tidal_ids)} Tidal favorites..."})}
                    await asyncio.sleep(0.1)
            except Exception as e:
                print(f"Error loading Tidal favorites: {e}")

            # Process tracks in streaming fashion
            added = 0
            not_found = []
            processed = 0

            async for track in iter_spotify_saved_tracks(spotify):
                processed += 1
                pct = 20 + int(processed / max(total_tracks, 1) * 75)

                if processed % 10 == 0:
                    yield {"event": "message", "data": json.dumps({"type": "progress", "task": "favorites", "percent": pct, "item": f"Processing {processed}/{total_tracks}"})}

                # Search for track on Tidal
                artist_name = track['artists'][0]['name'] if track.get('artists') else ''
                query = f"{simple(track['name'])} {simple(artist_name)}"

                try:
                    search_results = tidal_session.search(query, models=[tidalapi.media.Track], limit=5)
                    matched = False

                    for tidal_track in search_results.get('tracks', [])[:5]:
                        # Simple name matching
                        if (normalize(simple(tidal_track.name.lower())) == normalize(simple(track['name'].lower())) and
                            tidal_track.id not in existing_tidal_ids):
                            try:
                                tidal_session.user.favorites.add_track(tidal_track.id)
                                existing_tidal_ids.add(tidal_track.id)
                                added += 1
                            except Exception:
                                pass
                            matched = True
                            break
                        elif tidal_track.id in existing_tidal_ids:
                            matched = True
                            break

                    if not matched:
                        not_found.append(f"{artist_name} - {track['name']}")
                except Exception:
                    not_found.append(f"{artist_name} - {track['name']}")

                await asyncio.sleep(REQUEST_DELAY)

            gc.collect()
            result['favorites'] = {'added': added, 'total': total_tracks, 'not_found': not_found}
            yield {"event": "message", "data": json.dumps({"type": "done", "task": "favorites", "result": result['favorites']})}
            await asyncio.sleep(REQUEST_DELAY)
        except Exception as e:
            result['favorites'] = {'error': str(e)}
            yield {"event": "message", "data": json.dumps({"type": "error", "task": "favorites", "error": str(e)})}
            await asyncio.sleep(REQUEST_DELAY)

    # Albums - stream and process one at a time
    if do_sync_albums:
        yield {"event": "message", "data": json.dumps({"type": "start", "task": "albums", "label": "Albums"})}
        await asyncio.sleep(REQUEST_DELAY)
        try:
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "albums", "percent": 5, "item": "Counting albums..."})}
            total_albums = await count_spotify_items(spotify, 'albums')

            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "albums", "percent": 10, "item": f"Found {total_albums} albums"})}
            await asyncio.sleep(REQUEST_DELAY)

            # Get existing Tidal albums (just IDs) - fetch in batches
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "albums", "percent": 12, "item": "Loading Tidal albums..."})}
            tidal_favorite_albums = set()
            try:
                offset = 0
                batch_size = 100
                while True:
                    tidal_batch = tidal_session.user.favorites.albums(limit=batch_size, offset=offset)
                    if not tidal_batch:
                        break
                    for album in tidal_batch:
                        tidal_favorite_albums.add(album.id)
                    if len(tidal_batch) < batch_size:
                        break
                    offset += batch_size
                    yield {"event": "message", "data": json.dumps({"type": "progress", "task": "albums", "percent": 15, "item": f"Loaded {len(tidal_favorite_albums)} Tidal albums..."})}
                    await asyncio.sleep(0.1)
            except Exception as e:
                print(f"Error loading Tidal albums: {e}")

            added = 0
            not_found = []
            processed = 0

            async for spotify_album in iter_spotify_saved_albums(spotify):
                processed += 1
                album_name = spotify_album['name']
                artist_name = spotify_album['artists'][0]['name'] if spotify_album.get('artists') else ''

                pct = 20 + int(processed / max(total_albums, 1) * 80)
                yield {"event": "message", "data": json.dumps({"type": "progress", "task": "albums", "percent": pct, "item": f"{processed}/{total_albums}: {album_name[:25]}"})}

                query = f"{simple(album_name)} {simple(artist_name)}"
                try:
                    search_results = tidal_session.search(query, models=[tidalapi.album.Album], limit=5)
                    matched = False

                    for tidal_album in search_results.get('albums', [])[:5]:
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

            gc.collect()
            result['albums'] = {'added': added, 'total': total_albums, 'not_found': not_found}
            yield {"event": "message", "data": json.dumps({"type": "done", "task": "albums", "result": result['albums']})}
            await asyncio.sleep(REQUEST_DELAY)
        except Exception as e:
            result['albums'] = {'error': str(e)}
            yield {"event": "message", "data": json.dumps({"type": "error", "task": "albums", "error": str(e)})}
            await asyncio.sleep(REQUEST_DELAY)

    # Artists - stream and process one at a time
    if do_sync_artists:
        yield {"event": "message", "data": json.dumps({"type": "start", "task": "artists", "label": "Artists"})}
        await asyncio.sleep(REQUEST_DELAY)
        try:
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "artists", "percent": 5, "item": "Counting artists..."})}
            total_artists = await count_spotify_items(spotify, 'artists')

            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "artists", "percent": 10, "item": f"Found {total_artists} artists"})}
            await asyncio.sleep(REQUEST_DELAY)

            # Get existing Tidal artists (just IDs) - fetch in batches
            yield {"event": "message", "data": json.dumps({"type": "progress", "task": "artists", "percent": 12, "item": "Loading Tidal artists..."})}
            tidal_favorite_artists = set()
            try:
                offset = 0
                batch_size = 100
                while True:
                    tidal_batch = tidal_session.user.favorites.artists(limit=batch_size, offset=offset)
                    if not tidal_batch:
                        break
                    for artist in tidal_batch:
                        tidal_favorite_artists.add(artist.id)
                    if len(tidal_batch) < batch_size:
                        break
                    offset += batch_size
                    yield {"event": "message", "data": json.dumps({"type": "progress", "task": "artists", "percent": 15, "item": f"Loaded {len(tidal_favorite_artists)} Tidal artists..."})}
                    await asyncio.sleep(0.1)
            except Exception as e:
                print(f"Error loading Tidal artists: {e}")

            added = 0
            not_found = []
            processed = 0

            async for spotify_artist in iter_spotify_followed_artists(spotify):
                processed += 1
                artist_name = spotify_artist['name']

                pct = 20 + int(processed / max(total_artists, 1) * 80)
                yield {"event": "message", "data": json.dumps({"type": "progress", "task": "artists", "percent": pct, "item": f"{processed}/{total_artists}: {artist_name[:25]}"})}

                query = simple(artist_name)
                try:
                    search_results = tidal_session.search(query, models=[tidalapi.artist.Artist], limit=5)
                    matched = False

                    for tidal_artist in search_results.get('artists', [])[:5]:
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

            gc.collect()
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

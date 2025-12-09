"""
Sync logic adapted from spotify_to_tidal CLI.
"""

import asyncio
from difflib import SequenceMatcher
from typing import Any
import unicodedata

import httpx
import tidalapi


def normalize(s: str) -> str:
    """Remove accents from string."""
    return unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode('ascii')


def simple(input_string: str) -> str:
    """Simplify string for matching - remove version info."""
    return input_string.split('-')[0].strip().split('(')[0].strip().split('[')[0].strip()


def artist_match(tidal_item, spotify_artists: list[dict]) -> bool:
    """Check if at least one artist matches between Tidal and Spotify."""
    tidal_artists = {simple(a.name.lower()) for a in tidal_item.artists}
    spotify_artist_names = {simple(a['name'].lower()) for a in spotify_artists}
    return bool(tidal_artists & spotify_artist_names)


def check_album_similarity(spotify_album: dict, tidal_album, threshold: float = 0.6) -> bool:
    """Check if album names are similar enough and artists match."""
    ratio = SequenceMatcher(
        None,
        simple(spotify_album['name']).lower(),
        simple(tidal_album.name).lower()
    ).ratio()
    return ratio >= threshold and artist_match(tidal_album, spotify_album.get('artists', []))


class SpotifyClient:
    """Simple Spotify API client using access token."""

    def __init__(self, access_token: str):
        self.token = access_token
        self.base_url = "https://api.spotify.com/v1"

    async def _get(self, endpoint: str, params: dict = None) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}{endpoint}",
                headers={"Authorization": f"Bearer {self.token}"},
                params=params,
            )
            response.raise_for_status()
            return response.json()

    async def get_playlists(self) -> list[dict]:
        """Get all user playlists."""
        playlists = []
        data = await self._get("/me/playlists", {"limit": 50})
        playlists.extend(data['items'])

        while data.get('next'):
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    data['next'],
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                data = response.json()
                playlists.extend(data['items'])

        return playlists

    async def get_playlist_tracks(self, playlist_id: str) -> list[dict]:
        """Get all tracks from a playlist."""
        tracks = []
        data = await self._get(f"/playlists/{playlist_id}/tracks", {"limit": 100})
        tracks.extend([item['track'] for item in data['items'] if item['track']])

        while data.get('next'):
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    data['next'],
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                data = response.json()
                tracks.extend([item['track'] for item in data['items'] if item['track']])

        return tracks

    async def get_saved_albums(self) -> list[dict]:
        """Get all saved albums."""
        albums = []
        data = await self._get("/me/albums", {"limit": 50})
        albums.extend([item['album'] for item in data['items']])

        while data.get('next'):
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    data['next'],
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                data = response.json()
                albums.extend([item['album'] for item in data['items']])

        return albums

    async def get_followed_artists(self) -> list[dict]:
        """Get all followed artists."""
        artists = []
        data = await self._get("/me/following", {"type": "artist", "limit": 50})
        artists.extend(data['artists']['items'])

        while data['artists'].get('next'):
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    data['artists']['next'],
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                data = response.json()
                artists.extend(data['artists']['items'])

        return artists

    async def get_saved_tracks(self) -> list[dict]:
        """Get all saved/liked tracks."""
        tracks = []
        data = await self._get("/me/tracks", {"limit": 50})
        tracks.extend([item['track'] for item in data['items']])

        while data.get('next'):
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    data['next'],
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                data = response.json()
                tracks.extend([item['track'] for item in data['items']])

        return tracks


async def search_track_on_tidal(
    tidal: tidalapi.Session,
    spotify_track: dict
) -> tidalapi.Track | None:
    """Search for a Spotify track on Tidal."""
    query = f"{spotify_track['name']} {spotify_track['artists'][0]['name']}"

    def _search():
        return tidal.search(simple(query), models=[tidalapi.media.Track])

    results = await asyncio.to_thread(_search)

    for track in results.get('tracks', [])[:10]:
        # Check ISRC match first
        if spotify_track.get('external_ids', {}).get('isrc') == track.isrc:
            return track

        # Check name and artist match
        if (simple(track.name.lower()) in simple(spotify_track['name'].lower()) or
            simple(spotify_track['name'].lower()) in simple(track.name.lower())):
            if artist_match(track, spotify_track['artists']):
                return track

    return None


async def sync_albums(
    spotify: SpotifyClient,
    tidal: tidalapi.Session
) -> dict[str, Any]:
    """Sync saved albums from Spotify to Tidal."""
    spotify_albums = await spotify.get_saved_albums()

    # Get existing Tidal albums
    existing = set()
    try:
        tidal_albums = await asyncio.to_thread(tidal.user.favorites.albums)
        existing = {a.id for a in tidal_albums}
    except Exception:
        pass

    added = 0
    not_found = []

    for album in spotify_albums:
        query = f"{simple(album['name'])} {simple(album['artists'][0]['name'])}"

        def _search(q=query):
            return tidal.search(q, models=[tidalapi.album.Album])

        try:
            results = await asyncio.to_thread(_search)
            matched = False

            for tidal_album in results.get('albums', [])[:10]:
                if check_album_similarity(album, tidal_album):
                    if tidal_album.id not in existing:
                        await asyncio.to_thread(tidal.user.favorites.add_album, tidal_album.id)
                        existing.add(tidal_album.id)
                        added += 1
                    matched = True
                    break

            if not matched:
                artist = album['artists'][0]['name'] if album.get('artists') else 'Unknown'
                not_found.append(f"{artist} - {album['name']}")
        except Exception as e:
            not_found.append(f"{album['artists'][0]['name']} - {album['name']}")

    return {"added": added, "not_found": not_found}


async def sync_artists(
    spotify: SpotifyClient,
    tidal: tidalapi.Session
) -> dict[str, Any]:
    """Sync followed artists from Spotify to Tidal."""
    spotify_artists = await spotify.get_followed_artists()

    # Get existing Tidal artists
    existing = set()
    try:
        tidal_artists = await asyncio.to_thread(tidal.user.favorites.artists)
        existing = {a.id for a in tidal_artists}
    except Exception:
        pass

    added = 0
    not_found = []

    for artist in spotify_artists:
        def _search(name=artist['name']):
            return tidal.search(name, models=[tidalapi.artist.Artist])

        try:
            results = await asyncio.to_thread(_search)
            matched = False

            for tidal_artist in results.get('artists', [])[:5]:
                if simple(tidal_artist.name.lower()) == simple(artist['name'].lower()):
                    if tidal_artist.id not in existing:
                        await asyncio.to_thread(tidal.user.favorites.add_artist, tidal_artist.id)
                        existing.add(tidal_artist.id)
                        added += 1
                    matched = True
                    break

            if not matched:
                not_found.append(artist['name'])
        except Exception:
            not_found.append(artist['name'])

    return {"added": added, "not_found": not_found}


async def sync_playlist(
    spotify: SpotifyClient,
    tidal: tidalapi.Session,
    spotify_playlist: dict
) -> dict[str, Any]:
    """Sync a single playlist."""
    tracks = await spotify.get_playlist_tracks(spotify_playlist['id'])

    # Find or create Tidal playlist
    tidal_playlists = await asyncio.to_thread(tidal.user.playlists)
    tidal_playlist = None
    for p in tidal_playlists:
        if p.name == spotify_playlist['name']:
            tidal_playlist = p
            break

    if not tidal_playlist:
        tidal_playlist = await asyncio.to_thread(
            tidal.user.create_playlist,
            spotify_playlist['name'],
            spotify_playlist.get('description', '')
        )

    # Get existing tracks
    existing_tracks = set()
    try:
        existing = await asyncio.to_thread(tidal_playlist.tracks)
        existing_tracks = {t.id for t in existing}
    except Exception:
        pass

    added = 0
    not_found = []
    tracks_to_add = []

    for track in tracks:
        if not track or not track.get('id'):
            continue

        tidal_track = await search_track_on_tidal(tidal, track)
        if tidal_track:
            if tidal_track.id not in existing_tracks:
                tracks_to_add.append(tidal_track.id)
                existing_tracks.add(tidal_track.id)
                added += 1
        else:
            artist = track['artists'][0]['name'] if track.get('artists') else 'Unknown'
            not_found.append(f"{artist} - {track['name']}")

    # Add tracks in batches
    if tracks_to_add:
        for i in range(0, len(tracks_to_add), 50):
            batch = tracks_to_add[i:i+50]
            await asyncio.to_thread(tidal_playlist.add, batch)

    return {"added": added, "not_found": not_found}


async def sync_favorites(
    spotify: SpotifyClient,
    tidal: tidalapi.Session
) -> dict[str, Any]:
    """Sync liked songs to Tidal favorites."""
    tracks = await spotify.get_saved_tracks()

    # Get existing favorites
    existing = set()
    try:
        favorites = await asyncio.to_thread(tidal.user.favorites.tracks)
        existing = {t.id for t in favorites}
    except Exception:
        pass

    added = 0
    not_found = []

    for track in tracks:
        if not track or not track.get('id'):
            continue

        tidal_track = await search_track_on_tidal(tidal, track)
        if tidal_track:
            if tidal_track.id not in existing:
                await asyncio.to_thread(tidal.user.favorites.add_track, tidal_track.id)
                existing.add(tidal_track.id)
                added += 1
        else:
            artist = track['artists'][0]['name'] if track.get('artists') else 'Unknown'
            not_found.append(f"{artist} - {track['name']}")

    return {"added": added, "not_found": not_found}


async def run_sync(
    spotify_token: str,
    tidal_session: tidalapi.Session,
    sync_playlists: bool = True,
    sync_albums: bool = True,
    sync_artists: bool = True,
    sync_favorites: bool = True,
) -> dict[str, Any]:
    """Run the full sync process."""
    spotify = SpotifyClient(spotify_token)
    result = {}

    if sync_playlists:
        playlists = await spotify.get_playlists()
        playlist_results = []
        total_not_found = []

        for playlist in playlists:
            res = await sync_playlist(spotify, tidal_session, playlist)
            playlist_results.append(res)
            total_not_found.extend(res['not_found'])

        result['playlists'] = {
            'synced': len(playlists),
            'not_found': total_not_found,
        }

    if sync_favorites:
        result['favorites'] = await sync_favorites(spotify, tidal_session)

    if sync_albums:
        result['albums'] = await sync_albums(spotify, tidal_session)

    if sync_artists:
        result['artists'] = await sync_artists(spotify, tidal_session)

    return result

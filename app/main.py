import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import httpx
import tidalapi
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature

from sse_starlette.sse import EventSourceResponse

from .sync import run_sync, run_sync_streaming

# Config from environment (host sets these once)
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
ANALYTICS_API_KEY = os.environ.get("ANALYTICS_API_KEY", "")
IS_PRODUCTION = BASE_URL.startswith("https://")

# Cookie max age: 30 days (Tidal tokens last ~7 days, but refresh works)
COOKIE_MAX_AGE = 30 * 24 * 3600

serializer = URLSafeTimedSerializer(SECRET_KEY)

# In-memory store only for pending Tidal auth (device flow needs future object)
pending_tidal_auth: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    pending_tidal_auth.clear()


app = FastAPI(title="Spotify to Tidal Sync", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

if ANALYTICS_API_KEY:
    from api_analytics.fastapi import Analytics
    app.add_middleware(Analytics, api_key=ANALYTICS_API_KEY)


def get_cookie_data(request: Request, name: str) -> Optional[dict]:
    """Get and verify signed cookie data."""
    cookie = request.cookies.get(name)
    if not cookie:
        return None
    try:
        return serializer.loads(cookie, max_age=COOKIE_MAX_AGE)
    except BadSignature:
        return None


def set_cookie_data(response, name: str, data: dict):
    """Set signed cookie data."""
    signed = serializer.dumps(data)
    response.set_cookie(
        name, signed,
        httponly=True,
        secure=IS_PRODUCTION,
        samesite="lax",
        max_age=COOKIE_MAX_AGE
    )


def get_spotify_session(request: Request) -> Optional[dict]:
    """Get Spotify session from cookie."""
    return get_cookie_data(request, "spotify_session")


def get_tidal_session(request: Request) -> Optional[tidalapi.Session]:
    """Reconstruct Tidal session from cookie."""
    data = get_cookie_data(request, "tidal_session")
    if not data:
        return None

    try:
        tidal = tidalapi.Session()
        tidal.load_oauth_session(
            token_type=data["token_type"],
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expiry_time=data.get("expiry_time"),
        )
        if tidal.check_login():
            return tidal
    except Exception:
        pass
    return None


def save_tidal_session(response, tidal: tidalapi.Session):
    """Save Tidal session tokens to cookie."""
    # expiry_time might be a datetime or already a string
    expiry = tidal.expiry_time
    if expiry and hasattr(expiry, 'isoformat'):
        expiry = expiry.isoformat()

    data = {
        "token_type": tidal.token_type,
        "access_token": tidal.access_token,
        "refresh_token": tidal.refresh_token,
        "expiry_time": expiry,
        "user_id": str(tidal.user.id) if tidal.user else None,
    }
    set_cookie_data(response, "tidal_session", data)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    spotify = get_spotify_session(request)
    tidal = get_tidal_session(request)

    tidal_user = None
    if tidal and tidal.user:
        tidal_user = tidal.user.id

    return templates.TemplateResponse("index.html", {
        "request": request,
        "spotify_connected": spotify is not None,
        "tidal_connected": tidal is not None,
        "spotify_user": spotify.get("user") if spotify else None,
        "tidal_user": tidal_user,
    })


# --- Spotify OAuth ---

@app.get("/auth/spotify")
async def spotify_auth(request: Request):
    """Redirect to Spotify authorization."""
    state = secrets.token_urlsafe(16)
    scope = "user-library-read playlist-read-private playlist-read-collaborative user-follow-read"

    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": f"{BASE_URL}/auth/spotify/callback",
        "scope": scope,
        "state": state,
    }
    url = "https://accounts.spotify.com/authorize?" + "&".join(f"{k}={v}" for k, v in params.items())

    response = RedirectResponse(url)
    response.set_cookie("spotify_state", state, httponly=True, max_age=600)
    return response


@app.get("/auth/spotify/callback")
async def spotify_callback(request: Request, code: Optional[str] = None, error: Optional[str] = None):
    """Handle Spotify OAuth callback."""
    if error:
        return RedirectResponse(f"/?error={error}")

    if not code:
        return RedirectResponse("/?error=no_code")

    # Exchange code for token
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{BASE_URL}/auth/spotify/callback",
            },
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        )

        if resp.status_code != 200:
            return RedirectResponse(f"/?error=token_exchange_failed")

        token_data = resp.json()

        # Get user info
        user_response = await client.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {token_data['access_token']}"}
        )
        user_data = user_response.json()

    # Save to cookie
    session_data = {
        "token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token"),
        "user": user_data.get("display_name") or user_data.get("id"),
    }

    response = RedirectResponse("/")
    set_cookie_data(response, "spotify_session", session_data)
    return response


# --- Tidal OAuth ---

@app.get("/auth/tidal")
async def tidal_auth(request: Request):
    """Start Tidal device auth flow."""
    # Generate a unique ID for this auth attempt
    auth_id = secrets.token_urlsafe(16)

    tidal = tidalapi.Session()
    login, future = tidal.login_oauth()

    verification_uri = login.verification_uri_complete
    if not verification_uri.startswith("http"):
        verification_uri = f"https://{verification_uri}"

    # Store in memory (needed for the future object)
    pending_tidal_auth[auth_id] = {
        "session": tidal,
        "future": future,
        "verification_uri": verification_uri,
    }

    response = RedirectResponse("/auth/tidal/device")
    response.set_cookie("tidal_auth_id", auth_id, httponly=True, max_age=600)
    return response


@app.get("/auth/tidal/device", response_class=HTMLResponse)
async def tidal_device(request: Request):
    """Show Tidal device authorization page."""
    auth_id = request.cookies.get("tidal_auth_id")
    pending = pending_tidal_auth.get(auth_id) if auth_id else None

    if not pending:
        # Auth state lost (server restart or different machine) - restart auth
        return RedirectResponse("/auth/tidal")

    return templates.TemplateResponse("tidal_device.html", {
        "request": request,
        "verification_uri": pending["verification_uri"],
    })


@app.get("/auth/tidal/check")
async def tidal_check(request: Request):
    """Check if Tidal auth completed."""
    auth_id = request.cookies.get("tidal_auth_id")
    pending = pending_tidal_auth.get(auth_id) if auth_id else None

    if not pending:
        # Auth state lost - tell frontend to restart
        return {"status": "restart", "message": "Auth session expired, restarting..."}

    future = pending["future"]
    tidal = pending["session"]

    if future.done():
        try:
            future.result()
            if tidal.check_login():
                # Clean up pending auth
                del pending_tidal_auth[auth_id]

                # Return success with tokens to save client-side
                return {
                    "status": "success",
                    "tokens": {
                        "token_type": tidal.token_type,
                        "access_token": tidal.access_token,
                        "refresh_token": tidal.refresh_token,
                        "expiry_time": tidal.expiry_time.isoformat() if tidal.expiry_time else None,
                        "user_id": str(tidal.user.id) if tidal.user else None,
                    }
                }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return {"status": "pending"}


@app.post("/auth/tidal/save")
async def tidal_save(request: Request):
    """Save Tidal tokens to cookie."""
    import json

    # Accept both JSON and form data
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        tokens = data.get("tokens")
    else:
        form = await request.form()
        tokens_str = form.get("tokens")
        if tokens_str:
            tokens = json.loads(tokens_str)
        else:
            tokens = None

    if not tokens:
        raise HTTPException(400, "No tokens provided")

    # Verify tokens work by creating a session
    try:
        tidal = tidalapi.Session()
        tidal.load_oauth_session(
            token_type=tokens["token_type"],
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            expiry_time=tokens.get("expiry_time"),
        )
        if not tidal.check_login():
            raise HTTPException(400, "Invalid tokens")
    except Exception as e:
        raise HTTPException(400, f"Token validation failed: {e}")

    response = RedirectResponse("/", status_code=303)
    save_tidal_session(response, tidal)
    response.delete_cookie("tidal_auth_id")
    return response


@app.get("/sync/stream")
async def sync_stream(
    request: Request,
    playlists: bool = False,
    favorites: bool = False,
    albums: bool = False,
    artists: bool = False,
):
    """Stream sync progress via Server-Sent Events."""
    spotify = get_spotify_session(request)
    tidal = get_tidal_session(request)

    if not spotify:
        raise HTTPException(400, "Spotify not connected")
    if not tidal:
        raise HTTPException(400, "Tidal not connected")

    async def generate():
        try:
            async for event in run_sync_streaming(
                spotify_token=spotify["token"],
                tidal_session=tidal,
                sync_playlists=playlists,
                do_sync_albums=albums,
                do_sync_artists=artists,
                do_sync_favorites=favorites,
                spotify_refresh_token=spotify.get("refresh_token"),
            ):
                yield event
        except Exception as e:
            import traceback
            print(f"SSE stream error: {e}")
            traceback.print_exc()
            yield {"event": "message", "data": json.dumps({"type": "error", "task": "sync", "error": str(e)})}

    return EventSourceResponse(
        generate(),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        }
    )


@app.post("/sync")
async def sync(request: Request):
    """Run the sync process."""
    spotify = get_spotify_session(request)
    tidal = get_tidal_session(request)

    if not spotify:
        raise HTTPException(400, "Spotify not connected")
    if not tidal:
        raise HTTPException(400, "Tidal not connected")

    form = await request.form()
    sync_playlists = form.get("playlists") == "on"
    sync_albums = form.get("albums") == "on"
    sync_artists = form.get("artists") == "on"
    sync_favorites = form.get("favorites") == "on"

    result = await run_sync(
        spotify_token=spotify["token"],
        tidal_session=tidal,
        sync_playlists=sync_playlists,
        do_sync_albums=sync_albums,
        do_sync_artists=sync_artists,
        do_sync_favorites=sync_favorites,
        spotify_refresh_token=spotify.get("refresh_token"),
    )

    return templates.TemplateResponse("result.html", {
        "request": request,
        "result": result,
    })


@app.get("/logout")
async def logout(request: Request):
    """Clear session cookies."""
    response = RedirectResponse("/")
    response.delete_cookie("spotify_session")
    response.delete_cookie("tidal_session")
    response.delete_cookie("tidal_auth_id")
    return response


@app.get("/ping")
async def ping():
    """Simple ping endpoint to keep Fly.io machine active during long operations."""
    return {"status": "ok"}


# Keep sessions dict for test compatibility
sessions: dict[str, dict] = {}

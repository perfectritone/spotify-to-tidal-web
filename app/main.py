import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer

from .sync import run_sync

# Config from environment (host sets these once)
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")

serializer = URLSafeTimedSerializer(SECRET_KEY)

# In-memory session store (use Redis in production)
sessions: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown
    sessions.clear()


app = FastAPI(title="Spotify to Tidal Sync", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def get_session(request: Request) -> dict:
    """Get or create session for request."""
    session_id = request.cookies.get("session_id")
    if session_id and session_id in sessions:
        return sessions[session_id]
    return {}


def save_session(request: Request, response, session_data: dict):
    """Save session data, creating new session if needed."""
    session_id = request.cookies.get("session_id")
    if session_id and session_id in sessions:
        sessions[session_id] = session_data
    else:
        session_id = secrets.token_urlsafe(32)
        sessions[session_id] = session_data
        response.set_cookie(
            "session_id", session_id,
            httponly=True, secure=False, samesite="lax", max_age=3600
        )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    session = get_session(request)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "spotify_connected": "spotify_token" in session,
        "tidal_connected": "tidal_session" in session,
        "spotify_user": session.get("spotify_user"),
        "tidal_user": session.get("tidal_user"),
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
        response = await client.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{BASE_URL}/auth/spotify/callback",
            },
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        )

        if response.status_code != 200:
            return RedirectResponse(f"/?error=token_exchange_failed")

        token_data = response.json()

        # Get user info
        user_response = await client.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {token_data['access_token']}"}
        )
        user_data = user_response.json()

    # Update session
    session = get_session(request)
    session["spotify_token"] = token_data["access_token"]
    session["spotify_refresh"] = token_data.get("refresh_token")
    session["spotify_user"] = user_data.get("display_name") or user_data.get("id")

    response = RedirectResponse("/")
    save_session(request, response, session)
    return response


# --- Tidal OAuth ---

@app.get("/auth/tidal")
async def tidal_auth(request: Request):
    """Start Tidal device auth flow."""
    import tidalapi

    session = get_session(request)
    tidal = tidalapi.Session()
    login, future = tidal.login_oauth()

    # Store the session object for later
    verification_uri = login.verification_uri_complete
    if not verification_uri.startswith("http"):
        verification_uri = f"https://{verification_uri}"

    session["tidal_pending"] = {
        "session": tidal,
        "future": future,
        "verification_uri": verification_uri,
    }

    response = RedirectResponse("/auth/tidal/device")
    save_session(request, response, session)
    return response


@app.get("/auth/tidal/device", response_class=HTMLResponse)
async def tidal_device(request: Request):
    """Show Tidal device authorization page."""
    session = get_session(request)
    pending = session.get("tidal_pending")

    if not pending:
        return RedirectResponse("/")

    return templates.TemplateResponse("tidal_device.html", {
        "request": request,
        "verification_uri": pending["verification_uri"],
    })


@app.get("/auth/tidal/check")
async def tidal_check(request: Request):
    """Check if Tidal auth completed."""
    session = get_session(request)
    pending = session.get("tidal_pending")

    if not pending:
        return {"status": "error", "message": "No pending auth"}

    future = pending["future"]
    tidal = pending["session"]

    if future.done():
        try:
            future.result()
            if tidal.check_login():
                session["tidal_session"] = tidal
                session["tidal_user"] = tidal.user.id
                del session["tidal_pending"]
                return {"status": "success"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return {"status": "pending"}


@app.post("/sync")
async def sync(request: Request):
    """Run the sync process."""
    session = get_session(request)

    if "spotify_token" not in session:
        raise HTTPException(400, "Spotify not connected")
    if "tidal_session" not in session:
        raise HTTPException(400, "Tidal not connected")

    form = await request.form()
    sync_playlists = form.get("playlists") == "on"
    sync_albums = form.get("albums") == "on"
    sync_artists = form.get("artists") == "on"
    sync_favorites = form.get("favorites") == "on"

    result = await run_sync(
        spotify_token=session["spotify_token"],
        tidal_session=session["tidal_session"],
        sync_playlists=sync_playlists,
        sync_albums=sync_albums,
        sync_artists=sync_artists,
        sync_favorites=sync_favorites,
    )

    return templates.TemplateResponse("result.html", {
        "request": request,
        "result": result,
    })


@app.get("/logout")
async def logout(request: Request):
    """Clear session."""
    session_id = request.cookies.get("session_id")
    if session_id and session_id in sessions:
        del sessions[session_id]

    response = RedirectResponse("/")
    response.delete_cookie("session_id")
    return response

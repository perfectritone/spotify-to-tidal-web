# Spotify to Tidal Web

A web app for syncing Spotify playlists, albums, and artists to Tidal.

## Setup

### 1. Create Spotify App

1. Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Create a new app
3. Add redirect URI: `http://localhost:8000/auth/spotify/callback` (and your production URL)
4. Copy the Client ID and Client Secret

### 2. Local Development

```bash
# Install dependencies
uv sync

# Copy environment template
cp .env.example .env

# Edit .env with your Spotify credentials
# SPOTIFY_CLIENT_ID=xxx
# SPOTIFY_CLIENT_SECRET=xxx

# Run the app
uv run uvicorn app.main:app --reload
```

Visit http://localhost:8000

### 3. Deploy to Fly.io

```bash
# Install flyctl if needed
curl -L https://fly.io/install.sh | sh

# Launch (first time)
fly launch --no-deploy

# Set secrets
fly secrets set \
  SPOTIFY_CLIENT_ID=your_client_id \
  SPOTIFY_CLIENT_SECRET=your_client_secret \
  SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))") \
  BASE_URL=https://your-app.fly.dev

# Deploy
fly deploy

# Add production redirect URI to Spotify app:
# https://your-app.fly.dev/auth/spotify/callback
```

## Usage

1. Click "Connect Spotify" and authorize
2. Click "Connect Tidal" and authorize in the popup
3. Select what to sync (playlists, albums, artists, favorites)
4. Click "Start Sync"

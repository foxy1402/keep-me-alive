# Keep Me Alive 💓

A web service that keeps your free-tier hosted apps alive by visiting them with a real browser.

## Features

- 🔐 **Admin Authentication** - Password-protected admin panel
- 💾 **Gist Storage** - Persistent data via GitHub Gist
- 🌐 **Real Browser** - Playwright Chromium visits (waits 20-30s for content)
- ⏱️ **Randomized Intervals** - 10-14 min (configurable)
- 📸 **Screenshots** - Optional inline display after visit

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ADMIN_PASSWORD` | ✅ | Password to access admin panel |
| `GIST_TOKEN` | ✅ | GitHub Personal Access Token (gist scope) |
| `GIST_ID` | ✅ | ID of the Gist for data storage |
| `PORT` | ⬜ | Port the app binds to (default `8501`). Most PaaS platforms inject this automatically. |

## Setup Guide

### Step 1: Create GitHub Gist

1. Go to https://gist.github.com
2. **Description**: `keep-me-alive`
3. **Filename**: `keepmealive_data.json`
4. **Content** - paste this:
```json
{
  "websites": [],
  "settings": {
    "interval_min": 10,
    "interval_max": 14,
    "screenshots_enabled": false
  },
  "visit_history": []
}
```
5. Click **Create secret gist**
6. Copy the Gist ID from URL (e.g., `gist.github.com/user/abc123` → ID is `abc123`)

### Step 2: Create GitHub Token

1. Go to GitHub → Settings → Developer Settings → Personal Access Tokens → Tokens (classic)
2. Click **Generate new token (classic)**
3. Name: `keep-me-alive`
4. Select scope: **gist**
5. Click **Generate token**
6. Copy the token (starts with `ghp_`)

### Step 3: Deploy

#### Streamlit Cloud

1. Push to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Connect repo → Deploy
4. Go to **Settings → Secrets** and add:
```toml
ADMIN_PASSWORD = "your-password"
GIST_TOKEN = "ghp_xxxx"
GIST_ID = "your-gist-id"
```

#### Render.com

1. Push to GitHub
2. Create Web Service → Docker runtime
3. Add environment variables in Settings
4. Deploy

#### Prebuilt image (GHCR) — Portainer / any PaaS

A multi-arch image (`linux/amd64` + `linux/arm64`) is published to GHCR on every push to `main`:

```
ghcr.io/foxy1402/keep-me-alive:latest
```

Run it anywhere Docker/OCI images are supported:

```bash
docker run -d \
  -p 8501:8501 \
  -e ADMIN_PASSWORD="your-password" \
  -e GIST_TOKEN="ghp_xxxx" \
  -e GIST_ID="your-gist-id" \
  ghcr.io/foxy1402/keep-me-alive:latest
```

- **PaaS (Railway, Fly.io, Render, etc.):** the platform injects its own `PORT`; the container binds to it automatically. Just set the three env vars above.
- **Portainer:** create a container/stack from the image above, map the published port to `8501` (or set `PORT` to match your mapping), and add the env vars.

## Local Development

```bash
# Set environment variables (Windows)
set ADMIN_PASSWORD=test
set GIST_TOKEN=ghp_xxxx
set GIST_ID=your-gist-id

# Install and run
pip install -r requirements.txt
playwright install chromium
streamlit run app.py
```

## Pro Tip 🤓

Add this app's own URL to keep itself alive!

## License

MIT

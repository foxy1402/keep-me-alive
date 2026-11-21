# Keep Me Alive 💓

A small service that keeps free-tier hosted apps awake by visiting them with a real browser on a
randomized schedule.

Runs as a single container: FastAPI + server-rendered HTML (no frontend build, no Streamlit), with
Playwright driving Chromium.

## Features

- 🌐 **Real browser** — Playwright Chromium, launched so it does not advertise itself as headless
- 🛡️ **Honest results** — a visit counts as successful only when the *origin* answered; an edge/CDN
  challenge is recorded as `blocked` instead of silently looking healthy
- 🍪 **Session reuse** — a browser profile per host keeps challenge cookies between visits
- 🕵️ **Optional Firefox** — a second engine whose fingerprint is honest with no rewriting at all
- ⏱️ **Randomized intervals** — 10–14 min by default, configurable
- 📸 **Screenshots** — optional, served as PNGs and pruned automatically
- 🔐 **Password login** — signed cookie session, no user database; failed attempts are throttled per
  client
- 💾 **Two-tier storage** — sites and settings sync through GitHub Gist (optional); visit history
  stays local and is pruned to 3 days / 100 entries, so logging never spams Gist revisions

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ADMIN_PASSWORD` | ⬜ | Login password. If unset, the app is open (fine for local use only). |
| `GIST_TOKEN` | ⬜ | GitHub Personal Access Token with `gist` scope |
| `GIST_ID` | ⬜ | Gist ID used for storage. Without Gist vars, data lives in `data/` |
| `PORT` | ⬜ | Port to bind (default `8080`) |
| `SESSION_SECRET` | ⬜ | Cookie signing key. Defaults to a hash of `ADMIN_PASSWORD`, so changing the password logs everyone out. |
| `COOKIE_SECURE` | ⬜ | Set to `true` when serving over HTTPS so the session cookie is `Secure` |
| `BROWSER_ENGINES` | ⬜ | Build arg, not runtime: which engines the image installs (default `chromium`) |

## Deploy on Portainer

Add a stack with the image published to GHCR:

```yaml
services:
  keep-me-alive:
    image: ghcr.io/foxy1402/keep-me-alive:latest
    container_name: keep-me-alive
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      ADMIN_PASSWORD: "change-me"
      GIST_TOKEN: "ghp_xxxx"
      GIST_ID: "your-gist-id"
    volumes:
      - ./data:/app/data
    stop_grace_period: 30s
```

`docker run` equivalent:

```bash
docker run -d --name keep-me-alive -p 8080:8080 \
  -e ADMIN_PASSWORD="change-me" -e GIST_TOKEN="ghp_xxxx" -e GIST_ID="your-gist-id" \
  -v "$PWD/data:/app/data" \
  ghcr.io/foxy1402/keep-me-alive:latest
```

Notes:

- The health check is `GET /healthz`. Run **one** instance/worker: the scheduler lives inside the
  process.
- The app runs as an unprivileged user (`appuser`, uid 10001). The entrypoint starts as root only to
  make a bind-mounted `./data` writable, then drops privileges — so browser profiles and screenshots
  are never written by root.
- `stop_grace_period: 30s` matches the server's graceful-shutdown window; a browser visit in flight is
  allowed to finish instead of being killed at the default 10 s.
- Cookies are `HttpOnly` + `SameSite=Lax`. Set `COOKIE_SECURE=true` when the app is served over HTTPS.
- Failed logins are throttled per client address (10-minute window). A throttled client that supplies
  the correct password still gets in, so the admin cannot be locked out.

## Build it yourself

```bash
# Chromium only (default, smallest)
docker build -t keep-me-alive .

# Chromium + Firefox, to use the engine toggle
docker build --build-arg BROWSER_ENGINES="chromium firefox" -t keep-me-alive .
```

The default image is roughly 440 MB compressed (1.6 GB on disk) — the bulk is Chromium and its
system libraries. The stripped `headless_shell` build is deliberately removed: it reports a
`HeadlessChrome` user agent, which is the one thing this app exists to avoid.

`.github/workflows/docker-publish.yml` builds the image, **starts it**, waits for `/healthz`, checks
that Chromium actually launches, and only then publishes a multi-arch image (`linux/amd64` +
`linux/arm64`) to GHCR. Only pushes to `main` move the `latest` tag. The published image ships
Chromium only; build with `--build-arg BROWSER_ENGINES="chromium firefox"` yourself if you want the
Firefox toggle.

## When a keep-alive is "ignored"

If a free-tier app still sleeps while your monitor reports it up, the request is usually being
answered by a **CDN in front of the app**, not by the app. Cloudflare (and similar) serve a challenge
page from the edge; that response never reaches your origin, so the sleep timer never resets. Plain
HTTP monitors like Uptime Kuma hit this constantly.

What this app does about it:

- **Detects the challenge** and records the visit as `blocked` instead of successful, so a silent
  no-op is visible in the history.
- **Strips headless tells.** Playwright's headless Chromium advertises `HeadlessChrome` and sets
  `navigator.webdriver`; both are removed. The user agent is derived from the running build, which
  keeps it consistent with the `sec-ch-ua` client hints the browser sends — a hand-written UA
  string does not.
- **Keeps a profile per host** so a passed challenge (`cf_clearance`) is reused on the next visit
  rather than re-solved every time.
- **Offers Firefox** as a second engine, which needs no user-agent rewriting at all.

The most reliable fix, though, is to not go through the CDN at your origin: point the keep-alive at
the platform hostname (`yourapp.onrender.com`) instead of a custom domain behind Cloudflare.

## Setup guide (Gist storage)

Optional — without it, sites and settings are kept in `data/`.

The Gist holds **sites and settings only**. Visit history is a local log: it changes every few
minutes, so syncing it would create one Gist revision per visit. It lives in `data/websites.json` and
is pruned to the last 3 days (max 100 entries). Older Gists that still contain a `visit_history` are
migrated automatically on first load — the stored history is cleared once and history stays local
from then on.

### 1. Create a GitHub Gist

1. Go to https://gist.github.com
2. **Description**: `keep-me-alive`
3. **Filename**: `keepmealive_data.json`
4. **Content**:
```json
{
  "websites": [],
  "settings": {
    "interval_min": 10,
    "interval_max": 14,
    "screenshots_enabled": false,
    "browser_engine": "chromium",
    "post_load_wait_seconds": 5,
    "browser_timezone": "UTC"
  },
  "visit_history": []
}
```
5. **Create secret gist**, then copy the ID from the URL (`gist.github.com/user/abc123` → `abc123`)

Existing Gists keep working: missing settings keys are filled in from the defaults, and older visit
records render fine without the newer diagnostic fields.

### 2. Create a token

GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → **Generate new
token**, scope `gist`, then copy it (starts with `ghp_`).

## Local development

```bash
pip install -r requirements.txt
playwright install --no-shell chromium

set ADMIN_PASSWORD=test      # Windows; use export on Linux/macOS
uvicorn app:app --reload --port 8080
```

Then open http://127.0.0.1:8080.

## Pro tip 🤓

Add this app's own URL to keep itself alive.

## License

MIT

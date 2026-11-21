# Keep Me Alive - container image for Portainer / any Docker host.
#
# Slim by design: no Streamlit (which drags in pandas/pyarrow), and only the
# full Chromium build. It must be the full build, not the stripped headless
# shell, so the browser does not advertise itself as headless.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    # Keep browsers out of /root so a non-root user can run them.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Python dependencies first, for layer caching. --no-compile skips .pyc files.
COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt

# The Playwright CLI is a Node binary that needs libatomic to run, and
# `install-deps` (which would provide it) is itself a Playwright command.
# Install that one library first so the CLI can start on a slim image.
# gosu is used by the entrypoint to drop privileges after fixing data ownership.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 gosu \
    && rm -rf /var/lib/apt/lists/*

# System libraries for the browser, as root.
ARG BROWSER_ENGINES=chromium
RUN playwright install-deps ${BROWSER_ENGINES} \
    && rm -rf /var/lib/apt/lists/*

# Create the runtime user before downloading the browser, and download it as
# that user. Doing it after, with `chown -R /ms-playwright`, would copy every
# Chromium file into a second layer (~440 MB) for no benefit.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /ms-playwright \
    && chown appuser:appuser /app/data /ms-playwright

# --no-shell skips the stripped "headless shell" build (~110 MB compressed).
# This app never uses it: it always launches full Chromium via
# channel="chromium", because the shell reports a HeadlessChrome user agent and
# no plugins, which is exactly what we are trying to avoid.
#
# To also ship Firefox (for the engine toggle), build with:
#   --build-arg BROWSER_ENGINES="chromium firefox"
RUN gosu appuser playwright install --no-shell ${BROWSER_ENGINES}

COPY app.py browser_worker.py scheduler.py storage.py gist_storage.py ./
COPY templates ./templates
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=3)" || exit 1

# The entrypoint starts as root only to make a bind-mounted ./data writable,
# then drops to appuser. Chromium here is unsandboxed (needs --no-sandbox in a
# container), so it should not also be root.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-graceful-shutdown 30"]

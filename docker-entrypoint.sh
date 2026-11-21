#!/bin/sh
# Run the app as a non-root user, while still supporting a bind-mounted data
# directory that Docker created as root.
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/data
    # Best effort: if the volume is read-only or owned by someone else, the app
    # still starts and reports the problem rather than refusing to boot.
    chown -R appuser:appuser /app/data 2>/dev/null || true
    exec gosu appuser "$@"
fi

exec "$@"

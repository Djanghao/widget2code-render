#!/usr/bin/env bash
# Run the containerized render daemon (self-check first, then serve).
#
#   docker/run.sh [workers]
#   W2C_RENDER_ALLOW_REACT_ICONS=1 docker/run.sh [workers]
#
# The only mount is /tmp/w2c-render, the socket and heartbeat: source and
# screenshot both travel over the socket, so the daemon needs no access to the
# caller's files.
set -euo pipefail

WORKERS="${1:-8}"
NAME=w2c-render
IMAGE="${W2C_RENDER_IMAGE:-w2c-render:latest}"

# A machine that has never built anything can still serve: name a published
# image and it is pulled once. Pixels come from the image, not the host.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "pulling $IMAGE ..."
  docker pull "$IMAGE"
fi

mkdir -p /tmp/w2c-render
docker rm -f "$NAME" 2>/dev/null || true
# A stale socket from a previous container would fool the readiness wait
# below (and may be owned by another uid); the daemon binds a fresh one.
rm -f /tmp/w2c-render/render.sock /tmp/w2c-render/heartbeat.json
# --user: run as the invoking host user, not root — the socket has to be
# connectable by the caller. HOME=/tmp gives Chromium/fontconfig a writable
# home for their caches.
docker run -d --name "$NAME" \
  --restart unless-stopped \
  --init \
  --shm-size=2g \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e W2C_RENDER_WORKERS="$WORKERS" \
  -e W2C_RENDER_ALLOW_REACT_ICONS="${W2C_RENDER_ALLOW_REACT_ICONS:-0}" \
  -v /tmp/w2c-render:/tmp/w2c-render \
  "$IMAGE"

echo "waiting for self-check + daemon..."
for _ in $(seq 1 600); do
  if [ -S /tmp/w2c-render/render.sock ]; then
    echo "render daemon is up: /tmp/w2c-render/render.sock"
    exit 0
  fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != "true" ]; then
    echo "container exited — self-check failed or crash:" >&2
    docker logs "$NAME" | tail -20 >&2
    exit 1
  fi
  sleep 1
done
echo "daemon did not come up within 600s; docker logs $NAME" >&2
exit 1

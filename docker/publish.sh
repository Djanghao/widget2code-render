#!/usr/bin/env bash
# Publish the frozen render image to a registry, so other machines and other
# projects pull the pixels instead of rebuilding them.
#
#   docker login                                   # once, interactively
#   docker/publish.sh <namespace>           # e.g. yourname
#   W2C_DOCKERHUB_NAMESPACE=yourname docker/publish.sh
#
# What is published is the environment itself — Chromium, the reference
# machine's fonts, node_modules, and the golden checksums — which is why a
# pull reproduces this machine's renders byte for byte and a rebuild elsewhere
# would not. Tags: the commit the image was built from, and `latest`.
set -euo pipefail
cd "$(dirname "$0")/.."

NAMESPACE="${1:-${W2C_DOCKERHUB_NAMESPACE:-}}"
if [ -z "$NAMESPACE" ]; then
  echo "usage: docker/publish.sh <dockerhub-namespace>" >&2
  exit 1
fi
if [ ! -f "${HOME}/.docker/config.json" ]; then
  echo "not logged in — run 'docker login' first" >&2
  exit 1
fi
if ! docker image inspect w2c-render:latest >/dev/null 2>&1; then
  echo "w2c-render:latest not built — run docker/build.sh first" >&2
  exit 1
fi

SHA=$(git rev-parse --short HEAD 2>/dev/null || echo dev)
REMOTE="${NAMESPACE}/w2c-render"

# The self-check hashes only mean something for the image they were recorded
# in; publishing one whose canaries do not reproduce would ship a baseline
# nobody can meet.
echo "verifying the image reproduces its own golden set..."
docker run --rm --shm-size=2g w2c-render:latest python docker/selfcheck.py

docker tag w2c-render:latest "${REMOTE}:${SHA}"
docker tag w2c-render:latest "${REMOTE}:latest"
echo "pushing ${REMOTE}:${SHA} (~$(docker image inspect -f '{{.Size}}' w2c-render:latest | awk '{printf "%.1fGB", $1/1e9}'))..."
docker push "${REMOTE}:${SHA}"
docker push "${REMOTE}:latest"

# The overview a registry shows is not part of the image, so pushing one
# without it leaves an unexplained 4 GB download on the page.
if [ -n "${DOCKERHUB_TOKEN:-}" ]; then
  echo "updating the Docker Hub description..."
  JWT=$(curl -s -H "Content-Type: application/json" \
    -d "{\"username\":\"${NAMESPACE}\",\"password\":\"${DOCKERHUB_TOKEN}\"}" \
    https://hub.docker.com/v2/users/login/ | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))')
  if [ -n "$JWT" ]; then
    python3 - "$JWT" "$REMOTE" <<'PY'
import json, sys, urllib.request
jwt, remote = sys.argv[1], sys.argv[2]
body = json.dumps({
    "full_description": open("README.md").read(),
    "description": "Deterministic React-widget renderer (JSX to PNG) over a Unix socket.",
}).encode()
request = urllib.request.Request(
    f"https://hub.docker.com/v2/repositories/{remote}/", data=body, method="PATCH",
    headers={"Content-Type": "application/json", "Authorization": f"JWT {jwt}"})
with urllib.request.urlopen(request) as response:
    print("  description updated:", response.status)
PY
  else
    echo "  could not authenticate to the Hub API; paste DOCKERHUB.md into the web UI" >&2
  fi
else
  echo "set DOCKERHUB_TOKEN to also publish README.md as the Docker Hub overview"
fi

echo
echo "published. On any other machine:"
echo "    W2C_RENDER_IMAGE=${REMOTE}:${SHA} docker/run.sh 8"
echo "Pin the digest when it has to be provably the same image:"
docker image inspect -f '{{index .RepoDigests 0}}' "${REMOTE}:${SHA}" 2>/dev/null || true

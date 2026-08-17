#!/usr/bin/env bash
# Build the frozen render image. Build ONCE on one machine, then distribute
# the image itself (docker push, or docker save | docker load) — rebuilding
# per machine would re-resolve layers and reintroduce drift.
set -euo pipefail
cd "$(dirname "$0")/.."

SHA=$(git rev-parse --short HEAD 2>/dev/null || echo dev)
STAMP="${SHA}-$(date +%Y%m%d%H%M%S)"
TAG="w2c-render:${SHA}"

# Text rasterization is decided by the installed fonts, and the base image
# carries far fewer than a normal workstation — enough to move 0.35% of the
# pixels of a text-heavy widget. Baking THIS machine's font set (and its
# fontconfig rules) into the image makes every container reproduce this
# machine, everywhere. Set W2C_SKIP_HOST_FONTS=1 to keep the base image's
# fonts instead; then regenerate the golden checksums, because they describe
# whichever baseline the image actually has.
FONTS_TAR=docker/hostfonts.tar
rm -f "$FONTS_TAR"
if [ -z "${W2C_SKIP_HOST_FONTS:-}" ]; then
  echo "staging host fonts + fontconfig into $FONTS_TAR ..."
  tar -cf "$FONTS_TAR" -C / \
      usr/share/fonts usr/share/texmf/fonts etc/fonts 2>/dev/null || true
  echo "  $(du -h "$FONTS_TAR" | cut -f1), sha256 $(sha256sum "$FONTS_TAR" | cut -c1-16)…"
fi

docker build -f docker/Dockerfile --build-arg BUILD_STAMP="$STAMP" \
    -t "$TAG" -t w2c-render:latest .
echo
echo "built $TAG"
docker images --digests w2c-render | head -3
echo
echo "distribute with:  docker save w2c-render:latest | ssh <host> docker load"

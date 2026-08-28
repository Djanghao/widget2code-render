#!/usr/bin/env bash
# Build the frozen render image. Build ONCE on one machine, then distribute
# the image itself (docker push, or docker save | docker load) — rebuilding
# per machine would re-resolve layers and reintroduce drift.
set -euo pipefail
cd "$(dirname "$0")/.."

SHA=$(git rev-parse --short HEAD 2>/dev/null || echo dev)
STAMP="${SHA}-$(date +%Y%m%d%H%M%S)"
TAG="w2c-render:${SHA}"
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)

# Text rasterization is decided by the installed fonts, and the base image
# carries far fewer than a normal workstation — enough to move 0.35% of the
# pixels of a text-heavy widget. Baking THIS machine's font set (and its
# fontconfig rules) into the image makes every container reproduce this
# machine, everywhere. Set W2C_SKIP_HOST_FONTS=1 to keep the base image's
# fonts instead; then regenerate the golden checksums, because they describe
# whichever baseline the image actually has.
FONT_PATHS="usr/share/fonts usr/share/texmf/fonts etc/fonts"
FONTS_TAR=docker/hostfonts.tar
rm -f "$FONTS_TAR"
if [ -n "${W2C_RENDER_FONTS_FROM:-}" ]; then
  # The baseline belongs to the image, not to whichever host rebuilds it. A
  # machine's font set changes under it — this one went from 940 faces to 272
  # between two builds, and rebuilding from it silently produced an image whose
  # five canaries all mismatched and which refused to serve. Carrying the fonts
  # over from the image being replaced keeps the pixels of every existing score
  # comparable; the golden checksums then still describe what the image has.
  echo "carrying fonts + fontconfig over from $W2C_RENDER_FONTS_FROM ..."
  docker run --rm --entrypoint tar "$W2C_RENDER_FONTS_FROM" \
      -cf - -C / $FONT_PATHS > "$FONTS_TAR" 2>/dev/null || true
  echo "  $(du -h "$FONTS_TAR" | cut -f1), sha256 $(sha256sum "$FONTS_TAR" | cut -c1-16)…"
elif [ -z "${W2C_SKIP_HOST_FONTS:-}" ]; then
  echo "staging host fonts + fontconfig into $FONTS_TAR ..."
  tar -cf "$FONTS_TAR" -C / $FONT_PATHS 2>/dev/null || true
  echo "  $(du -h "$FONTS_TAR" | cut -f1), sha256 $(sha256sum "$FONTS_TAR" | cut -c1-16)…"
fi

docker build -f docker/Dockerfile --build-arg BUILD_STAMP="$STAMP" \
    -t "$TAG" -t "w2c-render:${VERSION}" -t w2c-render:latest .
echo
echo "built $TAG"
docker images --digests w2c-render | head -3
echo
echo "distribute with:  docker save w2c-render:latest | ssh <host> docker load"

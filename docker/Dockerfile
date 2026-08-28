# Frozen rendering environment: the pixels are defined by this image.
#
# Everything rasterization depends on is baked in and pinned — Chromium (which
# bundles its own skia/freetype/harfbuzz), the fonts, node, and the renderer's
# node_modules (the echarts/recharts versions decide chart pixels). The host
# contributes CPU and nothing else, which is why the same image renders
# byte-identically on every amd64 machine, and why docker/selfcheck.py proves
# that per host instead of assuming it.
#
# Build ONCE and distribute the image (docker push / save). A rebuild
# elsewhere re-resolves base layers and picks up that machine's fonts, which
# is a different baseline — see README.md.
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

# Read by registries and by anyone inspecting the image for what it is.
LABEL org.opencontainers.image.title="w2c-render" \
      org.opencontainers.image.description="Deterministic React-widget renderer (JSX to PNG) served over a Unix socket, with a frozen rasterization stack and a per-host golden self-check." \
      org.opencontainers.image.source="https://github.com/Djanghao/widget2code-render" \
      org.opencontainers.image.documentation="https://github.com/Djanghao/widget2code-render#readme" \
      org.opencontainers.image.licenses="MIT" \
      io.claude.skill="https://github.com/Djanghao/widget2code-render/blob/main/.claude/skills/render-widget/SKILL.md"

# Node for the Vite dev server and for installing its dependencies.
COPY --from=node:20-bullseye-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:20-bullseye-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

# The base image ships the browsers (/ms-playwright) but not the pip package
# for its system python. Pinned to the version whose browsers the base image
# carries. `docker/wheels/` is optional: drop wheels there to build without
# network access, which is why the copy tolerates the directory being empty.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY docker/wheels* /tmp/wheels/
RUN if ls /tmp/wheels/*.whl >/dev/null 2>&1; then \
        python3 -m pip install --no-cache-dir --no-index --find-links /tmp/wheels \
            playwright==1.62.0; \
    else \
        python3 -m pip install --no-cache-dir playwright==1.62.0; \
    fi && rm -rf /tmp/wheels

# Fonts are the one part of the raster stack the base image does not pin the
# way a deterministic renderer needs: its own 50 fonts put 0.35% of the pixels
# of a text-heavy widget somewhere else than a machine with a full font set
# does. build.sh stages the building machine's fonts and fontconfig rules into
# hostfonts.tar so the image reproduces *that* machine everywhere. The tar is
# optional: without it the image simply keeps the base image's fonts, and its
# golden checksums describe that baseline instead.
COPY docker/hostfonts.ta[r] /tmp/
RUN if [ -f /tmp/hostfonts.tar ]; then \
        tar -xf /tmp/hostfonts.tar -C / && rm /tmp/hostfonts.tar; \
    fi; fc-cache -f

WORKDIR /opt/w2c
COPY renderer/package.json renderer/package-lock.json renderer/
RUN cd renderer && npm ci --no-audit --no-fund

COPY renderer/ renderer/
COPY w2c_render/ w2c_render/
COPY .claude/skills/ .claude/skills/
COPY docker/selfcheck.py docker/selfcheck.py
COPY docker/golden/ docker/golden/
COPY docker/golden.json docker/golden.json

# Warm Vite once here so a container start never pays for it: the dependency
# pre-bundle (esbuild over react/echarts/recharts/react-icons) is the slow part of a cold
# start, and baking it in turns a multi-minute first start on a loaded machine
# into seconds. Then make the tree writable — the container runs as the
# invoking host user (run.sh --user), and Vite writes its dep cache and
# config-bundle staging inside node_modules.
#
# Never fatal. A build that dies because the warm-up was OOM-killed on a busy
# machine fails for a reason that has nothing to do with what it is building:
# the image renders identically either way, only its first start is slower.
RUN cd renderer && \
    (node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5199 --strictPort & \
     VPID=$!; \
     for _ in $(seq 1 180); do \
       python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5199',timeout=2)" \
         2>/dev/null && break; \
       sleep 1; \
     done; \
     kill -TERM $VPID 2>/dev/null; \
     for _ in $(seq 1 20); do \
       kill -0 $VPID 2>/dev/null || break; \
       sleep 0.25; \
     done; \
     kill -KILL $VPID 2>/dev/null; wait $VPID 2>/dev/null; true) || true; \
    chmod -R a+rwX /opt/w2c/renderer

# The daemon's socket + heartbeat live here; mount the same path from the host
# so a client outside the container finds them.
ENV W2C_RENDER_RUNTIME_DIR=/tmp/w2c-render

# Identifies this exact build. The self-check marker is keyed on it, so a
# machine re-proves itself iff the image actually changed.
ARG BUILD_STAMP=dev
ENV W2C_IMAGE_STAMP=${BUILD_STAMP}

# Verify the frozen environment renders the golden set byte-exactly, then
# serve. A host whose pixels deviate refuses to come up instead of quietly
# contaminating whatever consumes the renders. `--cached` records the proof in
# the mounted runtime dir, so only the first start of an image on a machine
# pays for it.
CMD ["bash", "-c", "python docker/selfcheck.py --cached && exec python w2c_render/supervisor.py --workers ${W2C_RENDER_WORKERS:-8}"]

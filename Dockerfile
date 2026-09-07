# Build stage
FROM python:3.13 AS builder

# Install system dependencies including Node.js and npm
RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    gnupg \
    dos2unix \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js and npm
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs

# Verify Node.js and npm installation
RUN node --version && npm --version

RUN mkdir -p /app/.web
RUN python -m venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install python app requirements and reflex in the container
COPY requirements.txt .
RUN pip install -r requirements.txt

# Install Playwright browsers for vidembed iframe authentication (without system dependencies)
RUN playwright install chromium

# Copy local context to `/app` inside container (see .dockerignore)
COPY . .

# Convert start.sh to Unix line endings and make it executable
RUN dos2unix /app/start.sh && chmod +x /app/start.sh

ARG PORT BACKEND_PORT API_URL DADDYLIVE_URI PROXY_CONTENT SOCKS5

# Set environment variables for the build
ENV PORT=${PORT:-3000} \
    BACKEND_PORT=${BACKEND_PORT:-8005} \
    BACKEND_URI=${BACKEND_URI:-http://0.0.0.0:${BACKEND_PORT:-8005}} \
    API_URL=${API_URL:-http://0.0.0.0:${PORT:-3000}} \
    DADDYLIVE_URI=${DADDYLIVE_URI:-"https://dlhd.st"} \
    PROXY_CONTENT=${PROXY_CONTENT:-TRUE} \
    SOCKS5=${SOCKS5:-""} \
    REFLEX_ENV=prod

# Initialize Reflex and build frontend
# ponytail: no `|| minimal frontend` fallback. It swallowed the real rolldown
# error and shipped a "successful" image serving a 152-byte stub. Fail loudly.
# Build the frontend with npm, not bun.
#
# REFLEX_USE_NPM makes `reflex init`/`reflex export` skip downloading bun at
# build time. That download (and Reflex's default npmmirror.com registry probe)
# are network calls made before any of our own npm config applies, so on a
# restricted or proxied network they fail and take the whole image build with
# them. Node 22 and npm are already installed above, so bun buys nothing here
# and only adds a failure mode.
ENV REFLEX_USE_NPM=1 \
    NPM_CONFIG_REGISTRY=https://registry.npmjs.org/

# Each stage announces itself before it runs, so a failure names the step that
# broke instead of reporting one exit code for the whole chain. `set -e` keeps
# the original fail-loudly behaviour -- there is deliberately no fallback that
# would ship an image serving a stub page.
RUN set -e; \
    echo "=== [1/5] environment ==="; \
    echo "API_URL=$API_URL"; \
    echo "reflex: $(reflex --version)"; \
    echo "node:   $(node --version)"; \
    echo "npm:    $(npm --version)"; \
    mkdir -p /srv; \
    echo "=== [2/5] npm config (BEFORE reflex init, which resolves packages) ==="; \
    npm config set strict-ssl false; \
    npm config set registry https://registry.npmjs.org/; \
    npm config get registry; \
    cd /app; \
    echo "=== [3/5] reflex init + npm install ==="; \
    reflex init; \
    cd .web; \
    npm install --legacy-peer-deps; \
    cd ..; \
    echo "=== [4/5] reflex export (bundling) ==="; \
    reflex export --frontend-only --no-zip; \
    echo "=== [5/5] publishing to /srv ==="; \
    test -d .web/build/client || { echo "ERROR: .web/build/client missing - export produced no output"; ls -la .web || true; exit 1; }; \
    mv .web/build/client/* /srv/; \
    rm -rf .web; \
    test -f /srv/index.html || { echo "ERROR: /srv/index.html missing after export"; exit 1; }; \
    echo "Frontend build successful - contents of /srv:"; \
    ls -la /srv/

# Final image with only necessary files
FROM python:3.13-slim

# Install Caddy, redis server, Node.js/npm, and Playwright system dependencies inside final image.
#
# The xvfb/pulseaudio/ffmpeg block serves virtual channels (a web page restreamed
# as live HLS, see freesky/virtual_session.py):
#   xvfb, x11-utils  - a private X display per session, which ffmpeg's x11grab captures
#   xdotool          - X-level mouse/keyboard injection for the admin control panel,
#                      so a click can reach Chromium's own UI (a save-password
#                      bubble, an autofill dropdown) and not just page content
#   pulseaudio(-utils) - the per-session null-sink Chromium plays into and whose
#                        .monitor ffmpeg records. Chromium runs HEADFUL against the
#                        X display precisely because headless Chrome has no reliable
#                        audio output path in a container.
#   ffmpeg           - capture and H.264/AAC encode to a rolling HLS playlist
#   dbus, dbus-x11   - Chromium expects a session bus when running headful
#   dumb-init        - PID 1, see ENTRYPOINT below
#   fonts-*          - without these every captured page renders as tofu boxes
#   libdrm2/libpango/libcairo2/libxss1 - headful Chromium needs these beyond the
#                        headless set already listed above
RUN apt-get update -y && apt-get install -y \
    caddy \
    redis-server \
    curl \
    gnupg \
    dos2unix \
    libnspr4 \
    libnss3 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    xvfb \
    x11-utils \
    xdotool \
    pulseaudio \
    pulseaudio-utils \
    ffmpeg \
    dbus \
    dbus-x11 \
    dumb-init \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-noto-core \
    fonts-noto-color-emoji \
    fontconfig \
    libdrm2 \
    libpango-1.0-0 \
    libcairo2 \
    libxss1 \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Verify Node.js and npm are available in final image
RUN node --version && npm --version

ARG PORT BACKEND_PORT API_URL DADDYLIVE_URI PROXY_CONTENT SOCKS5
ENV PATH="/app/.venv/bin:$PATH" \
    PORT=${PORT:-3000} \
    BACKEND_PORT=${BACKEND_PORT:-8005} \
    BACKEND_URI=${BACKEND_URI:-http://0.0.0.0:${BACKEND_PORT:-8005}} \
    API_URL=${API_URL:-${BACKEND_URI}:${PORT:-3000}} \
    DADDYLIVE_URI=${DADDYLIVE_URI:-"https://dlhd.st"} \
    REDIS_URL=redis://0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PROXY_CONTENT=${PROXY_CONTENT:-TRUE} \
    SOCKS5=${SOCKS5:-""} \
    WORKERS=${WORKERS:-1} \
    REFLEX_ENV=prod \
    REFLEX_SKIP_COMPILE=1

WORKDIR /app
COPY --from=builder /app /app
COPY --from=builder /srv /srv
COPY --from=builder /root/.cache/ms-playwright /root/.cache/ms-playwright

# Convert start.sh to Unix line endings and make it executable in the final image
RUN dos2unix /app/start.sh && chmod +x /app/start.sh

# Needed until Reflex properly passes SIGTERM on backend.
STOPSIGNAL SIGKILL

EXPOSE $PORT $BACKEND_PORT 3443

# dumb-init as PID 1. Chromium forks a tree of renderer/GPU/utility processes;
# with a shell as PID 1 none of them are reaped and the container accumulates
# zombies until it hits the PID limit. This is the documented answer to the
# zombie-process problem that follows containerised Chrome everywhere.
ENTRYPOINT ["/usr/bin/dumb-init", "--"]

# Starting the backend with multiple workers
CMD ["/app/start.sh"]
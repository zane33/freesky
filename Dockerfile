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
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
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
RUN echo "Building frontend with API_URL=$API_URL" && \
    echo "Reflex version: $(reflex --version)" && \
    mkdir -p /srv && \
    cd /app && \
    reflex init && \
    cd .web && \
    npm config set strict-ssl false && \
    npm config set registry https://registry.npmjs.org/ && \
    npm install --legacy-peer-deps && \
    cd .. && \
    reflex export --frontend-only --no-zip && \
    mv .web/build/client/* /srv/ && \
    rm -rf .web && \
    echo "Frontend build successful - contents of /srv:" && \
    ls -la /srv/

# Final image with only necessary files
FROM python:3.13-slim

# Install Caddy, redis server, Node.js/npm, and Playwright system dependencies inside final image
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
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
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
    WORKERS=${WORKERS:-6} \
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

EXPOSE $PORT $BACKEND_PORT

# Starting the backend with multiple workers
CMD ["/app/start.sh"]
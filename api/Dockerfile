# cadquery 2.8.0 requires Python >= 3.12.
FROM python:3.12-slim

# CadQuery/OCP need a few X and GL libraries even headless.
# The second group is Chromium's runtime set for the Playwright label scrape. We install
# it by hand because `playwright install --with-deps` misdetects Debian trixie as
# ubuntu20.04-arm64 and then demands ttf-unifont / ttf-ubuntu-font-family, which do not
# exist here. Note the t64 suffixes — trixie renamed these in the 64-bit time_t transition.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglu1-mesa libxrender1 libxext6 libsm6 libx11-6 ca-certificates \
      libnss3 libnspr4 libdbus-1-3 libatk1.0-0t64 libatk-bridge2.0-0t64 \
      libcups2t64 libdrm2 libatspi2.0-0t64 libxcomposite1 libxdamage1 \
      libxfixes3 libxrandr2 libgbm1 libxkbcommon0 libasound2t64 \
      libpango-1.0-0 libcairo2 fonts-liberation fonts-unifont \
 && rm -rf /var/lib/apt/lists/*

# uv replaces pip here. The dependency set lives in pyproject.toml; uv.lock pins the
# exact resolution, so the image is reproducible without a requirements.txt.
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /usr/local/bin/uv

# The venv lives outside /app because docker-compose bind-mounts ./generator into
# /app/generator; keeping it at /opt/venv means no mount can ever shadow it.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app
COPY pyproject.toml uv.lock ./
# --frozen: fail loudly if the lock does not match pyproject.toml, rather than
# silently re-resolving and producing a different image than the one we verified.
# --no-cache: with UV_LINK_MODE=copy the uv cache would be a second full copy of every
# package inside this layer; on a small disk (Codespaces) that made the export fail.
RUN uv sync --frozen --no-dev --no-cache \
 && /opt/venv/bin/playwright install chromium

ENV PATH="/opt/venv/bin:$PATH"

COPY app.py /app/app.py

ENV CACHE_DIR=/data/cache OUT_DIR=/data/out PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

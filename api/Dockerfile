# cadquery 2.8.0 requires Python >= 3.12.
FROM python:3.12-slim

# CadQuery/OCP need a few X and GL libraries even headless. No browser: the service works
# offline from generator/assets (the one-off asset downloader runs outside this image).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglu1-mesa libxrender1 libxext6 libsm6 libx11-6 ca-certificates \
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
RUN uv sync --frozen --no-dev --no-cache

ENV PATH="/opt/venv/bin:$PATH"

COPY app.py /app/app.py

ENV CACHE_DIR=/data/cache OUT_DIR=/data/out PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

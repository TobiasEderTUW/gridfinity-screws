# Throwaway headless-Chromium image for editor tests (the api image has no browser).
# docker build -t schublade-uitest -f tools/uitest.Dockerfile tools
# docker run --rm --network bridge -v $PWD:/w schublade-uitest python /w/<test>.py   (page: http://172.17.0.1:8080/)
# bookworm, not trixie: `playwright install --with-deps` misdetects trixie. ~2 GB; remove it after use.
FROM python:3.12-slim-bookworm
RUN pip install --no-cache-dir playwright==1.49.1 && playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

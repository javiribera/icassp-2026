# ── Lint + type-check (run locally or as a Cloud Build gate) ─────────────────
FROM python:3.12-slim AS lint

WORKDIR /app
COPY requirements.txt scraper.py ./
RUN pip install --no-cache-dir ruff mypy types-requests \
    && ruff check scraper.py \
    && python -m mypy scraper.py --ignore-missing-imports


# ── REST API mode (lightweight, no browser) ───────────────────────────────────
FROM python:3.12-slim AS api

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir requests tqdm

COPY scraper.py .
RUN mkdir -p /app/output

RUN useradd --no-create-home --uid 1000 scraper \
    && chown scraper /app/output
USER scraper

VOLUME ["/app/output"]
ENTRYPOINT ["python", "scraper.py"]
CMD ["--output", "/app/output"]


# ── Browser mode (Chromium via Playwright) ────────────────────────────────────
FROM python:3.12-slim AS browser

# Playwright/Chromium system dependencies (Debian Bookworm).
# libasound2 was renamed to libasound2t64 in Bookworm; install both names so
# the image builds correctly regardless of the base image minor version.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libatspi2.0-0 \
        libasound2t64 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir requests tqdm playwright \
    && playwright install chromium

COPY scraper.py .
RUN mkdir -p /app/output

# Cloud Run runs containers as root by default; --no-sandbox is required for
# Chromium when running as root or without user-namespace support (both are
# common in containerised environments). The flag is set in scraper.py.
VOLUME ["/app/output"]
ENTRYPOINT ["python", "scraper.py"]
CMD ["--browser", "--output", "/app/output"]

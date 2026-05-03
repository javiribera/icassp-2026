# ── REST API mode (default, lightweight) ────────────────────────────────────
FROM python:3.12-slim AS api

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir requests tqdm
COPY scraper.py .
VOLUME ["/app/output"]
ENTRYPOINT ["python", "scraper.py"]
CMD ["--output", "/app/output"]


# ── Browser mode (--browser flag, includes Chromium) ────────────────────────
FROM python:3.12-slim AS browser

# Chromium system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir requests tqdm playwright \
    && playwright install chromium

COPY scraper.py .
VOLUME ["/app/output"]
ENTRYPOINT ["python", "scraper.py"]
CMD ["--browser", "--output", "/app/output"]

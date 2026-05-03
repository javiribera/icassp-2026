FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py .

# Output lands in /app/output; mount a host directory here to retrieve files.
VOLUME ["/app/output"]

ENTRYPOINT ["python", "scraper.py"]
CMD ["--output", "/app/output"]

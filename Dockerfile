FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

COPY scraper.py estimate_relevance.py ./
RUN mkdir -p /app/output

VOLUME ["/app/output"]
ENTRYPOINT ["python", "scraper.py"]
CMD ["--output", "/app/output"]

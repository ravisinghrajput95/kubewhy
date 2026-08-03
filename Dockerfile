FROM python:3.13-slim

# Unbuffered so logs appear immediately under docker logs; no .pyc in a
# layer that is thrown away anyway.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copied first so the dependency layer is cached across source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The service only ever reads; there is no reason for it to run as root.
RUN useradd --create-home --uid 1000 triage && chown -R triage:triage /app
USER triage

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/platform')"

CMD ["fastapi", "run", "app.py", "--host", "0.0.0.0", "--port", "8000"]

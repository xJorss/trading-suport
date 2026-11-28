# syntax=docker/dockerfile:1

FROM python:3.11-slim

# No .pyc files in the image; unbuffered stdout so `docker logs` streams live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is only rebuilt when requirements.txt changes,
# so day-to-day source edits stay fast.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code. pairs.json ships with the image but is also bind-mounted by
# docker-compose, so the market can be changed without a rebuild.
COPY . .

# Non-root. /app must stay writable because the default state file lives there;
# docker-compose redirects it to the mounted volume via STATE_FILE_PATH.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# slim has no curl/wget; probe with the interpreter that is already present.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

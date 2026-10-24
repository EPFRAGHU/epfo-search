# Container image for running the app on Coolify (or any Docker host).
# Coolify: Build Pack "Dockerfile", Ports Exposes 8765, add your domain.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EPFO_HOST=0.0.0.0 \
    EPFO_PORT=8765 \
    EPFO_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
# Generous timeout/retries: some VPS links to PyPI are slow
RUN pip install --no-cache-dir --timeout 120 --retries 10 -r requirements.txt

COPY . .
# Build the local place index into the image so the first map is instant,
# then run as an unprivileged user that can still write the geocode cache.
RUN python -c "import epfo_geocode; epfo_geocode._places('PUNE')" \
    && useradd --system epfo \
    && mkdir -p /data && chown -R epfo /app /data
# Saved exact locations live in /data: mount a persistent volume there
# (Coolify: Persistent Storage, destination /data) so redeploys keep them.
VOLUME /data
USER epfo

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=4)"
CMD ["python", "epfo_web.py"]

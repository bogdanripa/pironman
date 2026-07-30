# The shared static host. Built for linux/arm64 (Raspberry Pi), listening on port
# 80 bound to :: (dual-stack) and running as root — the platform's own rules.
FROM python:3.12-slim

RUN pip install --no-cache-dir fastapi==0.115.6 "uvicorn[standard]==0.34.0" httpx==0.28.1

WORKDIR /srv
COPY server.py /srv/server.py

# Bundles are mounted here by the platform (shared volume with paas-api).
VOLUME ["/srv/frontends"]
EXPOSE 80

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:80/_health')"

CMD ["uvicorn", "server:app", "--host", "::", "--port", "80"]

# The shared static host. Built for linux/arm64 (Raspberry Pi), listening on port
# 80 bound to :: (dual-stack) and running as root — the platform's own rules.
FROM python:3.12-slim

# curl is not optional: Coolify's healthcheck runs inside the container and tries
# curl (then wget), and python:*-slim ships neither. Without it every deploy fails
# its healthcheck and gets rolled back even though the server is fine.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi==0.115.6 "uvicorn[standard]==0.34.0" httpx==0.28.1

WORKDIR /srv
COPY server.py run.py /srv/

# Bundles are mounted here by the platform (shared volume with paas-api).
VOLUME ["/srv/frontends"]
EXPOSE 80

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -fsS http://localhost:80/_health || exit 1

# run.py, not `uvicorn --host ::` — see run.py: that flag yields an IPv6-only
# socket on Python, which passes the localhost healthcheck but refuses the
# proxy's IPv4 connection, so the app 502s while reporting healthy.
CMD ["python", "run.py"]

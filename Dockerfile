FROM python:3.12-slim

# docker CLI so provision.py can exec into the database containers through
# the mounted socket
RUN apt-get update && apt-get install -y --no-install-recommends \
        docker.io curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s \
  CMD curl -fsS http://localhost:80/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]

COPY pdb /usr/local/bin/pdb
RUN chmod +x /usr/local/bin/pdb

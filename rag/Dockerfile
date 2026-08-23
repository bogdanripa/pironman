# The RAG service. Built for linux/arm64 (Raspberry Pi), listening on port 80
# bound dual-stack, running as root -- the platform's own rules, see
# apps_deploy_workflow's dockerfile_requirements.
FROM python:3.12-slim

# curl is not optional: Coolify's healthcheck runs INSIDE the container and
# tries curl then wget, and python:*-slim ships neither. Without it every deploy
# fails its healthcheck and is rolled back while the server is perfectly fine.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Every one of these has an aarch64 wheel -- verified on the box before this was
# written, so the emulated arm64 build stays a download rather than a compile.
# Note what is NOT here: torch. The embedding model is static, so there is no
# inference runtime at all.
RUN pip install --no-cache-dir \
      fastapi==0.115.6 "uvicorn[standard]==0.34.0" httpx==0.28.1 \
      numpy==2.5.2 model2vec==0.9.0 \
      pypdf==6.16.1 python-docx==1.2.0 python-pptx==1.0.2 selectolax==0.4.11

# Bake the model into the image. Downloading it at runtime would put a 30MB
# fetch in front of the first caller after every cold wake, and would make the
# service fail closed whenever Hugging Face is unreachable. From the baked copy
# it loads in 0.12s -- measured on this hardware.
RUN python -c "from model2vec import StaticModel; \
      StaticModel.from_pretrained('minishlab/potion-base-8M').save_pretrained('/srv/model')"

WORKDIR /srv
# Copy every module rather than a hand-maintained list: an omitted import builds
# fine, crashes at startup, fails the healthcheck and gets rolled back -- so the
# old container keeps serving and the deploy looks like it worked.
COPY *.py /srv/rag/

# The SQLite file lives here. This MUST be backed by a Coolify persistent volume
# (/data/pironman/rag -> /data, the same mechanism as the static host's
# frontends bind mount) -- an anonymous VOLUME would be a fresh empty directory
# on every redeploy, silently discarding every indexed document while the
# service came up looking healthy.
ENV RAG_DB_PATH=/data/rag.sqlite3
ENV RAG_MODEL_PATH=/srv/model
EXPOSE 80

# --start-interval is the flag Coolify's API cannot express and silently drops
# from custom_docker_run_options, and it is worth carrying: Docker's default
# delay before the FIRST probe is 5s, which is most of a cold wake for an app
# that sleeps. Measured on this box, it takes a wake from ~5.3s to ~0.5s.
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    --start-interval=250ms \
    CMD curl -fsS http://localhost:80/healthz || exit 1

# -m rag.run, not `python rag/run.py`: the package has to be importable as `rag`
# for the relative imports in server.py to resolve.
CMD ["python", "-m", "rag.run"]

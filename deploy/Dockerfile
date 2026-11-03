# Build only from deploy/prepare_context.py's allowlisted context, never repo root.
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SPINREAD_EMBED_WORKER=false SPINREAD_BOOTSTRAP_STORAGE=false SPINREAD_SEED_DEMO=false \
    SPINREAD_TMP_DIR=/tmp/spinread OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY training /app/training
COPY server /app/server
RUN pip install --no-cache-dir --no-deps /app/training /app/server && \
    mkdir -p /app/bin /tmp/spinread && chown app:app /tmp/spinread && \
    printf '#!/bin/sh\nexec python -m spinread.remote.dispatcher "$@"\n' > /app/bin/rallyloop-dispatcher && \
    chmod 755 /app/bin/rallyloop-dispatcher
WORKDIR /app/server
USER 10001:10001

FROM base AS api
EXPOSE 8000
CMD ["uvicorn", "spinread.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

FROM base AS dispatcher
CMD ["/app/bin/rallyloop-dispatcher"]

FROM base AS compute
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
USER 10001:10001
CMD ["python", "-m", "spinread.remote.dispatcher", "--check"]

# Optional NLF/BlurBall/ball-3D runtime. Model assets are mounted separately.
# This target needs a dedicated GPU acceptance run before enabling engines.
FROM compute AS gpu
USER root
COPY requirements-gpu.lock /app/requirements-gpu.lock
RUN pip install --no-cache-dir --require-hashes -r /app/requirements-gpu.lock
USER 10001:10001

# Isolated verification only; this target is never a deployment image.
FROM compute AS verify
USER root
RUN pip install --no-cache-dir pytest==8.4.2
COPY tests /app/verification
USER 10001:10001
CMD ["python", "-m", "pytest", "/app/verification", "-q", "-p", "no:cacheprovider"]

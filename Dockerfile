# Recall v2 — multi-stage Docker build
# Stage 1: builder — installs all deps with uv
# Stage 2: runtime — lean image with only the venv

# ── Builder ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:/root/.local/bin:$PATH"

WORKDIR /build
COPY . .

# Install all workspace deps (no dev group)
RUN uv sync --frozen --no-dev

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy venv from builder
COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build /app

# Ensure venv is used
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Default recall data dirs
ENV RECALL_DB_PATH=/data/recall.db
ENV RECALL_KUZU_PATH=/data/graph
ENV RECALL_CHROMA_PATH=/data/chroma
ENV RECALL_VAULT_PATH=/vault

EXPOSE 37777

ENTRYPOINT ["python", "-m", "recall"]
CMD ["serve"]

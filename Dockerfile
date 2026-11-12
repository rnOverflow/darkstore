# One image for every Python service. Select the entrypoint at runtime with SERVICE=<name>.
FROM python:3.12-slim AS base
# LightGBM needs the OpenMP runtime, which the slim image does not ship
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
RUN uv sync --frozen --no-dev && useradd --system --uid 10001 --no-create-home app
USER app
ENV SERVICE=order PATH="/app/.venv/bin:$PATH"
CMD ["sh", "-c", "exec python -m darkstore_${SERVICE}"]

# One base image for every Python service. Select the entrypoint with SERVICE.
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
RUN uv sync --frozen --no-dev
ARG SERVICE=order
ENV SERVICE=${SERVICE}
CMD ["sh", "-c", "uv run --no-sync python -m darkstore_${SERVICE}"]

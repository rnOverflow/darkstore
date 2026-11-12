#!/usr/bin/env python3
"""Usage: uv run --no-sync python scripts/replay.py <dlq-topic> [--bootstrap host:port] [--limit N]"""
from darkstore_common.replay import cli

cli()

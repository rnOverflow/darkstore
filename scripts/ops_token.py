#!/usr/bin/env python3
"""Print an ops-dashboard URL carrying a fresh ops token (valid 4 h). Reads TOKEN_SECRET from .env."""
import os
import sys
from pathlib import Path

from darkstore_common.tokens import sign

secret = os.environ.get("TOKEN_SECRET")
env = Path.cwd() / ".env"   # repo root under make; inside the order container TOKEN_SECRET is already in the env
if not secret and env.exists():
    secret = dict(line.split("=", 1) for line in env.read_text().split() if "=" in line).get("TOKEN_SECRET")
if not secret:
    sys.exit("TOKEN_SECRET not found: run `make env`")
print(f"http://localhost:8088/ops/#token={sign(secret, role='ops', ttl_s=4 * 3600)}")

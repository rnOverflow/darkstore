#!/usr/bin/env python3
"""Register all event JSON Schemas. Usage: register_schemas.py [registry_url]"""
import sys

from darkstore_common.schemas import register_all

ids = register_all(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8081")
for k, v in sorted(ids.items()):
    print(f"{v:>4}  {k}")

#!/usr/bin/env python3
"""Run every dashboard query against a live Prometheus: syntax errors fail, empty results are reported.

    python scripts/check_dashboards.py [http://localhost:9090] [--require-data]
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gen_dashboards import all_queries  # noqa: E402


def run(base: str, expr: str) -> tuple[str, int]:
    expr = expr.replace("$__rate_interval", "1m").replace("$__range", "30m")
    url = f"{base}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310 - local Prometheus
        body = json.load(r)
    if body["status"] != "success":
        return "error", 0
    return "ok", len(body["data"]["result"])


def main() -> int:
    base = next((a for a in sys.argv[1:] if a.startswith("http")), "http://localhost:9090")
    errors, empty, ok = [], [], 0
    for dash, panel, expr in all_queries():
        try:
            status, n = run(base, expr)
        except Exception as e:  # noqa: BLE001
            errors.append((dash, panel, f"{type(e).__name__}: {getattr(e, 'read', lambda: b'')()[:200]!r}"))
            continue
        if status != "ok":
            errors.append((dash, panel, status))
        elif n == 0:
            empty.append((dash, panel))
        else:
            ok += 1
        print(f"{'ok   ' if n else 'EMPTY'} {dash} · {panel}")
    print(f"\n{ok} queries returned data, {len(empty)} empty, {len(errors)} failed")
    for e in errors:
        print("FAILED", *e)
    return 1 if errors or (empty and "--require-data" in sys.argv) else 0


if __name__ == "__main__":
    sys.exit(main())

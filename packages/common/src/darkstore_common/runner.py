"""`python -m darkstore_<svc>` entrypoint: run until SIGTERM/SIGINT."""
from __future__ import annotations

import asyncio
import logging
import signal

from .settings import Settings


def main(serve, default_port: int = 8000) -> None:
    logging.basicConfig(level=logging.INFO)

    async def _run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        settings = Settings()
        if "http_port" not in settings.model_fields_set:
            settings = settings.model_copy(update={"http_port": default_port})
        await serve(settings, stop)

    asyncio.run(_run())

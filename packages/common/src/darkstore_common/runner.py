"""`python -m darkstore_<svc>` entrypoint: run until SIGTERM/SIGINT."""
from __future__ import annotations

import asyncio
import logging
import signal

from . import telemetry
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
        telemetry.init_from_settings(telemetry.service_name_from(serve.__module__), settings)
        try:
            await serve(settings, stop)
        finally:
            await asyncio.to_thread(telemetry.shutdown)   # flush the last spans and metrics

    asyncio.run(_run())

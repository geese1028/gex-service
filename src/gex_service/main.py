"""Entry point: ``gex-service`` or ``python -m gex_service.main``."""

from __future__ import annotations

import logging

import uvicorn

from .api import create_app
from .config import get_settings


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # ib_async is chatty about per-contract "no security definition" notices.
    logging.getLogger("ib_async.wrapper").setLevel(logging.ERROR)
    logging.getLogger("ib_async.client").setLevel(logging.WARNING)


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        loop="asyncio",
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )


if __name__ == "__main__":
    run()

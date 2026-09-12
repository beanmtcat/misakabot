from __future__ import annotations

import logging

import uvicorn

from .api import create_app
from .config import Settings


def main() -> None:
    settings = Settings.from_environment()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, access_log=True)

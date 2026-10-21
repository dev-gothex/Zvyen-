from __future__ import annotations

import logging
import os
import platform
import socket
import sys
import warnings
from importlib.metadata import version

warnings.filterwarnings(
    "ignore",
    message=r".*count.*positional argument.*",
    category=DeprecationWarning,
    module=r"discord\.utils",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)

from core.settings import settings

if __name__ == "__main__":
    if not settings.token:
        raise SystemExit("Set DISCORD_TOKEN in your .env file first.")
    from core.bot import bot

    logging.getLogger("zyven").info(
        "Starting bot worker host=%s pid=%s python=%s discord.py=%s database=SQLite+MongoDB-mirror",
        socket.gethostname(), os.getpid(), platform.python_version(), version("discord.py"),
    )
    bot.run(settings.token, log_handler=None)

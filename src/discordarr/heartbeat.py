"""A heartbeat the container's healthcheck can see.

Kuma watched the discordarr container, but a container can be "running"
while the bot inside it is useless: disconnected from Discord's gateway, or
stuck. So while -- and only while -- the bot is logged in and receiving
gateway heartbeats, it touches a file every 30 seconds. The Docker
healthcheck (`discordarr-healthcheck`, run by the image's HEALTHCHECK) calls
the container unhealthy once that file is older than two minutes. Kuma's
docker monitor reports that state, and freddy's rollout waits on it.

A bot that is idling on purpose (a missing token, see discord_bot._idle)
never beats, so it reads as unhealthy too. That is the truth: it is not
serving anyone.
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

HEARTBEAT_FILE = Path(os.environ.get("DISCORDARR_HEARTBEAT_FILE", "/tmp/discordarr-heartbeat"))
BEAT_SECONDS = 30
MAX_AGE_SECONDS = 120


def should_beat(is_ready: bool, is_closed: bool, latency: float) -> bool:
    """Whether the bot is actually connected right now.

    discord.py reports `latency` as NaN until the first gateway heartbeat is
    acknowledged, and as infinity after the websocket drops, so a finite
    latency on a ready, open client is the positive signal: connected, and
    hearing back from Discord.
    """
    return is_ready and not is_closed and math.isfinite(latency)


def is_fresh(path: Path, now: float, max_age: float = MAX_AGE_SECONDS) -> bool:
    """Whether the heartbeat file exists and was touched within `max_age`."""
    try:
        return now - path.stat().st_mtime < max_age
    except OSError:
        return False


def install(bot):  # type: ignore[no-untyped-def]
    """Beat from inside the bot's own event loop, so a stuck loop stops the
    beat as surely as a lost connection does."""
    from discord.ext import tasks

    @tasks.loop(seconds=BEAT_SECONDS)
    async def beat() -> None:
        if should_beat(bot.is_ready(), bot.is_closed(), bot.latency):
            HEARTBEAT_FILE.touch()

    async def start() -> None:
        if not beat.is_running():
            beat.start()

    bot.add_listener(start, "on_ready")
    return beat


def main() -> None:
    """`discordarr-healthcheck`: exit 0 when the heartbeat is fresh, 1 when not."""
    sys.exit(0 if is_fresh(HEARTBEAT_FILE, time.time()) else 1)

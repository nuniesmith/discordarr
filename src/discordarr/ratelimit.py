"""Per-Discord-user rate limit for movie/show/music ADDS.

Shelfmark's own rate limiting lives on the API side: it answers a 429 that
discord_bot.py turns into a sentence via `_rate_limit_message`
(clients.py/discord_bot.py). There is no equivalent server-side limiter for
Radarr/Sonarr/Lidarr requests -- adding a movie, a show or an album is a
single ordinary POST as far as those apps are concerned -- so the same
protection has to live here instead, shared across all three commands
because the thing being bounded is "how many things has this person just
asked three separate media servers to fetch", not a per-service budget.

Modeled on the SAME reasoning as shelfmark's `rate_limit_action_*` (see its
config.py): generous enough that a normal request session never notices,
tight enough to bound a slip or an impatient run of presses before it piles
up unwanted downloads across three independent libraries.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class AddRateLimiter:
    """A fixed-window counter per key (the Discord actor string).

    A plain in-memory dict is enough: discordarr runs as one process with no
    shared state to synchronize, the same scope shelfmark's own per-process
    breaker registry (clients.py's `_BREAKER_REGISTRY`) already assumes.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_requests = max(1, int(max_requests))
        self.window_seconds = max(1.0, float(window_seconds))
        self._clock = clock
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def try_consume(self, key: str) -> float | None:
        """Record one use for `key` if under budget.

        Returns `None` when the use was allowed (and recorded). Returns the
        number of seconds until the oldest hit in the window ages out
        otherwise -- recorded nowhere, so a blocked press never counts
        against the budget it was just refused from.
        """
        now = self._clock()
        with self._lock:
            hits = [t for t in self._hits.get(key, ()) if now - t < self.window_seconds]
            if len(hits) >= self.max_requests:
                self._hits[key] = hits
                return self.window_seconds - (now - min(hits))
            hits.append(now)
            self._hits[key] = hits
            return None


def wait_text(seconds: float) -> str:
    """'Try again in N minutes.' -- the same phrasing discord_bot.py's
    `_rate_limit_wait_text` uses for the Shelfmark API's own 429s, so a
    person sees one consistent style regardless of which limiter caught
    them."""
    seconds = max(0.0, seconds)
    if seconds >= 60:
        minutes = max(1, round(seconds / 60))
        return f"Try again in {minutes} minute{'s' if minutes != 1 else ''}."
    whole = max(1, round(seconds))
    return f"Try again in {whole} second{'s' if whole != 1 else ''}."


def rate_limit_message(kind: str, seconds: float) -> str:
    """The full sentence for a locally-enforced add limit -- mirrors
    discord_bot.py's `_rate_limit_message` shape ("You have run too many
    X. Try again in N.") so every command in the bot reads the same way
    whether the limit came from Shelfmark's API or from here."""
    return f"You have run too many {kind}. {wait_text(seconds)}"

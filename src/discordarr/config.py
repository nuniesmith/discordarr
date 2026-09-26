"""Environment-backed settings for discordarr.

Trimmed from shelfmark's ``src/shelfmark_service/config.py``: that dataclass
also configures a worker, an API, qBittorrent, and an SSH pull from a second
host, none of which discordarr runs. ``discord_large_release_threshold_mb``
is kept because the ported ``discord_bot.py`` reads it (see its
``install_commands`` default argument); everything else here is new, for the
Radarr/Sonarr/Lidarr commands in ``media_bot.py``.

``DISCORD_BOT_TOKEN``, ``SHELFMARK_API_URL``, ``SHELFMARK_API_TOKEN`` and the
other ``SHELFMARK_DISCORD_*`` variables are deliberately NOT read here --
``discord_bot.py`` reads those directly via `os.environ`, exactly as it did
in shelfmark, so that module stays a behaviour-identical port rather than
being wired through a second settings object.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _float_from_env(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return max(0.1, float(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _int_from_env(name: str, default: int, *, minimum: int = 1) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return max(minimum, int(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number") from exc


def _str_from_env(name: str) -> str | None:
    value = (os.environ.get(name) or "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the Radarr/Sonarr/Lidarr commands.

    Every field defaults to "not configured" (`None`, or a documented
    default) so that `Settings()` -- no environment read at all -- is a
    valid, inert configuration, the same way shelfmark's own `Settings()`
    is. Tests build this directly; only `from_env()` touches `os.environ`.
    """

    # Ported: discord_bot.py's install_commands falls back to
    # `Settings().discord_large_release_threshold_mb` when the bot's own
    # caller does not pass large_release_threshold_bytes explicitly (see
    # tests/test_discord_bot.py's CommandRegistrationTests, which does
    # exactly that).
    discord_large_release_threshold_mb: float = 5000.0

    radarr_url: str | None = None
    radarr_api_key: str | None = None
    radarr_root_folder: str | None = None
    radarr_quality_profile: str | None = None

    sonarr_url: str | None = None
    sonarr_api_key: str | None = None
    sonarr_root_folder: str | None = None
    sonarr_quality_profile: str | None = None
    # A show at or above either threshold stops for confirmation instead of
    # being requested on the first press -- see media_bot.py's
    # `_is_big_show`. Modeled on discord_bot.py's own
    # `discord_large_release_threshold_mb` gate for the same reason: a
    # mis-ranked or simply huge add should cost one extra deliberate press,
    # not queue itself on the tap that only meant to preview it.
    sonarr_big_show_season_threshold: int = 5
    sonarr_big_show_episode_threshold: int = 100

    lidarr_url: str | None = None
    lidarr_api_key: str | None = None
    lidarr_root_folder: str | None = None
    lidarr_quality_profile: str | None = None
    lidarr_metadata_profile: str = "Standard"
    # Adding a new artist populates its albums asynchronously (Lidarr
    # refreshes the discography from its metadata server in the
    # background) -- these bound how long `/music` waits for the just-added
    # album to appear before giving up and saying so. 5 attempts * 2s is
    # long enough for a normal refresh without holding the interaction open
    # for the full 15-minute webhook-token window.
    lidarr_album_poll_attempts: int = 5
    lidarr_album_poll_seconds: float = 2.0

    # Shared by every *arr HttpClient (see arr_clients.py) -- one timeout/
    # retry policy for all three, rather than three separate knobs nobody
    # would tune independently in practice.
    arr_http_timeout: float = 15.0
    arr_http_retries: int = 2

    # Per-Discord-user budget for movie/show/music ADDS specifically (not
    # searches -- see media_bot.py's guard placement). Modeled on
    # shelfmark's own rate_limit_action_* fields and discord_bot.py's
    # _rate_limit_message: generous enough that normal use never notices,
    # tight enough to bound a slip or an impatient run of presses across
    # three independent *arr instances.
    add_rate_limit_max: int = 10
    add_rate_limit_window_seconds: float = 3600.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            discord_large_release_threshold_mb=_float_from_env(
                "SHELFMARK_DISCORD_LARGE_RELEASE_THRESHOLD_MB", 5000.0
            ),
            radarr_url=_str_from_env("RADARR_URL"),
            radarr_api_key=_str_from_env("RADARR_API_KEY"),
            radarr_root_folder=_str_from_env("RADARR_ROOT_FOLDER"),
            radarr_quality_profile=_str_from_env("RADARR_QUALITY_PROFILE"),
            sonarr_url=_str_from_env("SONARR_URL"),
            sonarr_api_key=_str_from_env("SONARR_API_KEY"),
            sonarr_root_folder=_str_from_env("SONARR_ROOT_FOLDER"),
            sonarr_quality_profile=_str_from_env("SONARR_QUALITY_PROFILE"),
            sonarr_big_show_season_threshold=_int_from_env(
                "SONARR_BIG_SHOW_SEASON_THRESHOLD", 5
            ),
            sonarr_big_show_episode_threshold=_int_from_env(
                "SONARR_BIG_SHOW_EPISODE_THRESHOLD", 100
            ),
            lidarr_url=_str_from_env("LIDARR_URL"),
            lidarr_api_key=_str_from_env("LIDARR_API_KEY"),
            lidarr_root_folder=_str_from_env("LIDARR_ROOT_FOLDER"),
            lidarr_quality_profile=_str_from_env("LIDARR_QUALITY_PROFILE"),
            lidarr_metadata_profile=os.environ.get("LIDARR_METADATA_PROFILE", "Standard").strip()
            or "Standard",
            lidarr_album_poll_attempts=_int_from_env("LIDARR_ALBUM_POLL_ATTEMPTS", 5),
            lidarr_album_poll_seconds=_float_from_env("LIDARR_ALBUM_POLL_SECONDS", 2.0),
            arr_http_timeout=_float_from_env("DISCORDARR_ARR_HTTP_TIMEOUT_SECONDS", 15.0),
            arr_http_retries=_int_from_env("DISCORDARR_ARR_HTTP_RETRIES", 2, minimum=0),
            add_rate_limit_max=_int_from_env("DISCORDARR_ADD_RATE_LIMIT_MAX", 10),
            add_rate_limit_window_seconds=_float_from_env(
                "DISCORDARR_ADD_RATE_LIMIT_WINDOW_SECONDS", 3600.0
            ),
        )

    def radarr_configured(self) -> bool:
        return bool(self.radarr_url and self.radarr_api_key)

    def sonarr_configured(self) -> bool:
        return bool(self.sonarr_url and self.sonarr_api_key)

    def lidarr_configured(self) -> bool:
        return bool(self.lidarr_url and self.lidarr_api_key)

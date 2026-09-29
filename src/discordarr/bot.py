"""discordarr's entrypoint: one Discord bot, one login, one command set.

Composes the ported Shelfmark book commands (discord_bot.py) with the
Radarr/Sonarr/Lidarr commands (media_bot.py) onto a single `commands.Bot` --
there is only one Discord application and one bot token (see the README's
"why one bot" note), so there can only be one `.run()` call.

`discord_bot.build_bot()` returns a bot with the five book commands already
on its tree, plus the book half of `/request`. `media_bot.install_media_commands`
then adds `/request` itself (one command for books, movies, shows and music,
with that book half wired in) and `/queue` to that SAME tree before anything
syncs commands with Discord (`ShelfmarkBot.setup_hook`, inside `.run()`,
syncs whatever is on the tree at that point -- see discord_bot.py). Nothing
here duplicates discord_bot.py's own environment parsing or its `_idle`
crash-loop guard; it is reused wholesale.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

import discord
from discord.ext import commands

from . import discord_bot, heartbeat, media_bot
from .config import Settings

# `discord_bot.py` (ported from Shelfmark) reads these names directly. Setting
# a DISCORDARR_-prefixed value copies it into the canonical name when the
# canonical one is unset, so a fresh deployment can use either naming
# without discord_bot.py itself needing to know DISCORDARR_* exists -- see
# the "Environment compatibility is mandatory" requirement in the PR
# description: the OLD names must keep working unmodified, which is exactly
# what leaving discord_bot.py's own environment parsing untouched guarantees.
_ENV_ALIASES = {
    "DISCORD_BOT_TOKEN": "DISCORDARR_DISCORD_BOT_TOKEN",
    "SHELFMARK_API_URL": "DISCORDARR_API_URL",
    "SHELFMARK_API_TOKEN": "DISCORDARR_API_TOKEN",
    "SHELFMARK_DISCORD_GUILD_ID": "DISCORDARR_DISCORD_GUILD_ID",
    "SHELFMARK_DISCORD_ALLOWED_ROLE_IDS": "DISCORDARR_DISCORD_ALLOWED_ROLE_IDS",
    "SHELFMARK_DISCORD_MAX_ATTACHMENT_MB": "DISCORDARR_DISCORD_MAX_ATTACHMENT_MB",
    "SHELFMARK_DISCORD_LARGE_RELEASE_THRESHOLD_MB": "DISCORDARR_DISCORD_LARGE_RELEASE_THRESHOLD_MB",
}


def apply_env_aliases(environ: MutableMapping[str, str] = os.environ) -> None:
    """Copy each set DISCORDARR_* alias into its canonical name, when the
    canonical name is not already set. A plain dict can be passed in tests
    instead of mutating the real environment.
    """
    for canonical, alias in _ENV_ALIASES.items():
        if not (environ.get(canonical) or "").strip() and (environ.get(alias) or "").strip():
            environ[canonical] = environ[alias]


def build_bot() -> commands.Bot:
    """The one bot: the five book commands, `/request` for every media type,
    and `/queue`, on one tree."""
    bot, request_books = discord_bot.build_bot()
    settings = Settings.from_env()
    try:
        allowed_roles = discord_bot._int_set(os.environ.get("SHELFMARK_DISCORD_ALLOWED_ROLE_IDS"))
    except ValueError:
        # discord_bot.build_bot() above already printed the same parse
        # failure and fell back to "refuse everything" for the book
        # commands; the media ones must fail exactly as closed, not open,
        # just because this second parse is redundant.
        allowed_roles = set()
    media_bot.install_media_commands(bot, settings, allowed_roles, book_request=request_books)
    heartbeat.install(bot)
    return bot


def main() -> None:
    apply_env_aliases()
    problems = discord_bot.blocking_problems()
    if problems:
        discord_bot._idle(problems)
        return
    token = os.environ["DISCORD_BOT_TOKEN"]
    try:
        build_bot().run(token)
    except discord.LoginFailure:
        # A WRONG token, as distinct from a missing one. Retrying cannot fix
        # it, and under `restart: unless-stopped` an exit here would retry it
        # forever — against Discord's login endpoint, which is a good way to
        # get the application rate-limited.
        discord_bot._idle(["DISCORD_BOT_TOKEN was rejected by Discord — the token is wrong or was reset"])


if __name__ == "__main__":
    main()

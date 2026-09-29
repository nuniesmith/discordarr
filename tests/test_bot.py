from __future__ import annotations

import os
import unittest
from unittest import mock

from src.discordarr.bot import apply_env_aliases, build_bot


class EnvAliasTests(unittest.TestCase):
    """DISCORDARR_* aliases for the Shelfmark-era names -- see the PR
    description's "Environment compatibility is mandatory": the OLD names
    (DISCORD_BOT_TOKEN, SHELFMARK_*) must keep working unmodified, which is
    what discord_bot.py being an untouched port already guarantees. This is
    the OPTIONAL other direction: a fresh deployment may use DISCORDARR_*
    instead, and it gets copied into the canonical name before
    discord_bot.py ever reads it.
    """

    def test_an_alias_is_copied_into_the_canonical_name(self) -> None:
        environ = {"DISCORDARR_DISCORD_BOT_TOKEN": "abc123"}
        apply_env_aliases(environ)
        self.assertEqual(environ["DISCORD_BOT_TOKEN"], "abc123")

    def test_the_canonical_name_wins_when_both_are_set(self) -> None:
        """The old name is what a drop-in deployment already has set --
        an alias must never override it."""
        environ = {"DISCORD_BOT_TOKEN": "canonical", "DISCORDARR_DISCORD_BOT_TOKEN": "alias"}
        apply_env_aliases(environ)
        self.assertEqual(environ["DISCORD_BOT_TOKEN"], "canonical")

    def test_an_empty_canonical_value_still_counts_as_unset(self) -> None:
        """An unset GitHub/compose secret can write an empty string rather
        than an absent key -- discord_bot.py's own blocking_problems treats
        that as missing, and this must agree with it."""
        environ = {"DISCORD_BOT_TOKEN": "   ", "DISCORDARR_DISCORD_BOT_TOKEN": "alias"}
        apply_env_aliases(environ)
        self.assertEqual(environ["DISCORD_BOT_TOKEN"], "alias")

    def test_neither_set_leaves_neither_set(self) -> None:
        environ: dict[str, str] = {}
        apply_env_aliases(environ)
        self.assertNotIn("DISCORD_BOT_TOKEN", environ)

    def test_every_shelfmark_discord_variable_has_an_alias(self) -> None:
        canonical_names = [
            "DISCORD_BOT_TOKEN",
            "SHELFMARK_API_URL",
            "SHELFMARK_API_TOKEN",
            "SHELFMARK_DISCORD_GUILD_ID",
            "SHELFMARK_DISCORD_ALLOWED_ROLE_IDS",
            "SHELFMARK_DISCORD_MAX_ATTACHMENT_MB",
            "SHELFMARK_DISCORD_LARGE_RELEASE_THRESHOLD_MB",
        ]
        for name in canonical_names:
            with self.subTest(name=name):
                environ = {f"DISCORDARR_{name.removeprefix('SHELFMARK_')}": "value"}
                apply_env_aliases(environ)
                self.assertEqual(environ.get(name), "value", f"{name} has no working alias")



class ComposedBotTests(unittest.IsolatedAsyncioTestCase):
    """The tree the household actually gets: both halves composed by
    `build_bot`. Each half's own tests pin its part; this pins the whole,
    which is what Discord syncs and what a person sees when they type `/`."""

    _ENV = {
        "DISCORD_BOT_TOKEN": "not-a-real-token",
        "SHELFMARK_API_TOKEN": "not-a-real-token",
        "SHELFMARK_DISCORD_ALLOWED_ROLE_IDS": "1",
    }

    async def test_the_whole_command_set_is_exactly_this(self) -> None:
        """One /request for everything. /movie, /show and /music were
        folded into it; the picker says which kind."""
        with mock.patch.dict(os.environ, self._ENV), mock.patch("builtins.print"):
            bot = build_bot()
        names = {command.name for command in bot.tree.get_commands()}
        self.assertEqual(
            names, {"library", "request", "downloads", "job", "cancel", "scan", "queue"}
        )

    async def test_request_offers_every_media_type(self) -> None:
        with mock.patch.dict(os.environ, self._ENV), mock.patch("builtins.print"):
            bot = build_bot()
        [type_option, query_option] = bot.tree.get_command("request").to_dict(bot.tree)["options"]
        self.assertEqual(
            [choice["name"] for choice in type_option["choices"]],
            ["Audiobook", "Ebook", "Movie", "Show", "Music"],
        )
        self.assertTrue(type_option["required"])
        self.assertTrue(query_option["required"])


if __name__ == "__main__":
    unittest.main()

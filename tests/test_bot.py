from __future__ import annotations

import unittest

from src.discordarr.bot import apply_env_aliases


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


if __name__ == "__main__":
    unittest.main()

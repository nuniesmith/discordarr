from __future__ import annotations

import unittest
from unittest import mock

from src.discordarr.config import Settings


class DefaultsTests(unittest.TestCase):
    """`Settings()` with no environment read at all must be a valid, inert
    configuration -- the same way shelfmark's own `Settings()` is (see
    config.py's module docstring). discord_bot.py's `install_commands`
    relies on exactly this for its `large_release_threshold_bytes` default
    argument."""

    def test_no_service_is_configured_by_default(self) -> None:
        settings = Settings()
        self.assertFalse(settings.radarr_configured())
        self.assertFalse(settings.sonarr_configured())
        self.assertFalse(settings.lidarr_configured())

    def test_documented_defaults(self) -> None:
        settings = Settings()
        self.assertEqual(settings.discord_large_release_threshold_mb, 5000.0)
        self.assertEqual(settings.sonarr_big_show_season_threshold, 5)
        self.assertEqual(settings.sonarr_big_show_episode_threshold, 100)
        self.assertEqual(settings.lidarr_metadata_profile, "Standard")
        self.assertEqual(settings.add_rate_limit_max, 10)
        self.assertEqual(settings.add_rate_limit_window_seconds, 3600.0)


class FromEnvTests(unittest.TestCase):
    def test_a_service_with_both_url_and_key_is_configured(self) -> None:
        with mock.patch.dict(
            "os.environ", {"RADARR_URL": "http://radarr.invalid", "RADARR_API_KEY": "k"}, clear=True
        ):
            settings = Settings.from_env()
        self.assertTrue(settings.radarr_configured())
        self.assertFalse(settings.sonarr_configured())
        self.assertFalse(settings.lidarr_configured())

    def test_a_url_with_no_key_is_not_configured(self) -> None:
        """Each service is independent, and each needs BOTH halves -- a URL
        alone cannot authenticate, so it must not read as "configured"."""
        with mock.patch.dict("os.environ", {"SONARR_URL": "http://sonarr.invalid"}, clear=True):
            settings = Settings.from_env()
        self.assertFalse(settings.sonarr_configured())

    def test_services_are_independent(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"LIDARR_URL": "http://lidarr.invalid", "LIDARR_API_KEY": "k"},
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertTrue(settings.lidarr_configured())
        self.assertFalse(settings.radarr_configured())
        self.assertFalse(settings.sonarr_configured())

    def test_big_show_thresholds_are_configurable(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "SONARR_BIG_SHOW_SEASON_THRESHOLD": "3",
                "SONARR_BIG_SHOW_EPISODE_THRESHOLD": "50",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.sonarr_big_show_season_threshold, 3)
        self.assertEqual(settings.sonarr_big_show_episode_threshold, 50)

    def test_lidarr_metadata_profile_defaults_to_standard(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.lidarr_metadata_profile, "Standard")

    def test_lidarr_metadata_profile_is_overridable(self) -> None:
        with mock.patch.dict("os.environ", {"LIDARR_METADATA_PROFILE": "None"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.lidarr_metadata_profile, "None")

    def test_add_rate_limit_is_configurable(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"DISCORDARR_ADD_RATE_LIMIT_MAX": "3", "DISCORDARR_ADD_RATE_LIMIT_WINDOW_SECONDS": "60"},
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.add_rate_limit_max, 3)
        self.assertEqual(settings.add_rate_limit_window_seconds, 60.0)

    def test_root_folder_and_quality_profile_overrides_are_read_by_name(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "RADARR_ROOT_FOLDER": "/movies",
                "RADARR_QUALITY_PROFILE": "HD-1080p",
                "RADARR_URL": "http://radarr.invalid",
                "RADARR_API_KEY": "k",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.radarr_root_folder, "/movies")
        self.assertEqual(settings.radarr_quality_profile, "HD-1080p")

    def test_a_non_numeric_threshold_raises_rather_than_silently_falling_back(self) -> None:
        """Same "fail loudly, don't guess" rule discord_bot.py applies to
        SHELFMARK_DISCORD_MAX_ATTACHMENT_MB -- garbage input must not
        silently become a default with no sign that the environment was
        wrong."""
        with mock.patch.dict(
            "os.environ", {"SONARR_BIG_SHOW_SEASON_THRESHOLD": "not-a-number"}, clear=True
        ):
            with self.assertRaises(ValueError):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()

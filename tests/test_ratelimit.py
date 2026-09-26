from __future__ import annotations

import unittest

from src.discordarr.ratelimit import AddRateLimiter, rate_limit_message, wait_text


class AddRateLimiterTests(unittest.TestCase):
    """The budget every /movie, /show and /music Request press shares --
    see media_bot.py's `_add_movie`/`_add_show`/`_add_album`. A fake clock
    makes the window boundary exact instead of racing a real one.
    """

    def _limiter(self, max_requests: int, window_seconds: float) -> tuple[AddRateLimiter, dict]:
        clock = {"t": 0.0}
        limiter = AddRateLimiter(max_requests, window_seconds, clock=lambda: clock["t"])
        return limiter, clock

    def test_requests_under_the_budget_are_allowed(self) -> None:
        limiter, _clock = self._limiter(3, 3600.0)
        self.assertIsNone(limiter.try_consume("user-1"))
        self.assertIsNone(limiter.try_consume("user-1"))
        self.assertIsNone(limiter.try_consume("user-1"))

    def test_the_request_over_budget_is_blocked(self) -> None:
        """The default is 10/hour (see config.py); this pins the general
        rule with a small number so the test does not need ten calls."""
        limiter, _clock = self._limiter(3, 3600.0)
        for _ in range(3):
            self.assertIsNone(limiter.try_consume("user-1"))
        self.assertIsNotNone(limiter.try_consume("user-1"))

    def test_the_eleventh_add_is_blocked_at_the_documented_default(self) -> None:
        """Pins the exact default (10/hour) from config.py's
        `add_rate_limit_max`, not just "some" limit."""
        limiter, _clock = self._limiter(10, 3600.0)
        for _ in range(10):
            self.assertIsNone(limiter.try_consume("user-1"))
        self.assertIsNotNone(limiter.try_consume("user-1"))

    def test_a_blocked_press_is_not_itself_recorded(self) -> None:
        """A refused press must not consume a slot -- otherwise a person
        who keeps trying while blocked would never recover even once the
        window rolls forward."""
        limiter, clock = self._limiter(1, 100.0)
        self.assertIsNone(limiter.try_consume("user-1"))
        self.assertIsNotNone(limiter.try_consume("user-1"))
        self.assertIsNotNone(limiter.try_consume("user-1"))  # still blocked, still not recorded
        clock["t"] = 100.1  # window has now rolled past the first (only) hit
        self.assertIsNone(limiter.try_consume("user-1"))

    def test_each_user_has_their_own_budget(self) -> None:
        limiter, _clock = self._limiter(1, 3600.0)
        self.assertIsNone(limiter.try_consume("user-1"))
        self.assertIsNone(limiter.try_consume("user-2"))
        self.assertIsNotNone(limiter.try_consume("user-1"))

    def test_the_window_rolls_forward_rather_than_resetting_all_at_once(self) -> None:
        limiter, clock = self._limiter(1, 100.0)
        self.assertIsNone(limiter.try_consume("user-1"))
        clock["t"] = 50.0
        self.assertIsNotNone(limiter.try_consume("user-1"))
        clock["t"] = 100.1
        self.assertIsNone(limiter.try_consume("user-1"))

    def test_the_wait_time_counts_down_to_the_oldest_hit_aging_out(self) -> None:
        limiter, clock = self._limiter(1, 100.0)
        limiter.try_consume("user-1")
        clock["t"] = 40.0
        wait = limiter.try_consume("user-1")
        assert wait is not None
        self.assertAlmostEqual(wait, 60.0, places=3)


class WaitTextTests(unittest.TestCase):
    """Same phrasing discord_bot.py's `_rate_limit_wait_text` uses for the
    Shelfmark API's own 429s (see ratelimit.py's docstring) -- one consistent
    style regardless of which limiter caught the press."""

    def test_a_short_wait_is_reported_in_seconds(self) -> None:
        self.assertEqual(wait_text(42), "Try again in 42 seconds.")

    def test_a_single_second_is_not_pluralized(self) -> None:
        self.assertEqual(wait_text(1), "Try again in 1 second.")

    def test_a_minute_or_more_is_reported_in_minutes(self) -> None:
        self.assertEqual(wait_text(125), "Try again in 2 minutes.")

    def test_exactly_one_minute_is_not_pluralized(self) -> None:
        self.assertEqual(wait_text(60), "Try again in 1 minute.")

    def test_a_negative_wait_still_reads_sensibly(self) -> None:
        """Defensive: a clock/window edge case must not produce "Try again
        in -3 seconds", which would read as broken rather than reassuring."""
        self.assertEqual(wait_text(-5), "Try again in 1 second.")


class RateLimitMessageTests(unittest.TestCase):
    def test_names_the_kind_and_includes_the_wait_time(self) -> None:
        self.assertEqual(
            rate_limit_message("adds", 90),
            "You have run too many adds. Try again in 2 minutes.",
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import math
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src.discordarr import heartbeat


class ShouldBeatTests(unittest.TestCase):
    """Only a bot that is logged in AND hearing back from Discord beats."""

    def test_a_connected_bot_beats(self) -> None:
        self.assertTrue(heartbeat.should_beat(True, False, 0.08))

    def test_a_bot_not_ready_yet_does_not(self) -> None:
        self.assertFalse(heartbeat.should_beat(False, False, 0.08))

    def test_a_closed_client_does_not(self) -> None:
        self.assertFalse(heartbeat.should_beat(True, True, 0.08))

    def test_no_acknowledged_heartbeat_yet_does_not(self) -> None:
        self.assertFalse(heartbeat.should_beat(True, False, math.nan))

    def test_a_dropped_websocket_does_not(self) -> None:
        self.assertFalse(heartbeat.should_beat(True, False, math.inf))


class IsFreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "beat"

    def test_a_recent_beat_is_fresh(self) -> None:
        self.path.touch()
        self.assertTrue(heartbeat.is_fresh(self.path, time.time()))

    def test_a_beat_older_than_the_limit_is_stale(self) -> None:
        self.path.touch()
        old = time.time() - heartbeat.MAX_AGE_SECONDS - 5
        os.utime(self.path, (old, old))
        self.assertFalse(heartbeat.is_fresh(self.path, time.time()))

    def test_no_beat_at_all_is_not_fresh(self) -> None:
        self.assertFalse(heartbeat.is_fresh(self.path, time.time()))


class _FakeBot:
    def __init__(self, *, ready: bool, closed: bool, latency: float) -> None:
        self._ready, self._closed, self.latency = ready, closed, latency
        self.listeners: list[tuple[object, str]] = []

    def is_ready(self) -> bool:
        return self._ready

    def is_closed(self) -> bool:
        return self._closed

    def add_listener(self, fn: object, name: str) -> None:
        self.listeners.append((fn, name))


class InstallTests(unittest.IsolatedAsyncioTestCase):
    """The beat starts on `on_ready` and touches the file from the bot's own
    event loop -- the file appearing is the whole contract."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "beat"
        patch = mock.patch.object(heartbeat, "HEARTBEAT_FILE", self.path)
        patch.start()
        self.addCleanup(patch.stop)

    async def _run(self, bot: _FakeBot) -> None:
        beat = heartbeat.install(bot)
        [(start, name)] = bot.listeners
        self.assertEqual(name, "on_ready")
        await start()  # type: ignore[operator]
        await asyncio.sleep(0.05)
        beat.cancel()

    async def test_a_connected_bot_writes_the_heartbeat(self) -> None:
        await self._run(_FakeBot(ready=True, closed=False, latency=0.05))
        self.assertTrue(heartbeat.is_fresh(self.path, time.time()))

    async def test_a_disconnected_bot_writes_nothing(self) -> None:
        await self._run(_FakeBot(ready=True, closed=False, latency=math.inf))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()

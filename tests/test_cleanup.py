"""/cleanup: which titles count as unwatched, who may delete, and how a delete is sent."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from src.discordarr.arr_clients import RadarrClient, SonarrClient
from src.discordarr.cleanup import (
    Candidate,
    ConfirmDeleteView,
    can_delete,
    collect,
    find_unwatched,
    last_played_by_title,
    parse_time,
)
from src.discordarr.config import Settings

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


def movie(movie_id: int, path: str, added: str, size: int = 10_000_000_000) -> dict:
    return {"id": movie_id, "title": path.rsplit("/", 1)[-1], "year": 2020, "path": path,
            "sizeOnDisk": size, "added": added}


def play(path: str, when: str) -> dict:
    return {"Path": path, "UserData": {"LastPlayedDate": when}}


class UnwatchedTests(unittest.TestCase):
    def test_old_and_never_or_long_ago_watched_titles_qualify_biggest_first(self) -> None:
        items = [
            movie(1, "/movies/Never Watched (2019)", "2024-01-01T00:00:00Z", size=5_000_000_000),
            movie(2, "/movies/Watched Long Ago (2018)", "2023-01-01T00:00:00Z", size=9_000_000_000),
            movie(3, "/movies/Watched Last Month (2017)", "2023-01-01T00:00:00Z"),
            movie(4, "/movies/Added Last Month (2026)", "2026-09-17T00:00:00Z"),
            movie(5, "/movies/No File (2016)", "2020-01-01T00:00:00Z", size=0),
        ]
        plays = [
            play("/movies/Watched Long Ago (2018)/film.mkv", "2024-06-01T20:00:00.0000000Z"),
            play("/movies/Watched Last Month (2017)/film.mkv", "2026-09-20T20:00:00.0000000Z"),
        ]
        played = last_played_by_title([m["path"] for m in items], plays)
        found = find_unwatched("movie", items, played, NOW, 365)
        self.assertEqual([c.arr_id for c in found], [2, 1])
        self.assertIsNone(found[1].last_played)
        self.assertEqual(found[0].last_played, datetime(2024, 6, 1, 20, 0, tzinfo=timezone.utc))

    def test_one_episode_watched_keeps_the_whole_show(self) -> None:
        shows = [{"id": 7, "title": "Show", "path": "/shows/Show", "added": "2020-01-01T00:00:00Z",
                  "statistics": {"sizeOnDisk": 50_000_000_000}}]
        plays = [play("/shows/Show/Season 03/Show - S03E04.mkv", "2026-08-01T00:00:00Z")]
        played = last_played_by_title(["/shows/Show"], plays)
        self.assertEqual(find_unwatched("show", shows, played, NOW, 365), [])

    def test_a_similarly_named_folder_is_not_mistaken_for_the_title(self) -> None:
        played = last_played_by_title(
            ["/movies/Alien (1979)"], [play("/movies/Alien (1979) Directors Cut/f.mkv", "2026-10-01T00:00:00Z")]
        )
        self.assertEqual(played, {})

    def test_emby_times_with_seven_fractional_digits_parse(self) -> None:
        self.assertEqual(parse_time("2024-06-01T20:11:00.1234567Z"),
                         datetime(2024, 6, 1, 20, 11, tzinfo=timezone.utc))
        self.assertIsNone(parse_time("not a date"))


class CollectTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_watch_history_at_all_lists_nothing(self) -> None:
        """A broken path match looks exactly like "nobody watched anything",
        and that list must never be offered for deletion."""
        emby = mock.Mock()
        emby.users.return_value = [{"Id": "u1"}]
        emby.plays.return_value = []
        arr = mock.Mock()
        arr.movies.return_value = [movie(1, "/movies/A (2000)", "2020-01-01T00:00:00Z")]
        result = await collect("movie", emby, arr, Settings(), now=NOW)
        self.assertIsInstance(result, str)
        self.assertIn("no watch history", result)

    async def test_every_users_plays_count(self) -> None:
        emby = mock.Mock()
        emby.users.return_value = [{"Id": "u1"}, {"Id": "u2"}]
        emby.plays.side_effect = lambda user, kind: (
            [play("/movies/A (2000)/a.mkv", "2026-10-01T00:00:00Z")] if user == "u2" else []
        )
        arr = mock.Mock()
        arr.movies.return_value = [movie(1, "/movies/A (2000)", "2020-01-01T00:00:00Z"),
                                   movie(2, "/movies/B (2001)", "2020-01-01T00:00:00Z")]
        candidates, plays = await collect("movie", emby, arr, Settings(), now=NOW)
        self.assertEqual([c.arr_id for c in candidates], [2])
        self.assertEqual(plays, 1)


class PermissionTests(unittest.TestCase):
    def test_nobody_deletes_until_someone_is_listed(self) -> None:
        self.assertFalse(can_delete(42, Settings()))
        self.assertTrue(can_delete(42, Settings(purge_user_ids=frozenset({42}))))
        self.assertFalse(can_delete(7, Settings(purge_user_ids=frozenset({42}))))


class DeleteRequestTests(unittest.TestCase):
    def test_radarr_deletes_files_and_excludes_the_movie(self) -> None:
        client = RadarrClient("http://radarr", "k")
        with mock.patch.object(client.http, "request") as request:
            client.delete_movie(12)
        request.assert_called_once_with(
            "/api/v3/movie/12", method="DELETE", params={"deleteFiles": "true", "addImportExclusion": "true"}
        )

    def test_sonarr_deletes_files_and_excludes_the_series(self) -> None:
        client = SonarrClient("http://sonarr", "k")
        with mock.patch.object(client.http, "request") as request:
            client.delete_series(3)
        request.assert_called_once_with(
            "/api/v3/series/3", method="DELETE", params={"deleteFiles": "true", "addImportListExclusion": "true"}
        )


class ConfirmTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, user_id: int) -> mock.Mock:
        interaction = mock.Mock()
        interaction.user.id = user_id
        interaction.response.defer = mock.AsyncMock()
        interaction.response.edit_message = mock.AsyncMock()
        interaction.followup.send = mock.AsyncMock()
        return interaction

    def view(self, settings: Settings, arr: mock.Mock) -> ConfirmDeleteView:
        candidate = Candidate("movie", 12, "Big Film", 2001, 40_000_000_000, NOW, None)
        return ConfirmDeleteView(candidate, arr, settings, mock.AsyncMock(return_value=True))

    async def test_the_confirm_press_rechecks_the_purge_list(self) -> None:
        arr = mock.Mock()
        view = self.view(Settings(), arr)
        await view.confirm.callback(self.interaction(42))
        arr.delete_movie.assert_not_called()

    async def test_a_confirmed_delete_reaches_radarr_once(self) -> None:
        arr = mock.Mock()
        view = self.view(Settings(purge_user_ids=frozenset({42})), arr)
        interaction = self.interaction(42)
        await view.confirm.callback(interaction)
        await view.confirm.callback(interaction)
        arr.delete_movie.assert_called_once_with(12)
        self.assertIn("freeing 40.0 GB", interaction.followup.send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

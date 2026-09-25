from __future__ import annotations

import functools
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from discord.ext import commands

from src.discordarr.clients import ServiceError
from src.discordarr.config import Settings
from src.discordarr.media_bot import (
    _ConfirmAddShowView,
    _RequestView,
    _ServiceSetup,
    _add_album,
    _add_movie,
    _add_show,
    _album_artist_id,
    _album_artist_name,
    _album_label,
    _album_state,
    _build_guard,
    _find_local_album,
    _index_queue,
    _is_big_show,
    _movie_label,
    _movie_state,
    _queue_progress_text,
    _queue_records,
    _render_queue,
    _request_show,
    _resolve_metadata_profile,
    _resolve_quality_profile,
    _resolve_root_folder,
    _show_label,
    _show_state,
    install_media_commands,
)
from src.discordarr.ratelimit import AddRateLimiter


# ---------------------------------------------------------------------------
# Fakes -- same style as discord_bot.py's own test file's _FakeInteraction/
# _FakeResponse/_FakeFollowup/_FakeEbookApi: plain classes standing in for
# Discord and the *arr clients, not a mocking framework, so a test reads as
# "what happened" rather than "what was configured to happen".
# ---------------------------------------------------------------------------


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple[str | None, dict]] = []

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        self.sent.append((content, kwargs))


class _FakeResponse:
    def __init__(self) -> None:
        self._done = False
        self.messages: list[tuple[str | None, dict]] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **_kwargs: object) -> None:
        self.deferred = True
        self._done = True

    async def send_message(self, content: str | None = None, **kwargs: object) -> None:
        self.messages.append((content, kwargs))
        self._done = True

    async def edit_message(self, **kwargs: object) -> None:
        self.messages.append((kwargs.get("content"), kwargs))
        self._done = True


def _member(role_ids: tuple[int, ...] = (1,), user_id: int = 42) -> mock.Mock:
    """A `discord.Member` stand-in that actually passes
    `isinstance(x, discord.Member)` -- `_build_guard`'s (and discord_bot.py's
    own) guard checks that before looking at roles at all, so a plain fake
    class would silently fail every permitted-user test for the wrong
    reason. `Mock(spec=discord.Member)` is real `unittest.mock` behaviour
    (documented under "Autospeccing"): a spec'd mock's `__class__` is the
    spec'd class, which is what `isinstance` actually checks.
    """
    return mock.Mock(spec=discord.Member, id=user_id, roles=[SimpleNamespace(id=rid) for rid in role_ids])


class _FakeInteraction:
    def __init__(self, *, role_ids: tuple[int, ...] = (1,), user_id: int = 42) -> None:
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.user = _member(role_ids, user_id)
        self.guild_id = 1
        self.channel_id = 2


async def _allow(_interaction: discord.Interaction) -> bool:
    return True


async def _deny(_interaction: discord.Interaction) -> bool:
    return False


class _FakeRadarr:
    def __init__(
        self,
        *,
        lookup_result: list[dict] | None = None,
        add_result: dict | None = None,
        add_error: Exception | None = None,
        queue_result: dict | None = None,
        root_folders: list[dict] | None = None,
        quality_profiles: list[dict] | None = None,
        movies: list[dict] | None = None,
        lookup_error: Exception | None = None,
    ) -> None:
        self._lookup_result = lookup_result if lookup_result is not None else []
        self._lookup_error = lookup_error
        self._add_result = add_result if add_result is not None else {"id": 999}
        self._add_error = add_error
        self._queue_result = queue_result if queue_result is not None else {"records": []}
        self._root_folders = root_folders if root_folders is not None else [{"path": "/movies", "id": 1}]
        self._quality_profiles = (
            quality_profiles if quality_profiles is not None else [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}]
        )
        self._movies = movies if movies is not None else []
        self.add_calls: list[dict] = []

    def lookup(self, term: str) -> list[dict]:
        if self._lookup_error is not None:
            raise self._lookup_error
        return self._lookup_result

    def root_folders(self) -> list[dict]:
        return self._root_folders

    def quality_profiles(self) -> list[dict]:
        return self._quality_profiles

    def movies(self) -> list[dict]:
        return self._movies

    def queue(self) -> dict:
        return self._queue_result

    def add_movie(self, payload: dict) -> dict:
        self.add_calls.append(payload)
        if self._add_error is not None:
            raise self._add_error
        return self._add_result


class _FakeSonarr:
    def __init__(
        self,
        *,
        add_result: dict | None = None,
        add_error: Exception | None = None,
        root_folders: list[dict] | None = None,
        quality_profiles: list[dict] | None = None,
        series: list[dict] | None = None,
    ) -> None:
        self._add_result = add_result if add_result is not None else {"id": 5}
        self._add_error = add_error
        self._root_folders = root_folders if root_folders is not None else [{"path": "/shows", "id": 1}]
        self._quality_profiles = quality_profiles if quality_profiles is not None else [{"id": 1, "name": "Any"}]
        self._series = series if series is not None else []
        self.add_calls: list[dict] = []

    def root_folders(self) -> list[dict]:
        return self._root_folders

    def quality_profiles(self) -> list[dict]:
        return self._quality_profiles

    def series(self) -> list[dict]:
        return self._series

    def add_series(self, payload: dict) -> dict:
        self.add_calls.append(payload)
        if self._add_error is not None:
            raise self._add_error
        return self._add_result


class _FakeLidarr:
    def __init__(
        self,
        *,
        root_folders: list[dict] | None = None,
        quality_profiles: list[dict] | None = None,
        metadata_profiles: list[dict] | None = None,
        artists: list[dict] | None = None,
        add_artist_result: dict | None = None,
        add_artist_error: Exception | None = None,
        albums_by_artist: dict[int, list[dict]] | None = None,
    ) -> None:
        self._root_folders = root_folders if root_folders is not None else [{"path": "/music", "id": 1}]
        self._quality_profiles = quality_profiles if quality_profiles is not None else [{"id": 3, "name": "Standard"}]
        self._metadata_profiles = metadata_profiles if metadata_profiles is not None else [{"id": 1, "name": "Standard"}]
        self._artists = artists if artists is not None else []
        self._add_artist_result = add_artist_result if add_artist_result is not None else {"id": 900}
        self._add_artist_error = add_artist_error
        self._albums_by_artist = albums_by_artist or {}
        self.add_artist_calls: list[dict] = []
        self.monitor_calls: list[tuple[int, bool]] = []
        self.search_calls: list[int] = []

    def root_folders(self) -> list[dict]:
        return self._root_folders

    def quality_profiles(self) -> list[dict]:
        return self._quality_profiles

    def metadata_profiles(self) -> list[dict]:
        return self._metadata_profiles

    def artists(self) -> list[dict]:
        return self._artists

    def add_artist(self, payload: dict) -> dict:
        self.add_artist_calls.append(payload)
        if self._add_artist_error is not None:
            raise self._add_artist_error
        return self._add_artist_result

    def albums(self, artist_id: int) -> list[dict]:
        return self._albums_by_artist.get(artist_id, [])

    def set_album_monitored(self, album_id: int, monitored: bool) -> dict:
        self.monitor_calls.append((album_id, monitored))
        return {}

    def search_album(self, album_id: int) -> dict:
        self.search_calls.append(album_id)
        return {}


class AddRateLimiterStub:
    """Always allows -- for tests where the rate limit itself is not what's
    being tested (see test_ratelimit.py and AddMovieTests' own dedicated
    limiter test below for that)."""

    def try_consume(self, _key: str) -> None:
        return None


def _setup(root_folder: str = "/x", quality_profile_id: int = 1, metadata_profile_id: int | None = None) -> _ServiceSetup:
    """A `_ServiceSetup` that is already resolved -- for tests that only
    care about the add flow, not the resolution logic (see
    ServiceSetupTests/ResolveRootFolderTests/ResolveQualityProfileTests for
    that)."""

    async def _resolved(_setup: _ServiceSetup) -> None:
        return None

    setup = _ServiceSetup(_resolved)
    setup.root_folder = root_folder
    setup.quality_profile_id = quality_profile_id
    setup.metadata_profile_id = metadata_profile_id
    setup._done = True
    return setup


# ---------------------------------------------------------------------------
# Radarr state/label
# ---------------------------------------------------------------------------


class MovieStateTests(unittest.TestCase):
    """Radarr's `movie/lookup` has NO `hasFile` field (verified read-only,
    2026-09-25) -- `id` is absent until added, and `movieFileId`/`movieFile`
    are what say whether a file has landed. See arr_clients.py."""

    def test_not_in_library_has_no_id(self) -> None:
        self.assertEqual(_movie_state({"title": "New Movie"}), "none")

    def test_in_library_with_a_file_is_have(self) -> None:
        self.assertEqual(_movie_state({"id": 131, "movieFileId": 244}), "have")

    def test_in_library_with_no_file_yet_is_requested(self) -> None:
        self.assertEqual(_movie_state({"id": 186, "movieFileId": 0}), "requested")

    def test_a_present_movie_file_object_also_counts_as_have(self) -> None:
        """Defensive: `movieFileId` and `movieFile` are checked with `or`,
        since either one being truthy is sufficient."""
        self.assertEqual(_movie_state({"id": 131, "movieFileId": 0, "movieFile": {"id": 5}}), "have")


class MovieLabelTests(unittest.TestCase):
    def test_have_gets_a_checkmark(self) -> None:
        label = _movie_label({"id": 1, "title": "Dune", "year": 2021, "movieFileId": 5}, {})
        self.assertTrue(label.startswith("✅"))
        self.assertIn("Dune", label)
        self.assertIn("2021", label)

    def test_not_requested_gets_a_plus(self) -> None:
        label = _movie_label({"title": "Dune"}, {})
        self.assertTrue(label.startswith("➕"))

    def test_requested_gets_an_hourglass_and_queue_progress_when_available(self) -> None:
        queue_by_id = {7: {"size": 1000, "sizeleft": 250, "timeleft": "00:10:00"}}
        label = _movie_label({"id": 7, "title": "Dune", "movieFileId": 0}, queue_by_id)
        self.assertTrue(label.startswith("⏳"))
        self.assertIn("75%", label)
        self.assertIn("00:10:00 left", label)

    def test_requested_with_no_queue_entry_has_no_progress_suffix(self) -> None:
        label = _movie_label({"id": 7, "title": "Dune", "movieFileId": 0}, {})
        self.assertEqual(label, "⏳ Dune")


# ---------------------------------------------------------------------------
# Sonarr state/label/big-show gate
# ---------------------------------------------------------------------------


class ShowStateTests(unittest.TestCase):
    def test_not_in_library_has_no_id(self) -> None:
        self.assertEqual(_show_state({"title": "New Show"}), "none")

    def test_in_library_with_a_downloaded_episode_is_have(self) -> None:
        self.assertEqual(_show_state({"id": 90, "statistics": {"episodeFileCount": 12}}), "have")

    def test_in_library_with_no_downloaded_episodes_is_requested(self) -> None:
        self.assertEqual(_show_state({"id": 90, "statistics": {"episodeFileCount": 0}}), "requested")

    def test_missing_statistics_is_requested_not_have(self) -> None:
        self.assertEqual(_show_state({"id": 90}), "requested")


class ShowLabelTests(unittest.TestCase):
    def test_have_gets_a_checkmark(self) -> None:
        label = _show_label({"id": 1, "title": "The Office", "statistics": {"episodeFileCount": 5}}, {})
        self.assertTrue(label.startswith("✅"))


class IsBigShowTests(unittest.TestCase):
    """Mirrors discord_bot.py's `_needs_confirmation`: an unusable season
    count is the RISKY case, not the exempt one. Verified read-only against
    a real Sonarr (2026-09-25): `statistics.totalEpisodeCount` came back 0
    for five real 15-38-season shows -- only `seasonCount` is populated --
    so the episode branch below is exercised with an INVENTED value; it
    cannot be demonstrated against real data today.
    """

    def test_a_small_show_needs_no_confirmation(self) -> None:
        self.assertFalse(_is_big_show({"statistics": {"seasonCount": 3}}, 5, 100))

    def test_more_than_the_season_threshold_needs_confirmation(self) -> None:
        # NCIS, verified read-only: seasonCount 24 (real).
        self.assertTrue(_is_big_show({"statistics": {"seasonCount": 24}}, 5, 100))

    def test_exactly_at_the_season_threshold_needs_no_confirmation(self) -> None:
        """"more than 5 seasons" -- exactly 5 must not trip it."""
        self.assertFalse(_is_big_show({"statistics": {"seasonCount": 5}}, 5, 100))

    def test_more_than_the_episode_threshold_needs_confirmation(self) -> None:
        """Sonarr's lookup never actually populates this today (see class
        docstring) -- exercised with an invented value so the branch is not
        dead code, and starts working the day Sonarr changes."""
        self.assertTrue(_is_big_show({"statistics": {"seasonCount": 2, "totalEpisodeCount": 250}}, 5, 100))

    def test_missing_statistics_needs_confirmation(self) -> None:
        self.assertTrue(_is_big_show({}, 5, 100))

    def test_a_non_numeric_season_count_needs_confirmation(self) -> None:
        self.assertTrue(_is_big_show({"statistics": {"seasonCount": None}}, 5, 100))

    def test_thresholds_are_configurable(self) -> None:
        self.assertTrue(_is_big_show({"statistics": {"seasonCount": 3}}, 2, 100))
        self.assertFalse(_is_big_show({"statistics": {"seasonCount": 3}}, 5, 100))


# ---------------------------------------------------------------------------
# Lidarr state/label
# ---------------------------------------------------------------------------


class AlbumStateAndLabelTests(unittest.TestCase):
    """The album/lookup resource has no per-track file-count field (verified
    read-only, 2026-09-25) -- only `monitored` is available, so there is no
    "have" state here the way /movie and /show have one."""

    def test_monitored_is_requested(self) -> None:
        self.assertEqual(_album_state({"monitored": True}), "requested")

    def test_unmonitored_is_none(self) -> None:
        self.assertEqual(_album_state({"monitored": False}), "none")

    def test_label_includes_title_artist_and_year(self) -> None:
        label = _album_label(
            {"title": "Kind of Blue", "releaseDate": "1959-08-17", "artist": {"artistName": "Miles Davis"}}
        )
        self.assertIn("Kind of Blue", label)
        self.assertIn("Miles Davis", label)
        self.assertIn("1959", label)

    def test_label_with_no_artist_says_so_rather_than_crashing(self) -> None:
        label = _album_label({"title": "Untitled Album"})
        self.assertIn("unknown artist", label)


class AlbumArtistIdTests(unittest.TestCase):
    """The real Lidarr album/lookup response carries the artist id at the
    top level (`artistId`) -- verified read-only 2026-09-25 against a real
    household artist. The embedded `artist.id` is checked too, defensively,
    since it is the same value in that response."""

    def test_top_level_artist_id_is_used(self) -> None:
        self.assertEqual(_album_artist_id({"artistId": 402}), 402)

    def test_falls_back_to_the_embedded_artist_id(self) -> None:
        self.assertEqual(_album_artist_id({"artistId": 0, "artist": {"id": 402}}), 402)

    def test_no_artist_id_anywhere_is_none(self) -> None:
        self.assertIsNone(_album_artist_id({"artistId": 0, "artist": {"id": 0}}))
        self.assertIsNone(_album_artist_id({}))

    def test_artist_name_defaults_when_missing(self) -> None:
        self.assertEqual(_album_artist_name({}), "unknown artist")


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------


class QueueHelpersTests(unittest.TestCase):
    def test_queue_records_reads_the_records_key(self) -> None:
        self.assertEqual(_queue_records({"records": [{"id": 1}]}), [{"id": 1}])

    def test_queue_records_on_junk_payload_is_empty(self) -> None:
        self.assertEqual(_queue_records(None), [])
        self.assertEqual(_queue_records({"records": "not-a-list"}), [])

    def test_index_queue_keys_by_the_given_id_field(self) -> None:
        records = [{"movieId": 7, "status": "downloading"}, {"movieId": 9, "status": "queued"}]
        indexed = _index_queue(records, "movieId")
        self.assertEqual(indexed[7]["status"], "downloading")
        self.assertEqual(indexed[9]["status"], "queued")

    def test_progress_text_combines_percent_and_timeleft(self) -> None:
        text = _queue_progress_text({"size": 1000, "sizeleft": 400, "timeleft": "00:05:00"})
        self.assertEqual(text, "60% · 00:05:00 left")

    def test_progress_text_is_none_for_junk(self) -> None:
        self.assertIsNone(_queue_progress_text(None))
        self.assertIsNone(_queue_progress_text({}))

    def test_progress_text_handles_missing_timeleft(self) -> None:
        self.assertEqual(_queue_progress_text({"size": 100, "sizeleft": 50}), "50%")


# ---------------------------------------------------------------------------
# Root folder / quality profile / metadata profile resolution
# ---------------------------------------------------------------------------


class ResolveRootFolderTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_single_root_folder_is_used_automatically(self) -> None:
        client = _FakeRadarr(root_folders=[{"path": "/movies", "id": 1}])
        path, error = await _resolve_root_folder(client, None, "Radarr")
        self.assertEqual(path, "/movies")
        self.assertIsNone(error)

    async def test_more_than_one_root_folder_and_none_chosen_is_refused(self) -> None:
        client = _FakeRadarr(root_folders=[{"path": "/movies", "id": 1}, {"path": "/movies-4k", "id": 2}])
        path, error = await _resolve_root_folder(client, None, "Radarr")
        self.assertIsNone(path)
        assert error is not None
        self.assertIn("more than one root folder", error)

    async def test_a_configured_root_folder_that_matches_is_used(self) -> None:
        client = _FakeRadarr(root_folders=[{"path": "/movies", "id": 1}, {"path": "/movies-4k", "id": 2}])
        path, error = await _resolve_root_folder(client, "/movies-4k", "Radarr")
        self.assertEqual(path, "/movies-4k")
        self.assertIsNone(error)

    async def test_a_configured_root_folder_that_does_not_match_is_refused(self) -> None:
        client = _FakeRadarr(root_folders=[{"path": "/movies", "id": 1}])
        path, error = await _resolve_root_folder(client, "/nope", "Radarr")
        self.assertIsNone(path)
        assert error is not None
        self.assertIn("/nope", error)

    async def test_no_root_folders_at_all_is_refused(self) -> None:
        client = _FakeRadarr(root_folders=[])
        path, error = await _resolve_root_folder(client, None, "Radarr")
        self.assertIsNone(path)
        assert error is not None
        self.assertIn("no root folder", error)

    async def test_an_unreachable_service_is_reported_not_raised(self) -> None:
        client = _FakeRadarr()
        client.root_folders = mock.Mock(side_effect=ServiceError("radarr", "connection refused"))
        path, error = await _resolve_root_folder(client, None, "Radarr")
        self.assertIsNone(path)
        assert error is not None
        self.assertIn("could not be reached", error)


class ResolveQualityProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_most_used_profile_among_library_items_wins(self) -> None:
        client = _FakeRadarr(
            quality_profiles=[{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}],
            movies=[{"qualityProfileId": 4}, {"qualityProfileId": 4}, {"qualityProfileId": 1}],
        )
        profile_id, error = await _resolve_quality_profile(client, None, "Radarr", library_fetch=client.movies)
        self.assertEqual(profile_id, 4)
        self.assertIsNone(error)

    async def test_an_empty_library_falls_back_to_the_first_profile(self) -> None:
        client = _FakeRadarr(quality_profiles=[{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}], movies=[])
        profile_id, error = await _resolve_quality_profile(client, None, "Radarr", library_fetch=client.movies)
        self.assertEqual(profile_id, 1)
        self.assertIsNone(error)

    async def test_a_configured_profile_is_matched_by_name_case_insensitively(self) -> None:
        client = _FakeRadarr(quality_profiles=[{"id": 4, "name": "HD-1080p"}])
        profile_id, error = await _resolve_quality_profile(client, "hd-1080p", "Radarr", library_fetch=None)
        self.assertEqual(profile_id, 4)
        self.assertIsNone(error)

    async def test_a_configured_profile_that_does_not_exist_is_refused(self) -> None:
        client = _FakeRadarr(quality_profiles=[{"id": 1, "name": "Any"}])
        profile_id, error = await _resolve_quality_profile(client, "Nonexistent", "Radarr", library_fetch=None)
        self.assertIsNone(profile_id)
        assert error is not None
        self.assertIn("Nonexistent", error)

    async def test_no_quality_profiles_at_all_is_refused(self) -> None:
        client = _FakeRadarr(quality_profiles=[])
        profile_id, error = await _resolve_quality_profile(client, None, "Radarr", library_fetch=None)
        self.assertIsNone(profile_id)
        self.assertIsNotNone(error)


class ResolveMetadataProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_configured_name_is_matched(self) -> None:
        client = _FakeLidarr(metadata_profiles=[{"id": 1, "name": "Standard"}, {"id": 2, "name": "None"}])
        profile_id, error = await _resolve_metadata_profile(client, "Standard")
        self.assertEqual(profile_id, 1)
        self.assertIsNone(error)

    async def test_an_unknown_name_is_refused_with_the_available_names(self) -> None:
        client = _FakeLidarr(metadata_profiles=[{"id": 1, "name": "Standard"}, {"id": 2, "name": "None"}])
        profile_id, error = await _resolve_metadata_profile(client, "Nonexistent")
        self.assertIsNone(profile_id)
        assert error is not None
        self.assertIn("Standard", error)
        self.assertIn("None", error)


class ServiceSetupTests(unittest.IsolatedAsyncioTestCase):
    """"Fetch and cache these at startup or on first use" -- see the PR
    description. Resolved at most once on success; a failure is retried
    rather than wedging the service for the rest of the process."""

    async def test_resolve_runs_only_once_across_repeated_calls(self) -> None:
        calls = 0

        async def resolve(setup: _ServiceSetup) -> None:
            nonlocal calls
            calls += 1
            setup.root_folder = "/x"
            setup.quality_profile_id = 1
            return None

        setup = _ServiceSetup(resolve)
        self.assertIsNone(await setup.ensure())
        self.assertIsNone(await setup.ensure())
        self.assertIsNone(await setup.ensure())
        self.assertEqual(calls, 1)

    async def test_a_failure_is_not_cached_and_is_retried(self) -> None:
        calls = 0

        async def resolve(setup: _ServiceSetup) -> str | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                return "temporarily unreachable"
            setup.root_folder = "/x"
            setup.quality_profile_id = 1
            return None

        setup = _ServiceSetup(resolve)
        self.assertEqual(await setup.ensure(), "temporarily unreachable")
        self.assertIsNone(await setup.ensure())
        self.assertEqual(calls, 2)


# ---------------------------------------------------------------------------
# /movie add flow
# ---------------------------------------------------------------------------


class AddMovieTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_new_movie_is_requested(self) -> None:
        """Positive-capability first: the thing works before anything about
        it refuses."""
        client = _FakeRadarr()
        limiter = AddRateLimiterStub()
        interaction = _FakeInteraction()
        await _add_movie(
            interaction, {"title": "Dune", "year": 2021, "tmdbId": 1}, client=client, setup=_setup(quality_profile_id=4), limiter=limiter
        )
        [(content, kwargs)] = interaction.followup.sent
        self.assertIn("Requested", content)
        self.assertIn("Dune", content)
        self.assertTrue(kwargs.get("ephemeral"))
        [payload] = client.add_calls
        self.assertEqual(payload["title"], "Dune")
        self.assertEqual(payload["qualityProfileId"], 4)
        self.assertEqual(payload["rootFolderPath"], "/x")
        self.assertTrue(payload["monitored"])
        self.assertEqual(payload["minimumAvailability"], "released")
        self.assertEqual(payload["addOptions"], {"searchForMovie": True})

    async def test_an_already_in_library_movie_adds_nothing(self) -> None:
        """Mutation-check candidate from the PR description: break this,
        confirm this exact test fails, restore it."""
        client = _FakeRadarr()
        interaction = _FakeInteraction()
        await _add_movie(
            interaction, {"id": 131, "title": "Toy Story", "movieFileId": 244}, client=client, setup=_setup(), limiter=AddRateLimiterStub()
        )
        self.assertEqual(client.add_calls, [], "an already-owned movie must not be re-added")
        [(content, _kwargs)] = interaction.followup.sent
        self.assertIn("already in the library", content)

    async def test_radarr_erroring_on_add_is_reported_plainly(self) -> None:
        client = _FakeRadarr(add_error=ServiceError("radarr", "boom"))
        interaction = _FakeInteraction()
        await _add_movie(interaction, {"title": "Dune"}, client=client, setup=_setup(), limiter=AddRateLimiterStub())
        [(content, _kwargs)] = interaction.followup.sent
        self.assertIn("could not add", content)
        self.assertNotIn("Traceback", content)

    async def test_the_eleventh_add_in_the_window_is_blocked(self) -> None:
        """Same mutation-check candidate at the command layer, not just the
        limiter's own unit tests (test_ratelimit.py) -- proves the limiter
        is actually wired into the add path."""
        client = _FakeRadarr()
        limiter = AddRateLimiter(10, 3600.0)
        for _ in range(10):
            interaction = _FakeInteraction()
            await _add_movie(interaction, {"title": "Dune"}, client=client, setup=_setup(), limiter=limiter)
        self.assertEqual(len(client.add_calls), 10)
        eleventh = _FakeInteraction()
        await _add_movie(eleventh, {"title": "Dune"}, client=client, setup=_setup(), limiter=limiter)
        self.assertEqual(len(client.add_calls), 10, "the 11th press must not reach Radarr at all")
        [(content, _kwargs)] = eleventh.followup.sent
        self.assertIn("too many adds", content)

    async def test_a_setup_resolution_error_is_reported_not_raised(self) -> None:
        async def _fails(_setup: _ServiceSetup) -> str:
            return "it has more than one root folder"

        setup = _ServiceSetup(_fails)
        interaction = _FakeInteraction()
        await _add_movie(interaction, {"title": "Dune"}, client=_FakeRadarr(), setup=setup, limiter=AddRateLimiterStub())
        [(content, _kwargs)] = interaction.followup.sent
        self.assertIn("isn't fully set up here", content)
        self.assertIn("more than one root folder", content)


# ---------------------------------------------------------------------------
# /show add flow + confirmation
# ---------------------------------------------------------------------------


class AddShowTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_new_small_show_is_requested_directly(self) -> None:
        client = _FakeSonarr()
        interaction = _FakeInteraction()
        await _add_show(
            interaction, {"title": "A New Show", "statistics": {"seasonCount": 2}}, client=client, setup=_setup(quality_profile_id=1), limiter=AddRateLimiterStub()
        )
        [(content, _kwargs)] = interaction.followup.sent
        self.assertIn("Requested", content)
        [payload] = client.add_calls
        self.assertTrue(payload["seasonFolder"])
        self.assertEqual(payload["addOptions"], {"monitor": "all", "searchForMissingEpisodes": True})
        self.assertNotIn("languageProfileId", payload, "Sonarr v4 has no language profiles")

    async def test_an_already_in_library_show_adds_nothing(self) -> None:
        client = _FakeSonarr()
        interaction = _FakeInteraction()
        await _add_show(
            interaction, {"id": 90, "title": "The Office", "statistics": {"episodeFileCount": 5}}, client=client, setup=_setup(), limiter=AddRateLimiterStub()
        )
        self.assertEqual(client.add_calls, [])

    async def test_a_big_show_shows_a_confirmation_instead_of_adding(self) -> None:
        """Mutation-check candidate from the PR description."""
        client = _FakeSonarr()
        interaction = _FakeInteraction()
        await _request_show(
            interaction,
            {"title": "NCIS", "statistics": {"seasonCount": 24}},
            client=client,
            setup=_setup(),
            limiter=AddRateLimiterStub(),
            season_threshold=5,
            episode_threshold=100,
            guard=_allow,
        )
        self.assertEqual(client.add_calls, [], "a big show must not be added on the first press")
        [(content, kwargs)] = interaction.response.messages
        self.assertIn("24 season", content)
        self.assertIn("view", kwargs)

    async def test_a_small_show_is_added_directly_with_no_confirmation(self) -> None:
        client = _FakeSonarr()
        interaction = _FakeInteraction()
        await _request_show(
            interaction,
            {"title": "A Small Show", "statistics": {"seasonCount": 2}},
            client=client,
            setup=_setup(),
            limiter=AddRateLimiterStub(),
            season_threshold=5,
            episode_threshold=100,
            guard=_allow,
        )
        self.assertEqual(len(client.add_calls), 1)

    async def test_confirming_adds_the_show(self) -> None:
        client = _FakeSonarr()
        show = {"title": "NCIS", "statistics": {"seasonCount": 24}}
        add_fn = functools.partial(_add_show, client=client, setup=_setup(), limiter=AddRateLimiterStub())
        view = _ConfirmAddShowView(show, _allow, add_fn)
        interaction = _FakeInteraction()
        await view.confirm.callback(interaction)
        self.assertEqual(len(client.add_calls), 1)

    async def test_a_revoked_role_blocks_the_confirm_press(self) -> None:
        """Re-checked at confirm time, not assumed from the /show press that
        opened the dialog -- same reasoning as discord_bot.py's
        _ConfirmGrabView.confirm."""
        client = _FakeSonarr()
        show = {"title": "NCIS", "statistics": {"seasonCount": 24}}
        add_fn = functools.partial(_add_show, client=client, setup=_setup(), limiter=AddRateLimiterStub())
        view = _ConfirmAddShowView(show, _deny, add_fn)
        interaction = _FakeInteraction()
        await view.confirm.callback(interaction)
        self.assertEqual(client.add_calls, [], "a denied guard must not let the add through")

    async def test_cancel_adds_nothing(self) -> None:
        client = _FakeSonarr()
        show = {"title": "NCIS", "statistics": {"seasonCount": 24}}
        add_fn = functools.partial(_add_show, client=client, setup=_setup(), limiter=AddRateLimiterStub())
        view = _ConfirmAddShowView(show, _allow, add_fn)
        interaction = _FakeInteraction()
        await view.cancel.callback(interaction)
        self.assertEqual(client.add_calls, [])
        [(content, kwargs)] = interaction.response.messages
        self.assertIn("Cancelled", content)
        self.assertIsNone(kwargs.get("view"))


# ---------------------------------------------------------------------------
# /music add flow
# ---------------------------------------------------------------------------


class AddAlbumTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_album_for_an_existing_artist_is_requested_directly(self) -> None:
        client = _FakeLidarr(albums_by_artist={402: [{"id": 42620, "foreignAlbumId": "abc-1"}]})
        album = {"title": "Astral Rejection", "foreignAlbumId": "abc-1", "artistId": 402, "artist": {"artistName": "Some Band"}}
        interaction = _FakeInteraction()
        await _add_album(interaction, album, client=client, setup=_setup(), limiter=AddRateLimiterStub(), poll_attempts=3, poll_seconds=0)
        self.assertEqual(client.add_artist_calls, [], "the artist already exists -- nothing to add")
        self.assertEqual(client.monitor_calls, [(42620, True)])
        self.assertEqual(client.search_calls, [42620])
        [(content, _kwargs)] = interaction.followup.sent
        self.assertIn("Requested", content)
        self.assertIn("Astral Rejection", content)
        self.assertIn("Some Band", content)

    async def test_a_new_artist_is_added_then_the_album_is_found_and_requested(self) -> None:
        client = _FakeLidarr(add_artist_result={"id": 900}, albums_by_artist={900: [{"id": 55, "foreignAlbumId": "new-1"}]})
        album = {
            "title": "Some Album",
            "foreignAlbumId": "new-1",
            "artistId": 0,
            "artist": {"id": 0, "foreignArtistId": "fa-1", "artistName": "New Band"},
        }
        interaction = _FakeInteraction()
        await _add_album(interaction, album, client=client, setup=_setup(quality_profile_id=3, metadata_profile_id=1), limiter=AddRateLimiterStub(), poll_attempts=3, poll_seconds=0)
        [artist_payload] = client.add_artist_calls
        self.assertEqual(artist_payload["foreignArtistId"], "fa-1")
        self.assertEqual(artist_payload["qualityProfileId"], 3)
        self.assertEqual(artist_payload["metadataProfileId"], 1)
        self.assertEqual(artist_payload["monitorNewItems"], "none")
        self.assertEqual(artist_payload["addOptions"], {"monitor": "none", "searchForMissingAlbums": False})
        self.assertEqual(client.monitor_calls, [(55, True)])
        self.assertEqual(client.search_calls, [55])

    async def test_an_album_that_never_shows_up_gets_a_clear_message(self) -> None:
        """"Handle the case where adding the artist has not yet populated
        its albums... give a clear message if the album still isn't
        there" -- the PR description's exact wording for this case."""
        client = _FakeLidarr(add_artist_result={"id": 900}, albums_by_artist={900: []})
        album = {"title": "Some Album", "foreignAlbumId": "missing", "artistId": 0, "artist": {"id": 0, "foreignArtistId": "fa-1", "artistName": "New Band"}}
        interaction = _FakeInteraction()
        await _add_album(interaction, album, client=client, setup=_setup(), limiter=AddRateLimiterStub(), poll_attempts=2, poll_seconds=0)
        self.assertEqual(client.monitor_calls, [])
        self.assertEqual(client.search_calls, [])
        [(content, _kwargs)] = interaction.followup.sent
        self.assertIn("hasn't shown up in its catalog yet", content)
        self.assertIn("try", content.lower())

    async def test_an_already_requested_album_adds_nothing(self) -> None:
        client = _FakeLidarr()
        interaction = _FakeInteraction()
        await _add_album(
            interaction, {"title": "X", "monitored": True, "artistId": 402}, client=client, setup=_setup(), limiter=AddRateLimiterStub(), poll_attempts=1, poll_seconds=0
        )
        self.assertEqual(client.add_artist_calls, [])
        self.assertEqual(client.monitor_calls, [])

    async def test_find_local_album_polls_until_it_appears(self) -> None:
        """The FIRST attempt sees nothing (Lidarr hasn't refreshed yet); the
        second does -- proves the bounded wait actually waits rather than
        giving up on the first empty read."""
        calls = {"n": 0}

        class _SlowLidarr(_FakeLidarr):
            def albums(self, artist_id: int) -> list[dict]:
                calls["n"] += 1
                if calls["n"] < 2:
                    return []
                return [{"id": 77, "foreignAlbumId": "late-album"}]

        result = await _find_local_album(_SlowLidarr(), 900, "late-album", attempts=5, delay_seconds=0)
        self.assertEqual(result, 77)
        self.assertEqual(calls["n"], 2)


# ---------------------------------------------------------------------------
# _RequestView (shared by /movie, /show, /music)
# ---------------------------------------------------------------------------


class RequestViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_pressing_an_actionable_slot_calls_on_request_with_that_item(self) -> None:
        items = [{"title": f"item-{i}"} for i in range(3)]
        seen: list[dict] = []

        async def on_request(_interaction: discord.Interaction, item: dict) -> None:
            seen.append(item)

        view = _RequestView(items, lambda i: i["title"], lambda _i: True, _allow, on_request, "Results")
        interaction = _FakeInteraction()
        await view._make_callback(1)(interaction)
        self.assertEqual(seen, [items[1]])

    async def test_a_non_actionable_item_disables_its_button_and_on_request_is_never_called(self) -> None:
        items = [{"title": "already-have"}]
        called = False

        async def on_request(_interaction: discord.Interaction, _item: dict) -> None:
            nonlocal called
            called = True

        view = _RequestView(items, lambda i: i["title"], lambda _i: False, _allow, on_request, "Results")
        # Five button slots always exist per page (Discord's own per-row
        # cap -- see _PAGE_SIZE), regardless of how many items there are;
        # only the first is bound to this one real item.
        self.assertTrue(view._action_buttons[0].disabled)
        interaction = _FakeInteraction()
        await view._make_callback(0)(interaction)
        self.assertFalse(called, "a disabled slot's callback must still refuse, not silently add")
        [(content, _kwargs)] = interaction.response.messages
        self.assertIn("can't be requested", content)

    async def test_the_second_pages_first_slot_resolves_to_the_sixth_item(self) -> None:
        """Same bug class discord_bot.py's `_resolve_page_item` exists to
        prevent -- see its docstring. Reused here, not re-implemented, so
        this is really testing that the reuse is wired correctly."""
        items = [{"title": f"item-{i}"} for i in range(12)]
        seen: list[dict] = []

        async def on_request(_interaction: discord.Interaction, item: dict) -> None:
            seen.append(item)

        view = _RequestView(items, lambda i: i["title"], lambda _i: True, _allow, on_request, "Results")
        view.page = 1
        view._sync_action_buttons()
        interaction = _FakeInteraction()
        await view._make_callback(0)(interaction)
        self.assertEqual(seen, [items[5]])


# ---------------------------------------------------------------------------
# /queue
# ---------------------------------------------------------------------------


class RenderQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_three_unset_says_so(self) -> None:
        message = await _render_queue(None, None, None)
        self.assertIn("aren't set up here", message)

    async def test_an_unset_service_shows_not_set_up_here_in_its_own_section(self) -> None:
        radarr = _FakeRadarr(queue_result={"records": []})
        message = await _render_queue(radarr, None, None)
        self.assertIn("Movies", message)
        self.assertIn("nothing downloading", message)
        self.assertIn("Shows", message)
        self.assertIn("not set up here", message)

    async def test_an_active_download_shows_title_and_progress(self) -> None:
        radarr = _FakeRadarr(queue_result={"records": [{"title": "Some Movie", "size": 1000, "sizeleft": 300}]})
        message = await _render_queue(radarr, None, None)
        self.assertIn("Some Movie", message)
        self.assertIn("70%", message)

    async def test_an_unavailable_queue_is_reported_not_raised(self) -> None:
        radarr = _FakeRadarr()
        radarr.queue = mock.Mock(side_effect=ServiceError("radarr", "down"))
        message = await _render_queue(radarr, None, None)
        self.assertIn("unavailable", message)


# ---------------------------------------------------------------------------
# _build_guard -- same decision as discord_bot.py's own guard (`is_permitted`,
# already exhaustively tested in tests/test_discord_bot.py's PermissionTests
# -- not re-tested here), just rebuilt as its own closure for media_bot.py.
# ---------------------------------------------------------------------------


class BuildGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_member_with_the_allowed_role_passes(self) -> None:
        guard = _build_guard({1, 2})
        interaction = _FakeInteraction(role_ids=(2,))
        self.assertTrue(await guard(interaction))
        self.assertEqual(interaction.response.messages, [])

    async def test_a_member_without_the_allowed_role_is_refused(self) -> None:
        guard = _build_guard({1, 2})
        interaction = _FakeInteraction(role_ids=(999,))
        self.assertFalse(await guard(interaction))
        [(content, kwargs)] = interaction.response.messages
        self.assertIn("not allowed to use discordarr", content)
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_an_empty_allow_list_refuses_everyone_and_names_the_fix(self) -> None:
        guard = _build_guard(set())
        interaction = _FakeInteraction(role_ids=(1,))
        self.assertFalse(await guard(interaction))
        [(content, _kwargs)] = interaction.response.messages
        self.assertIn("SHELFMARK_DISCORD_ALLOWED_ROLE_IDS", content)

    async def test_a_non_member_such_as_a_dm_is_refused(self) -> None:
        guard = _build_guard({1})
        interaction = _FakeInteraction(role_ids=(1,))
        interaction.user = SimpleNamespace(id=42, roles=[SimpleNamespace(id=1)])  # not a discord.Member
        self.assertFalse(await guard(interaction))


# ---------------------------------------------------------------------------
# Command registration + the "unset service replies instead of crashing"
# mutation-check candidate, at the actual /movie /show /music surface.
# ---------------------------------------------------------------------------


def _bot_with(settings: Settings, roles: set[int] = frozenset({1})) -> commands.Bot:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    install_media_commands(bot, settings, set(roles))
    return bot


class CommandRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_command_set_is_exactly_this(self) -> None:
        bot = _bot_with(Settings())
        names = {c.name for c in bot.tree.get_commands()}
        self.assertEqual(names, {"movie", "show", "music", "queue"})

    async def test_movie_show_and_music_each_require_a_query(self) -> None:
        bot = _bot_with(Settings())
        for name in ("movie", "show", "music"):
            with self.subTest(command=name):
                options = bot.tree.get_command(name).to_dict(bot.tree)["options"]
                self.assertEqual([(o["name"], o["required"]) for o in options], [("query", True)])

    async def test_queue_takes_no_arguments(self) -> None:
        bot = _bot_with(Settings())
        options = bot.tree.get_command("queue").to_dict(bot.tree)["options"]
        self.assertEqual(options, [])

    async def test_every_command_serializes(self) -> None:
        bot = _bot_with(Settings())
        for command in bot.tree.get_commands():
            command.to_dict(bot.tree)  # raises if Discord would refuse it


class UnsetServiceRepliesTests(unittest.IsolatedAsyncioTestCase):
    """Mutation-check candidate from the PR description: break the "is it
    configured" check for one service, confirm the matching test below
    fails, restore it."""

    async def test_movie_replies_when_radarr_is_not_configured(self) -> None:
        bot = _bot_with(Settings())  # nothing configured
        interaction = _FakeInteraction()
        await bot.tree.get_command("movie").callback(interaction, query="anything")
        [(content, kwargs)] = interaction.response.messages
        self.assertIn("aren't set up here", content)
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_show_replies_when_sonarr_is_not_configured(self) -> None:
        bot = _bot_with(Settings())
        interaction = _FakeInteraction()
        await bot.tree.get_command("show").callback(interaction, query="anything")
        [(content, _kwargs)] = interaction.response.messages
        self.assertIn("aren't set up here", content)

    async def test_music_replies_when_lidarr_is_not_configured(self) -> None:
        bot = _bot_with(Settings())
        interaction = _FakeInteraction()
        await bot.tree.get_command("music").callback(interaction, query="anything")
        [(content, _kwargs)] = interaction.response.messages
        self.assertIn("isn't set up here", content)

    async def test_queue_still_answers_with_nothing_configured(self) -> None:
        bot = _bot_with(Settings())
        interaction = _FakeInteraction()
        await bot.tree.get_command("queue").callback(interaction)
        [(content, _kwargs)] = interaction.followup.sent
        self.assertIn("aren't set up here", content)

    async def test_a_user_without_the_required_role_is_refused_before_any_configuration_check(self) -> None:
        """Radarr is unconfigured here too -- if the guard did not run
        first, this would (wrongly) report "movies aren't set up here"
        instead of refusing on role alone."""
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        install_media_commands(bot, Settings(), {999})  # requires a role this user does not have
        interaction = _FakeInteraction(role_ids=(1,))
        await bot.tree.get_command("movie").callback(interaction, query="anything")
        [(content, kwargs)] = interaction.response.messages
        self.assertIn("not allowed", content)
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_an_empty_allow_list_refuses_everyone_and_says_why(self) -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        install_media_commands(bot, Settings(), set())  # empty allow-list
        interaction = _FakeInteraction(role_ids=(1,))
        await bot.tree.get_command("movie").callback(interaction, query="anything")
        [(content, _kwargs)] = interaction.response.messages
        self.assertIn("no allowed roles configured", content)


if __name__ == "__main__":
    unittest.main()

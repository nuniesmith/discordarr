"""Thin clients for Radarr, Sonarr and Lidarr.

Built on the same `HttpClient` shelfmark's own clients used (see
`clients.py`) -- same retry/circuit-breaker behaviour, same `X-Api-Key`
header convention that all three share (they are *arr apps built on the same
Servarr codebase, so their v3/v1 REST APIs agree on this).

Response shapes below are as verified READ-ONLY against a real Radarr
(:7878)/Sonarr (:8989)/Lidarr (:8686) instance on 2026-09-25 -- two are
surprising enough to spell out here rather than only where they are used:

* Radarr's `movie/lookup` (and Sonarr's `series/lookup`) resource has NO
  `hasFile` field at all -- not even `false` -- for a title that is not yet
  in the library; the key is simply absent, along with `id`. Whether a movie
  already has a file is `movieFileId` (nonzero) or a present `movieFile`
  object; whether it is in the library at all is `id` being present and
  truthy. media_bot.py's `_movie_state` is written around that, not around a
  `hasFile` key that does not exist.
* Sonarr's series lookup reports `statistics.totalEpisodeCount` (and every
  per-season `statistics`) as 0 unconditionally, in-library or not -- five
  real shows with 15-38 seasons each all came back with `totalEpisodeCount:
  0`. Only `statistics.seasonCount` is populated. media_bot.py's big-show
  gate (`_is_big_show`) checks the episode threshold too, in case a future
  Sonarr populates it, but today only the season threshold can ever fire.
"""

from __future__ import annotations

from typing import Any

from .clients import HttpClient


class ArrClient:
    """Shared plumbing for the three *arr apps: one base path convention per
    API version and one auth header, already handled by `HttpClient`'s own
    `api_key` -- so each subclass below is just its own endpoint names.
    """

    #: "v3" for Radarr/Sonarr, "v1" for Lidarr -- overridden per subclass.
    api_version = "v3"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        service: str,
        timeout: float = 15.0,
        retries: int = 2,
    ):
        self.http = HttpClient(
            base_url, service=service, api_key=api_key, timeout=timeout, retries=retries, backoff=0.25
        )

    def _get(self, path: str, **params: Any) -> Any:
        return self.http.request(f"/api/{self.api_version}/{path}", params=params or None)

    def _post(self, path: str, json_body: Any) -> Any:
        return self.http.request(f"/api/{self.api_version}/{path}", method="POST", json_body=json_body)

    def _put(self, path: str, json_body: Any) -> Any:
        return self.http.request(f"/api/{self.api_version}/{path}", method="PUT", json_body=json_body)

    def root_folders(self) -> Any:
        return self._get("rootfolder")

    def quality_profiles(self) -> Any:
        return self._get("qualityprofile")

    def queue(self) -> Any:
        return self._get("queue")


class RadarrClient(ArrClient):
    api_version = "v3"

    def __init__(self, base_url: str, api_key: str, **kwargs: Any):
        super().__init__(base_url, api_key, service="radarr", **kwargs)

    def lookup(self, term: str) -> Any:
        return self._get("movie/lookup", term=term)

    def movies(self) -> Any:
        """The whole library. Used ONLY to compute the most-used quality
        profile when none is configured (see media_bot.py's
        `_most_used_profile_id`) -- never for a per-search "is this already
        added" check, which `lookup`'s own `id` field already answers (see
        module docstring)."""
        return self._get("movie")

    def add_movie(self, payload: dict[str, Any]) -> Any:
        return self._post("movie", payload)


class SonarrClient(ArrClient):
    api_version = "v3"

    def __init__(self, base_url: str, api_key: str, **kwargs: Any):
        super().__init__(base_url, api_key, service="sonarr", **kwargs)

    def lookup(self, term: str) -> Any:
        return self._get("series/lookup", term=term)

    def series(self) -> Any:
        """Same role as `RadarrClient.movies` -- most-used-profile only."""
        return self._get("series")

    def add_series(self, payload: dict[str, Any]) -> Any:
        return self._post("series", payload)


class LidarrClient(ArrClient):
    api_version = "v1"

    def __init__(self, base_url: str, api_key: str, **kwargs: Any):
        super().__init__(base_url, api_key, service="lidarr", **kwargs)

    def metadata_profiles(self) -> Any:
        return self._get("metadataprofile")

    def lookup_album(self, term: str) -> Any:
        return self._get("album/lookup", term=term)

    def artists(self) -> Any:
        """Most-used-profile/metadata-profile only (see
        `RadarrClient.movies`) -- an album lookup's own `artistId`/embedded
        `artist.id` is what decides whether a SPECIFIC artist already
        exists (see media_bot.py's `_request_album`), not this list."""
        return self._get("artist")

    def add_artist(self, payload: dict[str, Any]) -> Any:
        return self._post("artist", payload)

    def albums(self, artist_id: int) -> Any:
        """An artist's LOCAL albums -- unlike `album/lookup`, these carry a
        real local `id` (what `album/monitor` and `AlbumSearch` need) rather
        than only a `foreignAlbumId`."""
        return self._get("album", artistId=artist_id)

    def set_album_monitored(self, album_id: int, monitored: bool) -> Any:
        return self._put("album/monitor", {"albumIds": [album_id], "monitored": monitored})

    def search_album(self, album_id: int) -> Any:
        return self._post("command", {"name": "AlbumSearch", "albumIds": [album_id]})

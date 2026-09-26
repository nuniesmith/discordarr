from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from src.discordarr.arr_clients import LidarrClient, RadarrClient, SonarrClient


class _FakeHTTPResponse:
    """Stands in for the `with self.opener.open(...) as response:` context
    HttpClient.request uses -- see clients.py."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.headers = SimpleNamespace(get_content_type=lambda: "application/json")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOpener:
    """Captures every request it is given instead of making one, so a test
    can assert on the exact URL/method/headers/body HttpClient built."""

    def __init__(self, body: object) -> None:
        self.body = json.dumps(body).encode("utf-8")
        self.requests: list = []

    def open(self, request, timeout: float | None = None) -> _FakeHTTPResponse:
        self.requests.append(request)
        return _FakeHTTPResponse(self.body)


def _wire(client, body: object = None) -> _FakeOpener:
    opener = _FakeOpener(body)
    client.http.opener = opener
    return opener


class RadarrClientTests(unittest.TestCase):
    def test_lookup_sends_the_term_to_the_movie_lookup_endpoint(self) -> None:
        client = RadarrClient("http://radarr.invalid", "key123")
        opener = _wire(client, body=[{"title": "Dune"}])
        result = client.lookup("dune part two")
        self.assertEqual(result, [{"title": "Dune"}])
        [request] = opener.requests
        self.assertIn("/api/v3/movie/lookup", request.full_url)
        self.assertIn("term=dune+part+two", request.full_url)
        self.assertEqual(request.get_method(), "GET")

    def test_every_call_carries_the_api_key_as_a_header_not_a_query_param(self) -> None:
        """See the PR description's "never log or echo API keys" rule -- a
        key in the query string ends up in any proxy access log this
        request crosses; X-Api-Key does not."""
        client = RadarrClient("http://radarr.invalid", "super-secret-key")
        opener = _wire(client, body=[])
        client.lookup("anything")
        [request] = opener.requests
        self.assertEqual(request.get_header("X-api-key"), "super-secret-key")
        self.assertNotIn("super-secret-key", request.full_url)

    def test_add_movie_posts_the_payload_to_the_movie_endpoint(self) -> None:
        client = RadarrClient("http://radarr.invalid", "key123")
        opener = _wire(client, body={"id": 9})
        client.add_movie({"title": "Dune", "tmdbId": 1, "qualityProfileId": 1})
        [request] = opener.requests
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/api/v3/movie"))
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(sent, {"title": "Dune", "tmdbId": 1, "qualityProfileId": 1})

    def test_root_folders_and_quality_profiles_hit_their_own_endpoints(self) -> None:
        client = RadarrClient("http://radarr.invalid", "key123")
        opener = _wire(client, body=[])
        client.root_folders()
        client.quality_profiles()
        client.queue()
        urls = [r.full_url for r in opener.requests]
        self.assertTrue(urls[0].endswith("/api/v3/rootfolder"))
        self.assertTrue(urls[1].endswith("/api/v3/qualityprofile"))
        self.assertTrue(urls[2].endswith("/api/v3/queue"))


class SonarrClientTests(unittest.TestCase):
    def test_lookup_hits_the_series_lookup_endpoint(self) -> None:
        client = SonarrClient("http://sonarr.invalid", "key123")
        opener = _wire(client, body=[{"title": "Some Show"}])
        client.lookup("some show")
        [request] = opener.requests
        self.assertIn("/api/v3/series/lookup", request.full_url)

    def test_add_series_posts_to_the_series_endpoint(self) -> None:
        client = SonarrClient("http://sonarr.invalid", "key123")
        opener = _wire(client, body={"id": 5})
        client.add_series({"title": "Some Show", "tvdbId": 1})
        [request] = opener.requests
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/api/v3/series"))


class LidarrClientTests(unittest.TestCase):
    """Lidarr is v1, not v3 -- the one thing every method here is really
    pinning, since a wrong `api_version` would silently 404 in production
    but still pass a test that only checked the path suffix."""

    def test_lookup_album_hits_the_v1_album_lookup_endpoint(self) -> None:
        client = LidarrClient("http://lidarr.invalid", "key123")
        opener = _wire(client, body=[{"title": "An Album"}])
        client.lookup_album("an album")
        [request] = opener.requests
        self.assertIn("/api/v1/album/lookup", request.full_url)
        self.assertIn("term=an+album", request.full_url)

    def test_add_artist_posts_to_the_v1_artist_endpoint(self) -> None:
        client = LidarrClient("http://lidarr.invalid", "key123")
        opener = _wire(client, body={"id": 31})
        client.add_artist({"foreignArtistId": "abc", "artistName": "Some Band"})
        [request] = opener.requests
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/api/v1/artist"))

    def test_albums_filters_by_artist_id(self) -> None:
        client = LidarrClient("http://lidarr.invalid", "key123")
        opener = _wire(client, body=[])
        client.albums(31)
        [request] = opener.requests
        self.assertTrue(request.full_url.startswith("http://lidarr.invalid/api/v1/album?"))
        self.assertIn("artistId=31", request.full_url)

    def test_set_album_monitored_puts_the_exact_documented_body(self) -> None:
        """PUT /api/v1/album/monitor {albumIds:[id], monitored:true} -- the
        exact shape the PR description specifies, not just "some body"."""
        client = LidarrClient("http://lidarr.invalid", "key123")
        opener = _wire(client, body={})
        client.set_album_monitored(42, True)
        [request] = opener.requests
        self.assertEqual(request.get_method(), "PUT")
        self.assertTrue(request.full_url.endswith("/api/v1/album/monitor"))
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"albumIds": [42], "monitored": True})

    def test_search_album_posts_the_exact_documented_albumsearch_command(self) -> None:
        client = LidarrClient("http://lidarr.invalid", "key123")
        opener = _wire(client, body={})
        client.search_album(42)
        [request] = opener.requests
        self.assertTrue(request.full_url.endswith("/api/v1/command"))
        self.assertEqual(
            json.loads(request.data.decode("utf-8")), {"name": "AlbumSearch", "albumIds": [42]}
        )

    def test_metadata_profiles_hits_its_own_v1_endpoint(self) -> None:
        client = LidarrClient("http://lidarr.invalid", "key123")
        opener = _wire(client, body=[])
        client.metadata_profiles()
        [request] = opener.requests
        self.assertTrue(request.full_url.endswith("/api/v1/metadataprofile"))


if __name__ == "__main__":
    unittest.main()

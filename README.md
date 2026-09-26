# discordarr

One Discord bot for a household's media. It answers slash commands for
books (via [Shelfmark](https://github.com/nuniesmith/shelfmark)), movies
(via Radarr), TV shows (via Sonarr), and music (via Lidarr, album by
album) -- and shows what all three are downloading right now.

## Why one bot

discordarr **replaces the Shelfmark Discord bot**, on the exact same bot
token and Discord application. Discord lets exactly one process own a
bot's slash commands at a time -- two bots sharing a token would each
overwrite the other's command registrations -- so there is no "run both
side by side" migration step: retire the Shelfmark bot container and
deploy this one in its place, pointed at the same `.env`.

Every one of Shelfmark's own commands is ported with identical behaviour
(see `src/discordarr/discord_bot.py`, carried over from
[shelfmark#39](https://github.com/nuniesmith/shelfmark/pull/39) unchanged
except its import paths). Nothing about how books work has changed.

## Commands

All replies are ephemeral (visible only to the person who ran the
command) and gated by the same role check: a member must hold one of the
roles in `SHELFMARK_DISCORD_ALLOWED_ROLE_IDS` to use ANY command below,
books or media alike. An empty allow-list means nobody may use the bot --
that is intentional fail-closed behaviour, not a bug waiting to be
configured.

### Books (Shelfmark API) -- ported, unchanged

| Command | What it does |
|---|---|
| `/library type:<audiobook\|ebook> query:<text>` | Browse (empty query) or search what's already on the server. Ebook results can be sent as a Discord attachment, or a signed download link when the file is over the attachment limit. |
| `/request type:<audiobook\|ebook> query:<text>` | Search Prowlarr for something new; a Grab button queues the download. A release at or above the large-release threshold asks for confirmation first. |
| `/downloads` | What Shelfmark's qBittorrent instance is downloading right now. |
| `/job [job_id]` | Recent jobs, or one by ID. |
| `/cancel [job_id]` | Stop a queued or running job. |
| `/scan [force]` | Ask Audiobookshelf to re-scan the library. |

### Movies, shows, and music (Radarr / Sonarr / Lidarr) -- new

| Command | What it does |
|---|---|
| `/movie query:<text>` | Search Radarr. Each result shows ✅ (in the library with a file), ⏳ (requested, no file yet -- with live download progress when available), or ➕ (not requested), with a Request button on the ones that are not requested yet. |
| `/show query:<text>` | Same shape, against Sonarr. **A show at or above the configured season/episode threshold (default: more than 5 seasons or 100 episodes) asks for confirmation before requesting all of it** -- a mis-ranked or simply huge show is one press away from every episode of it queuing at once. |
| `/music query:<text>` | Search Lidarr **at the album level** -- this requests one album, never a whole discography. If the artist is not in Lidarr yet, requesting an album adds the artist (with nothing else monitored) and then monitors and searches for just that one album once Lidarr's background refresh has populated it. |
| `/queue` | What Radarr, Sonarr and Lidarr are each downloading right now (title, progress, time left). Books keep their own `/downloads`, above -- Shelfmark's qBittorrent category is not visible to the other three apps. |

Movies, shows and music are each configured independently (see below). A
service left unconfigured makes its command reply "isn't set up here"
rather than crashing or disappearing from the slash-command list.

### How a request flows to each service

- **Books**: `/request` &rarr; `POST /api/v1/releases/grab` on the
  Shelfmark API, which hands the release to qBittorrent. Unchanged from
  the original bot.
- **Movies**: `/movie` &rarr; `GET /api/v3/movie/lookup` on Radarr for
  search, `POST /api/v3/movie` to request (with `qualityProfileId`,
  `rootFolderPath`, `monitored: true`, `minimumAvailability: "released"`,
  `addOptions: {searchForMovie: true}` merged onto the lookup result).
- **Shows**: `/show` &rarr; `GET /api/v3/series/lookup` on Sonarr for
  search, `POST /api/v3/series` to request (`qualityProfileId`,
  `rootFolderPath`, `seasonFolder: true`, `monitored: true`,
  `addOptions: {monitor: "all", searchForMissingEpisodes: true}`; no
  `languageProfileId` -- Sonarr v4 dropped language profiles).
- **Music**: `/music` &rarr; `GET /api/v1/album/lookup` on Lidarr for
  search. Requesting an album whose artist is not yet in Lidarr first
  does `POST /api/v1/artist` (monitoring nothing yet), polls
  `GET /api/v1/album?artistId=` for the requested album to appear, then
  `PUT /api/v1/album/monitor` and `POST /api/v1/command
  {name: "AlbumSearch"}` on it specifically.
- **Everything's downloads**: `/queue` &rarr; `GET /api/v3/queue`
  (Radarr/Sonarr) and `GET /api/v1/queue` (Lidarr), one section each.

Movies and shows go **directly to Radarr and Sonarr** -- there is no
Overseerr/Jellyseerr/Seerr layer in front of them; it was never set up
for this household, so discordarr talks to both apps' own APIs.

### Rate limiting

Every **add** across `/movie`, `/show` and `/music` shares one
per-Discord-user budget (default 10 per hour, see
`DISCORDARR_ADD_RATE_LIMIT_MAX`/`_WINDOW_SECONDS`), independent of
Shelfmark's own request-rate limiting on the API side. A search never
counts against it -- only pressing Request does. Going over it gets a
plain "you have run too many adds, try again in N minutes" reply, the
same phrasing Shelfmark's own 429 messages use.

## Configuration

Copy `.env.example` to `.env` and fill in real values; see that file for
every variable with its default and a short explanation. The short
version:

- `DISCORD_BOT_TOKEN` -- required.
- `SHELFMARK_API_URL`, `SHELFMARK_API_TOKEN`, `SHELFMARK_DISCORD_GUILD_ID`,
  `SHELFMARK_DISCORD_ALLOWED_ROLE_IDS`,
  `SHELFMARK_DISCORD_MAX_ATTACHMENT_MB`,
  `SHELFMARK_DISCORD_LARGE_RELEASE_THRESHOLD_MB` -- exactly the names the
  current Shelfmark bot container already uses; deployment is a drop-in
  replacement. `DISCORDARR_API_URL`, `DISCORDARR_API_TOKEN`,
  `DISCORDARR_DISCORD_BOT_TOKEN`, `DISCORDARR_DISCORD_GUILD_ID`,
  `DISCORDARR_DISCORD_ALLOWED_ROLE_IDS`,
  `DISCORDARR_DISCORD_MAX_ATTACHMENT_MB`, and
  `DISCORDARR_DISCORD_LARGE_RELEASE_THRESHOLD_MB` are accepted as
  aliases for a fresh deployment that would rather not carry the
  Shelfmark-era names; the canonical name always wins if both are set.
- `RADARR_URL`/`RADARR_API_KEY`, `SONARR_URL`/`SONARR_API_KEY`,
  `LIDARR_URL`/`LIDARR_API_KEY` -- each optional and independent. A
  service needs BOTH its URL and key set to count as configured.
- `RADARR_ROOT_FOLDER`/`RADARR_QUALITY_PROFILE`, and the equivalent pair
  for Sonarr and Lidarr, plus `LIDARR_METADATA_PROFILE` -- all optional.
  Left unset, the root folder is that app's only root folder (more than
  one and none chosen is refused with a config message, not a guess), and
  the quality profile is whichever one is MOST USED by items already in
  that app's library, falling back to its first profile. These are
  fetched once and cached for the life of the process, not re-fetched on
  every command.
- `SONARR_BIG_SHOW_SEASON_THRESHOLD`/`_EPISODE_THRESHOLD` -- the `/show`
  confirmation gate's thresholds.
- `LIDARR_ALBUM_POLL_ATTEMPTS`/`_POLL_SECONDS` -- how long `/music` waits
  for a newly-added artist's album to appear before giving up.
- `DISCORDARR_ADD_RATE_LIMIT_MAX`/`_WINDOW_SECONDS` -- the shared add
  budget described above.
- `DISCORDARR_ARR_HTTP_TIMEOUT_SECONDS`/`_ARR_HTTP_RETRIES` -- shared by
  the Radarr/Sonarr/Lidarr HTTP client.

Auth to every *arr app is the `X-Api-Key` header (never a query
parameter); API keys are never logged, echoed, or included in an error
message.

## Response-shape notes

A few things verified read-only against real Radarr/Sonarr/Lidarr
instances turned out to differ from the obvious assumption, and the code
is written around what is actually there:

- Radarr's `movie/lookup` (and Sonarr's `series/lookup`) has **no
  `hasFile` field** -- not even `false` -- for a title that isn't in the
  library yet; `id` is simply absent too. Whether a movie already has a
  file is `movieFileId` (nonzero) or a present `movieFile` object.
- Sonarr's series lookup reports `statistics.totalEpisodeCount` (and
  every per-season statistics block) as `0` unconditionally -- checked
  against five real shows with 15-38 seasons each. Only
  `statistics.seasonCount` is populated, so in practice only the season
  threshold in the `/show` confirmation gate can fire today; the episode
  threshold is still checked, in case a future Sonarr populates it.
- Lidarr's `album/lookup` result carries the artist's id at the top level
  (`artistId`) as well as embedded in `artist.id` -- either tells you
  whether the artist already exists -- but has no per-track file-count
  field the way the LOCAL `/api/v1/album?artistId=` listing does, so
  `/music`'s search results can only say "requested" or "not requested",
  not "downloaded".

See `src/discordarr/arr_clients.py` and `src/discordarr/media_bot.py` for
where each of these actually matters.

## Development

Stack: Python 3.13, `discord.py==2.5.2`, stdlib `unittest` -- no other
runtime dependency (every HTTP client here, Shelfmark's API included, is
plain `urllib`).

This machine's Python may not have `discord.py` installed; run the tests
in a container instead:

```sh
docker run --rm -v "$PWD:/app" -w /app python:3.13-slim \
  bash -c "pip install -q -r requirements.txt && python -m unittest discover -s tests -v"
```

Build the real image and run it (it idles and prints why if
`DISCORD_BOT_TOKEN`/`SHELFMARK_API_TOKEN` are missing, rather than
crash-looping):

```sh
docker build -t discordarr .
docker run --rm --env-file .env discordarr
```

## Deployment

CI (`.github/workflows/ci.yml`) runs the test suite and a Docker build on
every push and pull request. On push to `main`, it also publishes
`ghcr.io/nuniesmith/discordarr` (tags `latest` and the full commit SHA,
with the `org.opencontainers.image.revision` label set to that SHA) and
asks `nuniesmith/freddy` to roll the new image out via its
`update-service.yml` workflow, the same way Shelfmark's own CI does.

Every Monday (08:17 UTC) CI also rebuilds the image on a freshly pulled
`python:3.13-slim` and rolls it out the same way, so the base image and its
libraries keep receiving fixes even when the code has not changed. That
rebuild reuses the commit's SHA tag, and the bot reconnects once a week when it
lands.

### Health

The image's `HEALTHCHECK` runs `discordarr-healthcheck`, which passes only
while the bot keeps touching a heartbeat file (`/tmp/discordarr-heartbeat`,
every 30 seconds). It touches that file only when it is logged in and hearing
back from Discord's gateway (`heartbeat.py`). A container whose bot has
disconnected or stuck turns `unhealthy` within about two minutes, where it used
to look like any other running container. A bot idling on purpose, with no
token, reads unhealthy too, because it serves no one.

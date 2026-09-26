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

## Book commands: design notes

Carried over verbatim from Shelfmark's README when its bot moved here (nuniesmith/shelfmark, 2026-09-26). It says "the bot" and "Shelfmark"; both mean these six book commands, which talk to shelfmark-api exactly as they did there. The environment variable names are unchanged.

Two commands, each taking a **type** choice (`Audiobook` / `Ebook`, rendered
by Discord as a picker, not free text) and a query:

- **`/library type:<audiobook|ebook> query:<text>`** — what's already here.
  `audiobook` searches Audiobookshelf; `ebook` walks the on-disk ebooks root
  and offers a **Send** button. The type choice is the only thing that
  changes: the person searching doesn't need to know that audiobooks and
  ebooks live in two different places on the server.
- **`/request type:<audiobook|ebook> query:<text>`** — find something new and
  download it. Searches Prowlarr filtered to that type's configured
  categories and offers a **Grab** button. A release at or above
  `SHELFMARK_DISCORD_LARGE_RELEASE_THRESHOLD_MB` (default 5000 MB) doesn't
  queue on that first press — see "Confirming a large grab" below.

These replace four earlier commands — `/library-search`, `/ebook-search`,
`/ebook-request`, `/release-search` — retired outright rather than kept as
aliases. `/release-search` and `/ebook-request` had drifted into being the
exact same call (`/api/v1/releases/search`, `book_only=true`, the same grab
buttons) with only a cosmetic difference left; and with exactly two users of
this bot and `SHELFMARK_DISCORD_GUILD_ID` making new commands appear
instantly, a transition alias would only be permanent upkeep for a switch one
Discord message covers.

**Paging through results (2026-09-16).** All three result lists — `/request`
and both `/library` types — used to show five results and stop, with no way
to reach a sixth. That is a real problem, not a hypothetical one: Prowlarr
ranks on text match, not on what was actually asked for, and a real
`/request type:audiobook query:"the stand"` search put two *Creativity, Inc*
results and a 26 GB Westerns collection ahead of the Stephen King audiobook
actually searched for, which landed at position 3 — comfortably inside five
that time, but nothing stops the same ranking noise from landing a real
result at position 8 or 15 on a different query. The primary user is
non-technical, so "guess a narrower query" is the wrong answer.

- Previous/Next buttons page five results at a time, with the current
  position shown in the embed footer (e.g. "6-10 of 25"). Paging is
  **client-side** over whatever was already fetched in the one search
  behind the message — a page turn never re-queries Prowlarr, Audiobookshelf,
  or the ebook root, since a fresh search can take seconds and re-running one
  on every Next press would make paging feel broken.
- Fetch limits were raised so more of what a page turn can reach is actually
  fetched to begin with: `/request` 25→50 (`api.release_search`'s ceiling is
  200), `/library type:ebook` 10→25 (`api.ebook_search`'s own ceiling), and
  `/library type:audiobook` from an implicit 12 to an explicit 25. All three
  now page out to 5 full pages (`/request` gets 10).
  Previous is disabled on the first page and Next on the last, rather than
  erroring — pressing either never reaches a callback once Discord has
  greyed it out.
- **The Grab/Send buttons resolve against the current page, not a frozen
  index.** A view built once from the first five results, with buttons
  merely relabelled on a page turn, would keep grabbing items 0-4 forever no
  matter which page was showing — the button would read "Grab 6" while
  silently still queuing item 1, with nothing to tell the user until the
  wrong book arrived. Every Grab/Send callback instead recomputes its target
  from the CURRENT page each time it is pressed (`page * page_size +
  local_index`, evaluated fresh, not captured when the view was built).
- The role allow-list guard is re-checked on every Previous/Next press, the
  same way `_ConfirmGrabView`'s confirm button re-checks it (see "Confirming
  a large grab" below) — paging is the first control in this bot that
  invites sitting on a view and actively using it for its whole 900-second
  lifetime rather than pressing once and being done, so a role revoked
  partway through a paging session takes effect on the very next press.
- A view that times out (900s, unchanged) edits the message to say "This
  search has expired" and removes its buttons, rather than leaving a dead
  message where a press reaches a bot with no handler left for it (Discord's
  own answer to that is a bare "This interaction failed", with nothing to
  explain why). That edit rides the same ~15-minute interaction webhook
  token the timeout itself is keyed to the age of, so it is best-effort: if
  Discord's clock expires the token a beat before the edit runs, the edit
  itself fails and is swallowed rather than crashing the bot.

**Both types search only their own categories, on purpose.** Prowlarr indexes
everything, so an unfiltered search for "dune" returns
`Dune Part Two 2024 BluRay 1080p` (category 2050) and a Car SOS episode about
a dune buggy (5010) before it returns a single book. `/request` always sends
Prowlarr an explicit category set for the chosen type — it never searches
every category. Verified against the live indexer: filtering to `book_only`'s
7000 alone returns zero audiobooks across a 103-result sample; audiobooks are
under `3030` (Audio/Audiobook, standard Newznab) and `100064` (this indexer's
own AudioBook category).

- `PROWLARR_BOOK_CATEGORIES` (default `7000`) — the ebook filter. Not `7020`
  (EBook specifically): this indexer doesn't advertise that category, and a
  literal 7020 filter silently returns nothing.
- `PROWLARR_AUDIOBOOK_CATEGORIES` (default `3030,100064`) — the audiobook
  filter. `100064` is this indexer's own category id; a different indexer
  would need a different value here, which is why both stay configurable
  rather than hardcoded.

The underlying API route, `/api/v1/releases/search`, still has its older
`book_only` flag (default `true`, searches `PROWLARR_BOOK_CATEGORIES`) for
any caller that only wants "books, generically" without picking a type. The
newer `media_type` parameter the Discord commands use sits beside it and
takes precedence when set — pass `categories` explicitly to bypass both.

Other commands: `/downloads`, `/job`, `/metadata-match`, `/scan`,
`/organize-preview`. Every command is deferred before any network call and
answered ephemerally, so the interaction token is never used as a
long-running task channel.

**Ebooks.** Audiobookshelf has no ebook library, and running a separate
reader app just to browse files isn't wanted, so Shelfmark indexes
`SHELFMARK_EBOOKS_ROOT` itself:

- `/library type:ebook query:<text>` walks the ebooks root, matching on
  author, title, and filename, fetches up to 25 matches, and pages through
  them 5 at a time with a **Send** button per result on the current page.
  Pressing one fetches the file and attaches it to an ephemeral reply — open
  it from Discord on a phone and it lands in whichever app is registered for
  that format. When a book has more than one file (an epub next to a pdf,
  say), the better format wins automatically, in `EBOOK_PREF` order.
- `/request type:ebook query:<text>` searches Prowlarr restricted to
  `PROWLARR_BOOK_CATEGORIES` and offers a grab button.
- Discord refuses attachments over 10 MB on an unboosted server. The size is
  checked *before* any upload is attempted, and a book over the limit gets a
  **download link** instead, posted only to the person who asked. The link
  works for one book, expires (24 hours by default), and is served by the API
  at `/dl/<token>`, so it needs `SHELFMARK_PUBLIC_URL`. The token is signed
  with a key derived from `SHELFMARK_API_TOKEN`, so rotating that token
  revokes every outstanding link. Without a public URL the bot says the book
  is too big, naming it and its size, as before. Raise the attachment ceiling
  with `SHELFMARK_DISCORD_MAX_ATTACHMENT_MB` if the server is boosted.
- A search result's id is an opaque token, never a filesystem path. The
  download endpoint re-derives it from the files it finds under the ebooks
  root and only serves a match that resolves back inside that root — a
  request built from someone else's search result, or a raw path, matches
  nothing.

**Confirming a large grab.** Prowlarr ranks on text match, not on what was
actually asked for. A real `/request type:audiobook query:"the stand"` search
returned a 26 GB "Westerns ... GraphicAudio Collection" ahead of the book
itself, because it matched on "Stand-Alone" containing "stand" — one press of
its Grab button would have pulled the entire 26 GB collection instead of the
2.8 GB audiobook that was actually wanted. This is not a disk-space guard (a
26 GB download fits fine on either box); it exists because undoing a
mis-click means finding and removing the download in qBittorrent, on
Sullivan's disk, and possibly out of the library.

- A release at or above `SHELFMARK_DISCORD_LARGE_RELEASE_THRESHOLD_MB`
  (default `5000`, i.e. 5 GB) doesn't queue on the first Grab press. The bot
  instead replies with the release's title and size and a **Grab anyway** /
  **Cancel** pair; only pressing **Grab anyway** queues it, exactly as a
  normal Grab would. Below the threshold, one press still queues
  immediately — nothing changes for a normal 2–3 GB audiobook (Stephen
  King's unabridged *The Stand* is 2813 MB, comfortably under the default).
- A release with **no reported size** also stops for confirmation rather than
  being waved through. A grab has no second, real-bytes check the way the
  ebook attachment path does (that one re-checks the actual fetched size
  before sending) — the job goes straight to a remote worker and its true
  size is never seen again here, so an unusable size is treated as the risky
  case, not an exempt one.
- The confirm button re-checks `SHELFMARK_DISCORD_ALLOWED_ROLE_IDS` at the
  moment it's pressed, not just at the original `/request`. The size-warning
  message can sit for up to 15 minutes; someone whose role was revoked in
  that window cannot complete the grab just because the warning is still on
  their screen.

**Invite it with zero permissions.** Scopes `bot` and
`applications.commands`, permission integer `0`. Every reply is an ephemeral
interaction response, which needs no channel permission at all. It also
requests **no privileged intents** — `Intents.none()` plus `guilds` — so there
is nothing to justify in the developer portal and no verification gate later.

| Variable | What it does |
|---|---|
| `DISCORD_BOT_TOKEN` | Required. Without it the bot logs why and idles. |
| `SHELFMARK_API_TOKEN` | Required — the bot calls the API with it. |
| `SHELFMARK_DISCORD_GUILD_ID` | Syncs commands to one guild, which is instant. Without it they sync globally and can take up to an hour to appear. |
| `SHELFMARK_DISCORD_ALLOWED_ROLE_IDS` | Comma-separated role IDs permitted to use the bot. **Empty means nobody.** |
| `SHELFMARK_DISCORD_MAX_ATTACHMENT_MB` | `/library type:ebook`'s file-size ceiling before Discord would refuse the upload. Default `10`; raise it if the server is boosted. |
| `SHELFMARK_PUBLIC_URL` | Where people reach the API, e.g. `https://shelfmark.example.org`. The base of the download link a book over the attachment ceiling gets instead. Unset: no links. Set on the **API**, which issues them. |
| `SHELFMARK_DOWNLOAD_LINK_HOURS` | How long a download link works. Default `24`. |
| `SHELFMARK_DOWNLOAD_LINK_NOTE` | One line posted with every link, e.g. that the address only opens on a private network. |
| `SHELFMARK_DISCORD_LARGE_RELEASE_THRESHOLD_MB` | Size (in MB) at or above which `/request`'s Grab button asks for confirmation instead of queuing immediately. Default `5000`. |
| `PROWLARR_BOOK_CATEGORIES` | Categories `/request type:ebook` restricts to. Default `7000`, since a typical indexer advertises the general Books bucket rather than 7020 (EBook) specifically. |
| `PROWLARR_AUDIOBOOK_CATEGORIES` | Categories `/request type:audiobook` restricts to. Default `3030,100064` (standard Newznab Audio/Audiobook plus this indexer's own AudioBook category) — verified against the live indexer; `book_only`'s 7000 alone returns zero audiobooks. |

To collect the IDs, turn on **User Settings → Advanced → Developer Mode**, then
right-click the server for its ID and a role (in **Server Settings → Roles**)
for its ID.

The role itself needs **no Discord permissions**. It is used only as a
membership tag — the check is a set intersection on role IDs and never reads a
permission bit. Granting it anything real would hand those people server powers
the bot will never consult.

Restrict the bot to one channel through **Server Settings → Integrations →
Shelfmark → Manage** rather than in code. Discord enforces that before the
interaction is ever sent.

**The allow-list fails closed.** With `SHELFMARK_DISCORD_ALLOWED_ROLE_IDS`
unset, every command is refused with a message saying so. It used to permit
everyone, which meant a bot deployed before its roles were configured let any
member of the server run any command — `/organize-preview` included, which
takes an arbitrary absolute path and reports what is at it.

**Missing configuration does not crash-loop.** The container runs under
`restart: unless-stopped`, so exiting on a missing token would respawn forever
and scroll the one useful message out of the log. Instead the bot prints what
is missing and idles until the next deployment supplies it. A token Discord
*rejects* is treated the same way, rather than retried against the login
endpoint until the application is rate-limited.

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

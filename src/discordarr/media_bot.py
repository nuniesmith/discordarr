"""`/movie`, `/show`, `/music` and `/queue` -- the commands discordarr adds on
top of the ported Shelfmark bot (see discord_bot.py).

Reuses discord_bot.py's own building blocks rather than re-implementing them:
`_PagedView` (paging chrome), `_resolve_page_item` (the fix for a button that
must resolve against the CURRENT page, not the one it was built on -- see
its docstring), `is_permitted` (the fail-closed role check) and `_actor`
(the same per-user key used for logging and rate limiting). Everything below
is new: one generic `_RequestView` for the Request-button row (shared by all
three add commands, the same way ReleaseView/EbookView/CancelView are three
thin subclasses of one base in discord_bot.py), the Radarr/Sonarr/Lidarr
state functions, and the add flows themselves.

Response shapes referenced below (Radarr's missing `hasFile`, Sonarr's
always-zero episode counts, Lidarr's artist-embedded-in-album-lookup) were
verified READ-ONLY against the real services on 2026-09-25 -- see
arr_clients.py's module docstring for the details.
"""

from __future__ import annotations

import asyncio
import functools
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .arr_clients import LidarrClient, RadarrClient, SonarrClient
from .clients import ServiceError
from .config import Settings
from .discord_bot import _PAGE_SIZE, _PagedView, _actor, _resolve_page_item, is_permitted
from .ratelimit import AddRateLimiter, rate_limit_message

# ---------------------------------------------------------------------------
# Access control -- one allow-list, shared with the ported commands.
# ---------------------------------------------------------------------------


def _build_guard(allowed_roles: set[int]) -> Callable[[discord.Interaction], Awaitable[bool]]:
    """Same decision as discord_bot.py's own guard: `is_permitted` (imported,
    not re-implemented) fails closed on an empty allow-list. Rebuilt here
    rather than exported from discord_bot.py because that module's `guard`
    closure is private to `install_commands` -- Part 1 ports it unchanged,
    it does not grow a new export for Part 2 to reach into.
    """

    def permitted(interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            return False  # a DM has no roles, so it can never be allowed
        return is_permitted((role.id for role in member.roles), allowed_roles)

    async def guard(interaction: discord.Interaction) -> bool:
        if permitted(interaction):
            return True
        if not allowed_roles:
            message = (
                "discordarr has no allowed roles configured, so every command is "
                "refused. Set SHELFMARK_DISCORD_ALLOWED_ROLE_IDS and redeploy."
            )
        else:
            message = "You are not allowed to use discordarr."
        await interaction.response.send_message(message, ephemeral=True)
        return False

    return guard


# ---------------------------------------------------------------------------
# Queue rendering -- shared by /movie, /show's inline progress and /queue.
# ---------------------------------------------------------------------------


def _queue_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        records = payload.get("records")
        if isinstance(records, list):
            return [r for r in records if isinstance(r, dict)]
    return []


def _index_queue(records: list[dict[str, Any]], id_key: str) -> dict[int, dict[str, Any]]:
    """Queue records keyed by movieId/seriesId/albumId, so a search result
    can show its own download progress without a second lookup per item."""
    result: dict[int, dict[str, Any]] = {}
    for record in records:
        rid = record.get(id_key)
        if isinstance(rid, int):
            result[rid] = record
    return result


def _queue_progress_text(record: dict[str, Any] | None) -> str | None:
    """'42% · 00:12:00 left' from a Radarr/Sonarr/Lidarr queue record, or
    `None` when there is nothing usable to show.

    Written against the documented QueueResource shape (`size`/`sizeleft`/
    `timeleft`) rather than a live example: every queue was empty (nothing
    downloading) on all three services at verification time, 2026-09-25 --
    see the PR description. Defensive `.get()`/`isinstance()` throughout so
    a field Sonarr/Radarr/Lidarr rename or omit degrades to "no progress
    shown" instead of an exception.
    """
    if not isinstance(record, dict):
        return None
    parts: list[str] = []
    size = record.get("size")
    sizeleft = record.get("sizeleft")
    if isinstance(size, (int, float)) and size > 0 and isinstance(sizeleft, (int, float)):
        pct = max(0.0, min(100.0, (size - sizeleft) / size * 100))
        parts.append(f"{pct:.0f}%")
    timeleft = record.get("timeleft")
    if isinstance(timeleft, str) and timeleft:
        parts.append(f"{timeleft} left")
    return " · ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Radarr: /movie
# ---------------------------------------------------------------------------


def _movie_state(item: dict[str, Any]) -> str:
    """"have" | "requested" | "none" -- from a Radarr `movie/lookup` item.

    There is NO `hasFile` field on this resource (verified read-only against
    a real Radarr, 2026-09-25 -- see arr_clients.py) -- `id` is only present
    once a title is added, and `movieFileId`/`movieFile` are what actually
    say whether a file has landed.
    """
    if not item.get("id"):
        return "none"
    if item.get("movieFileId") or item.get("movieFile"):
        return "have"
    return "requested"


def _movie_label(item: dict[str, Any], queue_by_id: dict[int, dict[str, Any]]) -> str:
    title = str(item.get("title") or "untitled")
    year = item.get("year")
    year_text = f" ({year})" if year else ""
    state = _movie_state(item)
    if state == "have":
        return f"✅ {title[:80]}{year_text}"
    if state == "requested":
        progress = _queue_progress_text(queue_by_id.get(item.get("id")))
        suffix = f" — {progress}" if progress else ""
        return f"⏳ {title[:80]}{year_text}{suffix}"
    return f"➕ {title[:80]}{year_text}"


async def _add_movie(
    interaction: discord.Interaction,
    movie: dict[str, Any],
    *,
    client: RadarrClient,
    setup: "_ServiceSetup",
    limiter: AddRateLimiter,
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    wait = limiter.try_consume(_actor(interaction))
    if wait is not None:
        await interaction.followup.send(rate_limit_message("adds", wait), ephemeral=True)
        return
    # Re-checked against the same state used to decide the button's enabled
    # state, not trusted from it -- the same defence ReleaseView/EbookView
    # use against a stale client-side press reaching an action its slot was
    # disabled for (see discord_bot.py's `_resolve_page_item` docstring).
    if _movie_state(movie) != "none":
        await interaction.followup.send(
            f"**{movie.get('title') or 'That movie'}** is already in the library. Nothing was added.",
            ephemeral=True,
        )
        return
    error = await setup.ensure()
    if error:
        await interaction.followup.send(f"Radarr isn't fully set up here: {error}", ephemeral=True)
        return
    payload = {
        **movie,
        "qualityProfileId": setup.quality_profile_id,
        "rootFolderPath": setup.root_folder,
        "monitored": True,
        "minimumAvailability": "released",
        "addOptions": {"searchForMovie": True},
    }
    title = str(movie.get("title") or "that movie")
    try:
        await asyncio.to_thread(client.add_movie, payload)
    except ServiceError:
        await interaction.followup.send(f"Radarr could not add **{title}**.", ephemeral=True)
        return
    year = movie.get("year")
    year_text = f" ({year})" if year else ""
    await interaction.followup.send(
        f"Requested **{title}{year_text}**. Radarr is now searching for it.", ephemeral=True
    )


# ---------------------------------------------------------------------------
# Sonarr: /show
# ---------------------------------------------------------------------------


def _show_state(item: dict[str, Any]) -> str:
    """"have" | "requested" | "none" -- from a Sonarr `series/lookup` item.

    Same absent-`id`-until-added shape as Radarr's movie lookup. "have" is
    read as "at least one downloaded episode" (`episodeFileCount`), since a
    partially-downloaded show is still meaningfully different from a bare
    request with nothing landed yet.
    """
    if not item.get("id"):
        return "none"
    stats = item.get("statistics")
    if isinstance(stats, dict) and stats.get("episodeFileCount"):
        return "have"
    return "requested"


def _show_label(item: dict[str, Any], queue_by_id: dict[int, dict[str, Any]]) -> str:
    title = str(item.get("title") or "untitled")
    year = item.get("year")
    year_text = f" ({year})" if year else ""
    state = _show_state(item)
    if state == "have":
        return f"✅ {title[:80]}{year_text}"
    if state == "requested":
        progress = _queue_progress_text(queue_by_id.get(item.get("id")))
        suffix = f" — {progress}" if progress else ""
        return f"⏳ {title[:80]}{year_text}{suffix}"
    return f"➕ {title[:80]}{year_text}"


def _is_big_show(item: dict[str, Any], season_threshold: int, episode_threshold: int) -> bool:
    """Whether a Sonarr add must stop for confirmation instead of queuing on
    the first press.

    Mirrors discord_bot.py's `_needs_confirmation` in both shape and
    philosophy: a season count that cannot be read is treated as the RISKY
    case, not exempted from the gate -- the same call `_needs_confirmation`
    makes for a release with no usable size.

    The episode branch is checked anyway even though it cannot currently
    fire against a real Sonarr: `statistics.totalEpisodeCount` came back 0
    for every show tried at verification time (2026-09-25), five real
    15-to-38-season shows included -- SkyHook's lookup does not populate it,
    only `statistics.seasonCount` is real. Kept so this starts working the
    day that changes, instead of needing a second patch to notice it did.
    """
    stats = item.get("statistics")
    season_count = stats.get("seasonCount") if isinstance(stats, dict) else None
    if not isinstance(season_count, (int, float)):
        return True
    if season_count > season_threshold:
        return True
    episode_count = stats.get("totalEpisodeCount") if isinstance(stats, dict) else None
    if isinstance(episode_count, (int, float)) and episode_count > episode_threshold:
        return True
    return False


class _ConfirmAddShowView(discord.ui.View):
    """Second confirmation for one big show `_RequestView` has already
    flagged -- same shape as discord_bot.py's `_ConfirmGrabView`, and for the
    same reason: the warning is a NEW ephemeral reply with its own component
    row, so a second, single-item view is the minimum needed to carry a
    Confirm/Cancel pair for it.
    """

    def __init__(
        self,
        show: dict[str, Any],
        guard: Callable[[discord.Interaction], Awaitable[bool]],
        add_fn: Callable[[discord.Interaction, dict[str, Any]], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=900)
        self.show = show
        self.guard = guard
        self.add_fn = add_fn

    @discord.ui.button(label="Request anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.response.is_done():
            return
        # Re-checked HERE, not assumed from the /show press that led to
        # this message -- same reasoning as _ConfirmGrabView.confirm: a role
        # can be revoked during the up-to-15-minute window this view stays
        # alive.
        if not await self.guard(interaction):
            return
        await self.add_fn(interaction, self.show)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.response.is_done():
            return
        await interaction.response.edit_message(content="Cancelled -- nothing was requested.", view=None)


async def _add_show(
    interaction: discord.Interaction,
    show: dict[str, Any],
    *,
    client: SonarrClient,
    setup: "_ServiceSetup",
    limiter: AddRateLimiter,
) -> None:
    """The actual add -- shared by a direct Request press (small show) and
    `_ConfirmAddShowView`'s confirmed one (big show), the same way
    discord_bot.py's `_queue_grab` is shared by ReleaseView's immediate press
    and `_ConfirmGrabView`'s confirmed one."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    wait = limiter.try_consume(_actor(interaction))
    if wait is not None:
        await interaction.followup.send(rate_limit_message("adds", wait), ephemeral=True)
        return
    if _show_state(show) != "none":
        await interaction.followup.send(
            f"**{show.get('title') or 'That show'}** is already in the library. Nothing was added.",
            ephemeral=True,
        )
        return
    error = await setup.ensure()
    if error:
        await interaction.followup.send(f"Sonarr isn't fully set up here: {error}", ephemeral=True)
        return
    payload = {
        **show,
        "qualityProfileId": setup.quality_profile_id,
        "rootFolderPath": setup.root_folder,
        "seasonFolder": True,
        "monitored": True,
        # Sonarr v4 dropped language profiles -- no languageProfileId here,
        # even though the lookup resource still carries one (see
        # arr_clients.py's module docstring).
        "addOptions": {"monitor": "all", "searchForMissingEpisodes": True},
    }
    title = str(show.get("title") or "that show")
    try:
        await asyncio.to_thread(client.add_series, payload)
    except ServiceError:
        await interaction.followup.send(f"Sonarr could not add **{title}**.", ephemeral=True)
        return
    await interaction.followup.send(
        f"Requested **{title}**. Sonarr is now searching for it.", ephemeral=True
    )


async def _request_show(
    interaction: discord.Interaction,
    show: dict[str, Any],
    *,
    client: SonarrClient,
    setup: "_ServiceSetup",
    limiter: AddRateLimiter,
    season_threshold: int,
    episode_threshold: int,
    guard: Callable[[discord.Interaction], Awaitable[bool]],
) -> None:
    """Entry point for `/show`'s Request button: gate big shows behind a
    confirmation, add everything else directly."""
    if interaction.response.is_done():
        return
    if _is_big_show(show, season_threshold, episode_threshold):
        stats = show.get("statistics") if isinstance(show.get("statistics"), dict) else {}
        season_count = stats.get("seasonCount") if isinstance(stats, dict) else None
        season_text = (
            f"{int(season_count)} seasons"
            if isinstance(season_count, (int, float))
            else "an unusually large number of seasons"
        )
        add_fn = functools.partial(_add_show, client=client, setup=setup, limiter=limiter)
        await interaction.response.send_message(
            f"**{show.get('title') or 'That show'}** has {season_text} -- at or above the "
            f"{season_threshold}-season confirmation threshold. A mis-ranked or simply huge "
            "show is one press away from every episode of it queuing at once. Confirm to "
            "request all of it anyway.",
            view=_ConfirmAddShowView(show, guard, add_fn),
            ephemeral=True,
        )
        return
    await _add_show(interaction, show, client=client, setup=setup, limiter=limiter)


# ---------------------------------------------------------------------------
# Lidarr: /music (album-level requests)
# ---------------------------------------------------------------------------


def _album_artist_name(item: dict[str, Any]) -> str:
    artist = item.get("artist")
    if isinstance(artist, dict) and artist.get("artistName"):
        return str(artist["artistName"])
    return "unknown artist"


def _album_artist_id(item: dict[str, Any]) -> int | None:
    """The artist's LOCAL Lidarr id if it already has one, from either shape
    an album/lookup result can carry it in -- the top-level `artistId` the
    real API returns, or the embedded `artist.id` -- checked in that order
    but either is authoritative."""
    artist_id = item.get("artistId")
    if isinstance(artist_id, int) and artist_id:
        return artist_id
    artist = item.get("artist")
    if isinstance(artist, dict) and isinstance(artist.get("id"), int) and artist["id"]:
        return artist["id"]
    return None


def _album_state(item: dict[str, Any]) -> str:
    """"requested" | "none" -- from a Lidarr `album/lookup` item.

    Unlike Radarr/Sonarr, there is no third "have" state here: the lookup
    resource carries no per-track file-count field at all (verified
    read-only 2026-09-25 -- only the LOCAL `/api/v1/album?artistId=`
    listing has a `statistics` block with one). `monitored` is the only
    signal the search view has, so "downloaded" and "requested but not
    landed yet" read the same way here; `/queue` is where real progress
    belongs.
    """
    return "requested" if item.get("monitored") else "none"


def _album_label(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "untitled")
    artist = _album_artist_name(item)
    year = None
    release_date = item.get("releaseDate")
    if isinstance(release_date, str) and len(release_date) >= 4 and release_date[:4].isdigit():
        year = release_date[:4]
    year_text = f" ({year})" if year else ""
    icon = "⏳" if _album_state(item) == "requested" else "➕"
    return f"{icon} {title[:60]} — {artist[:40]}{year_text}"


async def _find_local_album(
    client: LidarrClient, artist_id: int, foreign_album_id: Any, attempts: int, delay_seconds: float
) -> int | None:
    """Poll `GET /api/v1/album?artistId=` for the just-added artist's
    albums, bounded, because adding an artist only queues Lidarr's own
    metadata refresh -- the album is not guaranteed to be there on the very
    next read. See media_bot.py's module docstring and the PR description's
    Lidarr verification notes.
    """
    for attempt in range(max(1, attempts)):
        if attempt:
            await asyncio.sleep(delay_seconds)
        try:
            albums = await asyncio.to_thread(client.albums, artist_id)
        except ServiceError:
            return None
        if isinstance(albums, list):
            for candidate in albums:
                if isinstance(candidate, dict) and candidate.get("foreignAlbumId") == foreign_album_id:
                    return candidate.get("id")
    return None


async def _add_album(
    interaction: discord.Interaction,
    album: dict[str, Any],
    *,
    client: LidarrClient,
    setup: "_ServiceSetup",
    limiter: AddRateLimiter,
    poll_attempts: int,
    poll_seconds: float,
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    wait = limiter.try_consume(_actor(interaction))
    if wait is not None:
        await interaction.followup.send(rate_limit_message("adds", wait), ephemeral=True)
        return
    if _album_state(album) != "none":
        await interaction.followup.send(
            f"**{album.get('title') or 'That album'}** is already requested. Nothing was added.",
            ephemeral=True,
        )
        return
    error = await setup.ensure()
    if error:
        await interaction.followup.send(f"Lidarr isn't fully set up here: {error}", ephemeral=True)
        return
    title = str(album.get("title") or "that album")
    artist_name = _album_artist_name(album)
    foreign_album_id = album.get("foreignAlbumId")
    artist_id = _album_artist_id(album)

    if artist_id is None:
        artist = album.get("artist")
        if not isinstance(artist, dict):
            await interaction.followup.send(
                f"**{title}** has no artist information to add.", ephemeral=True
            )
            return
        artist_payload = {
            **artist,
            "qualityProfileId": setup.quality_profile_id,
            "metadataProfileId": setup.metadata_profile_id,
            "rootFolderPath": setup.root_folder,
            "monitored": True,
            # Bulk-monitoring is deliberately OFF here: this is an
            # album-level request (see module docstring), not "get me this
            # artist's whole discography" -- the specific album is monitored
            # and searched individually just below, once it exists locally.
            "monitorNewItems": "none",
            "addOptions": {"monitor": "none", "searchForMissingAlbums": False},
        }
        try:
            added = await asyncio.to_thread(client.add_artist, artist_payload)
        except ServiceError:
            await interaction.followup.send(
                f"Lidarr could not add the artist for **{title}**.", ephemeral=True
            )
            return
        artist_id = added.get("id") if isinstance(added, dict) else None
        if not artist_id:
            await interaction.followup.send(
                f"Added the artist for **{title}**, but Lidarr didn't report its new ID. "
                "Try `/music` again in a minute to request the album.",
                ephemeral=True,
            )
            return

    local_album_id = await _find_local_album(client, artist_id, foreign_album_id, poll_attempts, poll_seconds)
    if local_album_id is None:
        await interaction.followup.send(
            f"Added **{artist_name}** to Lidarr, but **{title}** hasn't shown up in its catalog "
            "yet -- Lidarr refreshes a new artist's albums in the background. Try `/music` again "
            "in a minute to request it.",
            ephemeral=True,
        )
        return
    try:
        await asyncio.to_thread(client.set_album_monitored, local_album_id, True)
        await asyncio.to_thread(client.search_album, local_album_id)
    except ServiceError:
        await interaction.followup.send(f"Lidarr could not start searching for **{title}**.", ephemeral=True)
        return
    await interaction.followup.send(
        f"Requested **{title}** — {artist_name}. Lidarr is searching for it.", ephemeral=True
    )


# ---------------------------------------------------------------------------
# Request button row -- shared by /movie, /show and /music.
# ---------------------------------------------------------------------------


class _RequestView(_PagedView):
    """Request buttons, five per page, over every search result -- same
    shape as discord_bot.py's ReleaseView/EbookView/CancelView (see
    `_resolve_page_item`'s docstring for the bug this guards against: a
    button must resolve against the CURRENT page, not an index frozen when
    the view was built), generalized across /movie, /show and /music rather
    than copied three times, since the paging and dead-slot logic is
    identical for all three -- only the state check and the add action
    differ, and those are already separate functions above.
    """

    page_size: int = _PAGE_SIZE

    def __init__(
        self,
        items: list[dict[str, Any]],
        label_fn: Callable[[dict[str, Any]], str],
        actionable: Callable[[dict[str, Any]], bool],
        guard: Callable[[discord.Interaction], Awaitable[bool]],
        on_request: Callable[[discord.Interaction, dict[str, Any]], Awaitable[None]],
        title: str,
    ) -> None:
        super().__init__(items, title, label_fn, guard)
        self.items = items
        self.actionable = actionable
        self.on_request = on_request
        self._action_buttons: list[discord.ui.Button] = []
        for local_index in range(self.page_size):
            button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                custom_id=f"discordarr:request:{local_index}",
                row=0,
            )
            button.callback = self._make_callback(local_index)  # type: ignore[method-assign]
            self._action_buttons.append(button)
            self.add_item(button)
        self._sync_action_buttons()

    def _sync_action_buttons(self) -> None:
        for local_index, button in enumerate(self._action_buttons):
            item = _resolve_page_item(self.items, self.page, local_index)
            if item is None or not self.actionable(item):
                # A partial last page, or an item already in the library --
                # disable rather than leave a button that would either
                # resolve to nothing or add something a second time.
                button.label = "—"
                button.disabled = True
            else:
                absolute_number = self.page * self.page_size + local_index + 1
                button.label = f"Request {absolute_number}"
                button.disabled = False

    def _make_callback(self, local_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.response.is_done():
                return
            item = _resolve_page_item(self.items, self.page, local_index)
            if item is None or not self.actionable(item):
                # Unreachable via a normal press -- the slot's button is
                # disabled whenever this is true -- but fail loudly rather
                # than let a stale client-side button state through.
                await interaction.response.send_message(
                    "That slot can't be requested.", ephemeral=True
                )
                return
            await self.on_request(interaction, item)

        return callback


# ---------------------------------------------------------------------------
# Root folder / quality profile / metadata profile resolution.
#
# "Fetch and cache these at startup or on first use" (see the PR
# description) means resolved lazily, on the first /movie, /show or /music
# use per service, and kept for the life of the process -- these almost
# never change, and re-fetching them on every command would be a wasted
# round trip against household infrastructure for something static.
# ---------------------------------------------------------------------------


async def _resolve_root_folder(client: Any, configured: str | None, app_name: str) -> tuple[str | None, str | None]:
    """Returns (root_folder_path, error). `configured` is the *_ROOT_FOLDER
    override, by path; left unset, the app's only root folder is used,
    refusing with a clear message rather than guessing when there is more
    than one -- the same "say so, don't guess" style as discord_bot.py's own
    error replies.
    """
    try:
        folders = await asyncio.to_thread(client.root_folders)
    except ServiceError:
        return None, f"{app_name} could not be reached to look up its root folder."
    paths = [f.get("path") for f in folders if isinstance(f, dict) and f.get("path")] if isinstance(folders, list) else []
    if configured:
        if configured in paths:
            return configured, None
        available = ", ".join(paths) or "none configured"
        return None, f"its configured root folder `{configured}` does not match any of its actual root folders ({available})."
    if len(paths) == 1:
        return paths[0], None
    if not paths:
        return None, "it has no root folder configured yet."
    available = ", ".join(paths)
    return None, f"it has more than one root folder ({available}) and none is chosen -- set its *_ROOT_FOLDER."


async def _resolve_quality_profile(
    client: Any,
    configured: str | None,
    app_name: str,
    *,
    library_fetch: Callable[[], Any] | None,
) -> tuple[int | None, str | None]:
    """Returns (quality_profile_id, error). `configured` is the
    *_QUALITY_PROFILE override, by name; left unset, the profile MOST USED
    by items already in the library is used, falling back to the first
    profile when the library (or the fetch itself) is empty.
    """
    try:
        profiles = await asyncio.to_thread(client.quality_profiles)
    except ServiceError:
        return None, f"{app_name} could not be reached to look up its quality profiles."
    profiles = [p for p in profiles if isinstance(p, dict)] if isinstance(profiles, list) else []
    if not profiles:
        return None, "it has no quality profiles configured yet."
    if configured:
        for profile in profiles:
            if str(profile.get("name", "")).casefold() == configured.casefold():
                return profile.get("id"), None
        names = ", ".join(str(p.get("name")) for p in profiles)
        return None, f"its configured quality profile `{configured}` was not found (has: {names})."
    if library_fetch is None:
        return profiles[0].get("id"), None
    try:
        items = await asyncio.to_thread(library_fetch)
    except ServiceError:
        return profiles[0].get("id"), None
    counts: Counter[int] = Counter(
        item.get("qualityProfileId")
        for item in items
        if isinstance(item, dict) and isinstance(item.get("qualityProfileId"), int)
    ) if isinstance(items, list) else Counter()
    if not counts:
        return profiles[0].get("id"), None
    return counts.most_common(1)[0][0], None


async def _resolve_metadata_profile(client: LidarrClient, configured_name: str) -> tuple[int | None, str | None]:
    """Lidarr-only: `configured_name` always has a value (default
    "Standard", see config.py), so unlike the quality profile above there is
    no "most used" fallback to compute -- just a name lookup.
    """
    try:
        profiles = await asyncio.to_thread(client.metadata_profiles)
    except ServiceError:
        return None, "it could not be reached to look up its metadata profiles."
    profiles = [p for p in profiles if isinstance(p, dict)] if isinstance(profiles, list) else []
    for profile in profiles:
        if str(profile.get("name", "")).casefold() == configured_name.casefold():
            return profile.get("id"), None
    names = ", ".join(str(p.get("name")) for p in profiles) or "none configured"
    return None, f"its metadata profile `{configured_name}` was not found (has: {names})."


class _ServiceSetup:
    """Lazily-resolved, cached root folder + quality profile (+ metadata
    profile for Lidarr) for one *arr app.

    Resolved on first use rather than eagerly when the bot starts: building
    the bot must not require every configured service to already be
    reachable (the same reasoning as discord_bot.py's `_idle` -- a slow or
    briefly-down service should not block the OTHER commands from working),
    and resolving eagerly for a service nobody ends up using this session
    would be a wasted call against household infrastructure for nothing.

    The lock makes concurrent callers share one resolution rather than each
    firing their own: two people pressing /movie moments apart, before the
    cache is warm, must not both hit Radarr's rootfolder/qualityprofile/
    movie endpoints.

    Only a SUCCESSFUL resolution is cached. A failure (the service briefly
    unreachable on the very first press) is not -- it is returned as an
    error for that one command to report, and the next attempt resolves
    again from scratch, rather than that service being wedged broken for
    every command until the process restarts.
    """

    def __init__(self, resolve: Callable[["_ServiceSetup"], Awaitable[str | None]]) -> None:
        self._resolve = resolve
        self._lock = asyncio.Lock()
        self._done = False
        self.root_folder: str | None = None
        self.quality_profile_id: int | None = None
        self.metadata_profile_id: int | None = None  # Lidarr only

    async def ensure(self) -> str | None:
        if self._done:
            return None
        async with self._lock:
            if self._done:  # a second caller may have arrived while we waited for the lock
                return None
            error = await self._resolve(self)
            if error:
                return error
            self._done = True
            return None


async def _resolve_radarr_setup(setup: _ServiceSetup, *, client: RadarrClient, settings: Settings) -> str | None:
    root_folder, error = await _resolve_root_folder(client, settings.radarr_root_folder, "Radarr")
    if error:
        return error
    profile_id, error = await _resolve_quality_profile(
        client, settings.radarr_quality_profile, "Radarr", library_fetch=client.movies
    )
    if error:
        return error
    setup.root_folder = root_folder
    setup.quality_profile_id = profile_id
    return None


async def _resolve_sonarr_setup(setup: _ServiceSetup, *, client: SonarrClient, settings: Settings) -> str | None:
    root_folder, error = await _resolve_root_folder(client, settings.sonarr_root_folder, "Sonarr")
    if error:
        return error
    profile_id, error = await _resolve_quality_profile(
        client, settings.sonarr_quality_profile, "Sonarr", library_fetch=client.series
    )
    if error:
        return error
    setup.root_folder = root_folder
    setup.quality_profile_id = profile_id
    return None


async def _resolve_lidarr_setup(setup: _ServiceSetup, *, client: LidarrClient, settings: Settings) -> str | None:
    root_folder, error = await _resolve_root_folder(client, settings.lidarr_root_folder, "Lidarr")
    if error:
        return error
    profile_id, error = await _resolve_quality_profile(
        client, settings.lidarr_quality_profile, "Lidarr", library_fetch=client.artists
    )
    if error:
        return error
    metadata_id, error = await _resolve_metadata_profile(client, settings.lidarr_metadata_profile)
    if error:
        return error
    setup.root_folder = root_folder
    setup.quality_profile_id = profile_id
    setup.metadata_profile_id = metadata_id
    return None


# ---------------------------------------------------------------------------
# /queue
# ---------------------------------------------------------------------------


async def _queue_section(name: str, client: Any | None) -> str:
    if client is None:
        return f"**{name}**: not set up here."
    try:
        payload = await asyncio.to_thread(client.queue)
    except ServiceError:
        return f"**{name}**: queue is unavailable."
    records = _queue_records(payload)[:10]
    if not records:
        return f"**{name}**: nothing downloading."
    lines = [f"**{name}**:"]
    for record in records:
        title = str(
            record.get("title")
            or (record.get("movie") or {}).get("title")
            or (record.get("series") or {}).get("title")
            or (record.get("album") or {}).get("title")
            or "unknown"
        )
        progress = _queue_progress_text(record) or "starting"
        lines.append(f"• {title[:80]} — {progress}")
    return "\n".join(lines)


async def _render_queue(radarr: Any | None, sonarr: Any | None, lidarr: Any | None) -> str:
    if radarr is None and sonarr is None and lidarr is None:
        return "Movies, shows, and music aren't set up here yet."
    sections = [
        await _queue_section("Movies", radarr),
        await _queue_section("Shows", sonarr),
        await _queue_section("Music", lidarr),
    ]
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Command registration.
# ---------------------------------------------------------------------------


def install_media_commands(
    bot: commands.Bot,
    settings: Settings,
    allowed_roles: set[int],
    *,
    limiter: AddRateLimiter | None = None,
    radarr_client: RadarrClient | None = None,
    sonarr_client: SonarrClient | None = None,
    lidarr_client: LidarrClient | None = None,
) -> None:
    """Add `/movie`, `/show`, `/music` and `/queue` to `bot`'s command tree.

    `radarr_client`/`sonarr_client`/`lidarr_client` are normally built here
    from `settings`, but can be injected -- the same escape hatch
    `HttpClient.breaker` gives clients.py -- so tests can point a command at
    a fake without a real `Settings.radarr_url` to construct one from.
    """
    guard = _build_guard(allowed_roles)
    limiter = limiter or AddRateLimiter(settings.add_rate_limit_max, settings.add_rate_limit_window_seconds)

    if radarr_client is None and settings.radarr_configured():
        radarr_client = RadarrClient(
            settings.radarr_url, settings.radarr_api_key,
            timeout=settings.arr_http_timeout, retries=settings.arr_http_retries,
        )
    if sonarr_client is None and settings.sonarr_configured():
        sonarr_client = SonarrClient(
            settings.sonarr_url, settings.sonarr_api_key,
            timeout=settings.arr_http_timeout, retries=settings.arr_http_retries,
        )
    if lidarr_client is None and settings.lidarr_configured():
        lidarr_client = LidarrClient(
            settings.lidarr_url, settings.lidarr_api_key,
            timeout=settings.arr_http_timeout, retries=settings.arr_http_retries,
        )

    radarr_setup = (
        _ServiceSetup(functools.partial(_resolve_radarr_setup, client=radarr_client, settings=settings))
        if radarr_client is not None
        else None
    )
    sonarr_setup = (
        _ServiceSetup(functools.partial(_resolve_sonarr_setup, client=sonarr_client, settings=settings))
        if sonarr_client is not None
        else None
    )
    lidarr_setup = (
        _ServiceSetup(functools.partial(_resolve_lidarr_setup, client=lidarr_client, settings=settings))
        if lidarr_client is not None
        else None
    )

    @bot.tree.command(name="movie", description="Search Radarr for a movie, or request one")
    @app_commands.describe(query="Title to search for")
    async def movie(interaction: discord.Interaction, query: str) -> None:
        if not await guard(interaction):
            return
        if radarr_client is None or radarr_setup is None:
            await interaction.response.send_message(
                "Movies aren't set up here. Ask the operator to configure RADARR_URL and "
                "RADARR_API_KEY.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            results = await asyncio.to_thread(radarr_client.lookup, query)
        except ServiceError:
            await interaction.followup.send("Radarr search is unavailable.", ephemeral=True)
            return
        movies = [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []
        if not movies:
            await interaction.followup.send("No matching movies were found.", ephemeral=True)
            return
        try:
            queue_payload = await asyncio.to_thread(radarr_client.queue)
            queue_by_id = _index_queue(_queue_records(queue_payload), "movieId")
        except ServiceError:
            queue_by_id = {}
        on_request = functools.partial(_add_movie, client=radarr_client, setup=radarr_setup, limiter=limiter)
        view = _RequestView(
            movies,
            lambda item: _movie_label(item, queue_by_id),
            lambda item: _movie_state(item) == "none",
            guard,
            on_request,
            f"Movie results for {query}",
        )
        sent = await interaction.followup.send(embed=view.render_embed(), view=view, ephemeral=True)
        view.message = sent

    @bot.tree.command(name="show", description="Search Sonarr for a TV show, or request one")
    @app_commands.describe(query="Title to search for")
    async def show(interaction: discord.Interaction, query: str) -> None:
        if not await guard(interaction):
            return
        if sonarr_client is None or sonarr_setup is None:
            await interaction.response.send_message(
                "Shows aren't set up here. Ask the operator to configure SONARR_URL and "
                "SONARR_API_KEY.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            results = await asyncio.to_thread(sonarr_client.lookup, query)
        except ServiceError:
            await interaction.followup.send("Sonarr search is unavailable.", ephemeral=True)
            return
        shows = [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []
        if not shows:
            await interaction.followup.send("No matching shows were found.", ephemeral=True)
            return
        try:
            queue_payload = await asyncio.to_thread(sonarr_client.queue)
            queue_by_id = _index_queue(_queue_records(queue_payload), "seriesId")
        except ServiceError:
            queue_by_id = {}
        on_request = functools.partial(
            _request_show,
            client=sonarr_client,
            setup=sonarr_setup,
            limiter=limiter,
            season_threshold=settings.sonarr_big_show_season_threshold,
            episode_threshold=settings.sonarr_big_show_episode_threshold,
            guard=guard,
        )
        view = _RequestView(
            shows,
            lambda item: _show_label(item, queue_by_id),
            lambda item: _show_state(item) == "none",
            guard,
            on_request,
            f"Show results for {query}",
        )
        sent = await interaction.followup.send(embed=view.render_embed(), view=view, ephemeral=True)
        view.message = sent

    @bot.tree.command(name="music", description="Search Lidarr for an album, or request one")
    @app_commands.describe(query="Album or artist to search for")
    async def music(interaction: discord.Interaction, query: str) -> None:
        if not await guard(interaction):
            return
        if lidarr_client is None or lidarr_setup is None:
            await interaction.response.send_message(
                "Music isn't set up here. Ask the operator to configure LIDARR_URL and "
                "LIDARR_API_KEY.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            results = await asyncio.to_thread(lidarr_client.lookup_album, query)
        except ServiceError:
            await interaction.followup.send("Lidarr search is unavailable.", ephemeral=True)
            return
        albums = [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []
        if not albums:
            await interaction.followup.send("No matching albums were found.", ephemeral=True)
            return
        on_request = functools.partial(
            _add_album,
            client=lidarr_client,
            setup=lidarr_setup,
            limiter=limiter,
            poll_attempts=settings.lidarr_album_poll_attempts,
            poll_seconds=settings.lidarr_album_poll_seconds,
        )
        view = _RequestView(
            albums,
            _album_label,
            lambda item: _album_state(item) == "none",
            guard,
            on_request,
            f"Album results for {query}",
        )
        sent = await interaction.followup.send(embed=view.render_embed(), view=view, ephemeral=True)
        view.message = sent

    @bot.tree.command(name="queue", description="Show what Radarr, Sonarr and Lidarr are downloading right now")
    async def queue_cmd(interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await _render_queue(radarr_client, sonarr_client, lidarr_client)
        await interaction.followup.send(message, ephemeral=True)

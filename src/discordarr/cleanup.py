"""/cleanup: library titles nobody has watched in a year, and deleting the ones you pick.

Sullivan's HDD pool, the library Emby reads, was 91% full on 2026-10-17. The
automatic part of the cleanup (Sullivan's media-janitor) only ever touches
download leftovers; whether a film or show is still wanted is a person's call,
so this lists the candidates and leaves the deleting to a button press.

- Watch history comes from Emby: every user's played and partly played items,
  each with its LastPlayedDate. A play anywhere inside a title's folder counts
  for the whole title, so one episode watched keeps the show.
- A title qualifies when Radarr or Sonarr added it more than
  `cleanup_unwatched_days` ago (365) and nobody has played it since then, or
  ever. The biggest come first.
- Deleting goes through Radarr or Sonarr, files and all, with an import
  exclusion so it is not downloaded again. Emby drops it on its next library
  refresh.
- Only Discord users in DISCORDARR_PURGE_USER_IDS can delete. Everyone the bot
  allows can see the list.

Emby and the arrs see the library under the same paths (/movies, /shows),
which is what lets a played file be matched to its title. If Emby reports no
watch history at all, nothing is listed: a broken match would make every title
look unwatched, and that list must never be the one offered for deletion.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands

from .clients import HttpClient, ServiceError
from .config import Settings
from .discord_bot import _PAGE_SIZE, _PagedView, _resolve_page_item

GB = 1_000_000_000


class EmbyClient:
    """The two Emby calls /cleanup needs, read-only."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0, retries: int = 2):
        self.http = HttpClient(base_url, service="emby", timeout=timeout, retries=retries, backoff=0.25)
        self.headers = {"X-Emby-Token": api_key}

    def users(self) -> list[dict[str, Any]]:
        payload = self.http.request("/emby/Users", headers=self.headers)
        return payload if isinstance(payload, list) else []

    def plays(self, user_id: str, item_type: str) -> list[dict[str, Any]]:
        """Items of `item_type` this user has finished or started."""
        items: list[dict[str, Any]] = []
        for status in ("IsPlayed", "IsResumable"):
            payload = self.http.request(
                f"/emby/Users/{user_id}/Items",
                params={"Recursive": "true", "IncludeItemTypes": item_type, "Filters": status, "Fields": "Path"},
                headers=self.headers,
            )
            if isinstance(payload, dict) and isinstance(payload.get("Items"), list):
                items += [item for item in payload["Items"] if isinstance(item, dict)]
        return items


def parse_time(value: Any) -> datetime | None:
    """An Emby or *arr timestamp, to the second, in UTC. Emby sends seven
    fractional digits, which `fromisoformat` will not take, and neither the
    fraction nor the zone suffix matters at the scale of a year."""
    text = str(value or "")
    try:
        return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def last_played_by_title(title_paths: list[str], plays: list[dict[str, Any]]) -> dict[str, datetime]:
    """For each title folder, the latest play of any file inside it."""
    folders = {str(PurePosixPath(p)): p for p in title_paths if p}
    latest: dict[str, datetime] = {}
    for item in plays:
        played = parse_time((item.get("UserData") or {}).get("LastPlayedDate"))
        path = str(item.get("Path") or "")
        if not played or not path:
            continue
        for parent in PurePosixPath(path).parents:
            title = folders.get(str(parent))
            if title is not None:
                if played > latest.get(title, datetime.min.replace(tzinfo=timezone.utc)):
                    latest[title] = played
                break
    return latest


@dataclass(frozen=True)
class Candidate:
    kind: str  # "movie" | "show"
    arr_id: int
    title: str
    year: int | None
    size: int
    added: datetime
    last_played: datetime | None


def _size_of(item: dict[str, Any]) -> int:
    size = item.get("sizeOnDisk")
    if size is None:
        size = (item.get("statistics") or {}).get("sizeOnDisk")
    return int(size or 0)


def find_unwatched(
    kind: str, items: list[dict[str, Any]], played: dict[str, datetime], now: datetime, days: int
) -> list[Candidate]:
    """Titles with files, added over `days` ago, not played in `days` (or ever). Biggest first."""
    cutoff = now - timedelta(days=days)
    out = []
    for item in items:
        size, added = _size_of(item), parse_time(item.get("added"))
        if size <= 0 or added is None or added > cutoff:
            continue
        last = played.get(str(item.get("path") or ""))
        if last is not None and last > cutoff:
            continue
        out.append(Candidate(kind, int(item["id"]), str(item.get("title") or "untitled"),
                             item.get("year"), size, added, last))
    return sorted(out, key=lambda c: c.size, reverse=True)


def _label(candidate: Candidate) -> str:
    year = f" ({candidate.year})" if candidate.year else ""
    watched = (f"last watched {candidate.last_played:%Y-%m-%d}" if candidate.last_played else "never watched")
    return f"**{candidate.title[:60]}{year}** · {candidate.size / GB:.1f} GB · {watched} · added {candidate.added:%Y-%m-%d}"


def can_delete(user_id: int, settings: Settings) -> bool:
    """Fails closed: with no ids configured, nobody deletes."""
    return bool(settings.purge_user_ids) and user_id in settings.purge_user_ids


async def collect(kind: str, emby: EmbyClient, arr: Any, settings: Settings,
                  now: datetime | None = None) -> tuple[list[Candidate], int] | str:
    """The candidates and how many plays backed the check, or why there are none."""
    now = now or datetime.now(timezone.utc)
    emby_type = "Movie" if kind == "movie" else "Episode"
    list_items = arr.movies if kind == "movie" else arr.series
    try:
        items = await asyncio.to_thread(list_items)
        users = await asyncio.to_thread(emby.users)
        plays: list[dict[str, Any]] = []
        for user in users:
            plays += await asyncio.to_thread(emby.plays, str(user.get("Id")), emby_type)
    except ServiceError as exc:
        return f"Could not read {'Radarr' if kind == 'movie' else 'Sonarr'} or Emby: {exc.message[:200]}"
    if not plays:
        return (
            "Emby reports no watch history at all, so every title would look unwatched. "
            "Nothing is listed until that looks right."
        )
    items = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    played = last_played_by_title([str(i.get("path") or "") for i in items], plays)
    return find_unwatched(kind, items, played, now, settings.cleanup_unwatched_days), len(plays)


class CleanupView(_PagedView):
    """Delete buttons over the unwatched titles, for the people allowed to use them."""

    page_size: int = _PAGE_SIZE

    def __init__(self, candidates: list[Candidate], title: str, arr: Any, settings: Settings,
                 guard: Callable[[discord.Interaction], Awaitable[bool]]):
        super().__init__(candidates, title, _label, guard)
        self.candidates = candidates
        self.arr = arr
        self.settings = settings
        self._buttons: list[discord.ui.Button] = []
        for local_index in range(self.page_size):
            button = discord.ui.Button(style=discord.ButtonStyle.danger, row=0,
                                       custom_id=f"discordarr:cleanup:{local_index}")
            button.callback = self._make_callback(local_index)  # type: ignore[method-assign]
            self._buttons.append(button)
            self.add_item(button)
        self._sync_action_buttons()

    def _sync_action_buttons(self) -> None:
        for local_index, button in enumerate(self._buttons):
            candidate = _resolve_page_item(self.candidates, self.page, local_index)
            button.disabled = candidate is None
            button.label = "—" if candidate is None else f"Delete {self.page * self.page_size + local_index + 1}"

    def _make_callback(self, local_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.response.is_done() or not await self.guard(interaction):
                return
            candidate = _resolve_page_item(self.candidates, self.page, local_index)
            if candidate is None:
                await interaction.response.send_message("That slot is empty on this page.", ephemeral=True)
                return
            if not can_delete(interaction.user.id, self.settings):
                await interaction.response.send_message(
                    "Deleting is limited to the people listed in DISCORDARR_PURGE_USER_IDS.", ephemeral=True
                )
                return
            app = "Radarr" if candidate.kind == "movie" else "Sonarr"
            await interaction.response.send_message(
                f"Delete {_label(candidate)} and its files? {app} will not download it again.",
                view=ConfirmDeleteView(candidate, self.arr, self.settings, self.guard),
                ephemeral=True,
            )

        return callback


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, candidate: Candidate, arr: Any, settings: Settings,
                 guard: Callable[[discord.Interaction], Awaitable[bool]]):
        super().__init__(timeout=300)
        self.candidate, self.arr, self.settings, self.guard = candidate, arr, settings, guard
        self.done = False

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Checked again here: a role or the purge list can change in the
        # minutes this message stays up, and THIS press is the deletion.
        if self.done or not await self.guard(interaction):
            return
        if not can_delete(interaction.user.id, self.settings):
            await interaction.response.edit_message(content="Deleting is not allowed for you.", view=None)
            return
        self.done = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        c = self.candidate
        delete = self.arr.delete_movie if c.kind == "movie" else self.arr.delete_series
        try:
            await asyncio.to_thread(delete, c.arr_id)
        except ServiceError as exc:
            self.done = False
            await interaction.followup.send(f"{c.title} was not deleted: {exc.message[:200]}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Deleted **{c.title}**, freeing {c.size / GB:.1f} GB. Emby drops it on its next library refresh.",
            ephemeral=True,
        )

    @discord.ui.button(label="Keep", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Kept. Nothing was deleted.", view=None)


def install_cleanup_command(
    bot: discord.ext.commands.Bot,
    settings: Settings,
    guard: Callable[[discord.Interaction], Awaitable[bool]],
    *,
    radarr_client: Any | None,
    sonarr_client: Any | None,
    emby_client: EmbyClient | None = None,
) -> None:
    if emby_client is None and settings.emby_configured():
        emby_client = EmbyClient(settings.emby_url, settings.emby_api_key,  # type: ignore[arg-type]
                                 timeout=max(settings.arr_http_timeout, 30.0), retries=settings.arr_http_retries)

    @bot.tree.command(name="cleanup", description="List movies or shows nobody has watched in a year, and delete the ones you pick")
    @app_commands.describe(type="Movies or shows")
    @app_commands.choices(type=[
        app_commands.Choice(name="Movies", value="movie"),
        app_commands.Choice(name="Shows", value="show"),
    ])
    async def cleanup(interaction: discord.Interaction, type: app_commands.Choice[str]) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        arr = radarr_client if type.value == "movie" else sonarr_client
        if emby_client is None or arr is None:
            await interaction.followup.send(
                "Cleanup needs Emby (EMBY_URL, EMBY_API_KEY) and "
                f"{'Radarr' if type.value == 'movie' else 'Sonarr'} configured.",
                ephemeral=True,
            )
            return
        result = await collect(type.value, emby_client, arr, settings)
        if isinstance(result, str):
            await interaction.followup.send(result, ephemeral=True)
            return
        candidates, plays = result
        days = settings.cleanup_unwatched_days
        if not candidates:
            await interaction.followup.send(
                f"Every {type.name.lower()[:-1]} added over {days} days ago has been watched since. "
                f"({plays} plays checked.)",
                ephemeral=True,
            )
            return
        total = sum(c.size for c in candidates) / GB
        title = f"{len(candidates)} {type.name.lower()} unwatched for {days} days · {total:.0f} GB"
        view = CleanupView(candidates, title, arr, settings, guard)
        note = (None if can_delete(interaction.user.id, settings)
                else "You can look, but deleting is limited to DISCORDARR_PURGE_USER_IDS.")
        sent = await interaction.followup.send(content=note, embed=view.render_embed(), view=view, ephemeral=True)
        view.message = sent

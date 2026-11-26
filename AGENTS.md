# AGENTS.md — discordarr

**Superseded.** This Discord bot was merged with `shelfmark` on 2026-11-20 and the combined service renamed **`managarr`** — the live bot runs from [nuniesmith/managarr](https://github.com/nuniesmith/managarr) on freddy. This repo is the pre-merge source, kept for history.

## Shared context

Read the shared agent context first: `soul.md` and `memory.md` (especially §3) in the [agents repo](https://github.com/nuniesmith/agents).

## Rules

- **Never run this bot.** It shares its Discord token and application with the live managarr bot, and Discord lets exactly one process own a bot's slash commands at a time — two processes on one token fight over the commands and break the household's bot.
- Don't deploy from here and don't fix live behaviour here; bot work belongs in `managarr`.

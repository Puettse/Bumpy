# Bumpy

A simple Discord bot that runs `/bump` every 2 hours and 1 minute.

## Commands
- `$start now` → Start bumping
- `$end now` → Stop bumping
- `$assign channel: (channel id)` → Set bump channel
- `$log channel: (channel id)` → Set log channel
- `$restart` → Restart bump loop

## Deployment on Railway
1. Create a new Railway project.
2. Link this repo.
3. Add a Railway variable:
   - Key: `DISCORD_TOKEN`
   - Value: your bot token
4. Deploy — Bumpy will go live.

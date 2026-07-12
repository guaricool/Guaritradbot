# Backup strategy (Sprint 46R audit M11.5 + B7)

The bot's runtime state lives in two Docker named volumes on the VPS:

- `wyn2ah6rflg6ufwzpvzk436f_bot-audit/` — the forensic record (every
  `audit.jsonl` line, every Telegram retry failure, every mode/pause
  override, the auth token signing key)
- `wyn2ah6rflg6ufwzpvzk436f_bot-data-store/` — live trading state
  (open `positions.json`, `equity_state.json` for the drawdown
  kill-switch, `drawdown_kill_state.json`, the historical-price CSV
  caches the bot has warmed up)

Audit M11.5 + B7 found no backup strategy for either. A disk failure
or accidental `docker volume rm` would lose both — the audit log
is the ONLY record of what the bot did, and `positions.json` is
the only record of what the bot currently holds.

## What runs daily

A cron job at `03:17 UTC` runs `/root/scripts/backup_bot_state.sh`,
which:

1. `tar czf /backups/bot-audit-YYYYMMDD-HHMMSS.tgz` — the entire
   `_data/` of the bot-audit volume
2. `tar czf /backups/bot-data-store-YYYYMMDD-HHMMSS.tgz` — same for
   bot-data-store
3. Prunes `.tgz` older than 14 days (so we keep ~14 of each, ~14MB
   total, well under any reasonable disk budget)
4. Logs to `/var/log/guaritradbot-backup.log`

Cron is registered in `/etc/cron.d/guaritradbot-backup`.

## Why reading while the bot is running is safe

- `audit.jsonl` writes go through `flock(fd, LOCK_EX)` per line
  (`src/safety/audit_ledger.py`). Worst case during backup: we read
  a half-written line. The audit ledger's malformed-line detector
  (same file) handles that on the next bot boot — the malformed
  line is quarantined to `audit.jsonl.malformed` and the rest of
  the ledger stays valid.
- `positions.json`, `equity_state.json`, and `drawdown_kill_state.json`
  are all written via `src/core/atomic_write.py` (Sprint 46R audit
  B8): open tmp, write + `fsync`, `os.replace()`. The `os.replace()`
  is atomic on the container's overlay filesystem, so backup always
  sees either the old or the new full file, never a partial one.

So we don't need to stop the bot during the backup. Tested with a
manual run; the resulting tarballs contain the same files a
filesystem-level snapshot would.

## Recovering from a backup

```bash
# 1. Stop the bot container
docker stop guaritradbot-wyn2ah6rflg6ufwzpvzk436f-XXXXX

# 2. Pick the latest backup
ls -lt /backups/bot-audit-*.tgz | head -1
ls -lt /backups/bot-data-store-*.tgz | head -1

# 3. Restore (this OVERWRITES live state)
docker run --rm -v wyn2ah6rflg6ufwzpvzk436f_bot-audit:/dest \
    -v /backups:/src alpine tar xzf /src/bot-audit-YYYYMMDD-HHMMSS.tgz -C /dest
docker run --rm -v wyn2ah6rflg6ufwzpvzk436f_bot-data-store:/dest \
    -v /backups:/src alpine tar xzf /src/bot-data-store-YYYYMMDD-HHMMSS.tgz -C /dest

# 4. Restart the bot (Coolify's restart: unless-stopped brings it back)
docker start guaritradbot-wyn2ah6rflg6ufwzpvzk436f-XXXXX
```

## What's NOT in this backup

- The bot's **source code** (clone from `git clone
  https://github.com/guaricool/Guaritradbot`)
- The **dashboard** container (rebuild via Coolify webhook on push)
- `/etc/cron.d/guaritradbot-backup` and `/root/scripts/backup_bot_state.sh`
  themselves — Carlos should mirror these to a separate dotfiles repo
  or to `/backups/` (the cron script excludes itself, but the
  intent is to back up the script's settings as part of the VPS
  config, not the bot's runtime state)
- `/backups/` itself — the backup tarballs are on the same VPS
  disk as the volumes. A second VPS disk or offsite (B2, S3) is
  the next step, but out of scope for this commit. Carlos should
  add `rclone sync /backups/ b2:guaritradbot-backups/` to a
  weekly cron when ready.

## When the cron was installed

Sprint 46R, 2026-07-12. Initial manual run: 44618 bytes for
bot-audit, 504322 bytes for bot-data-store (the extra size is the
historical price CSVs the bot caches; not critical but nice to
have).

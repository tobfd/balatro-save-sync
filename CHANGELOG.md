# Changelog

## 1.1.0 - 2026-11-08

Linux support and a safer first sync, based on feedback from a Linux (Steam + Proton) user.

- **Safer first sync:** a device that meets a profile slot for the first time compares how many rounds were played on it (`career_stats` in `profile.jkr`). If it played more, it keeps its saves and uploads them instead of adopting the server state, so a fresh device that happened to sync first can't overwrite real progress
- **Status page login:** log in with a form, a cookie keeps you logged in. The token no longer appears in URLs, links or bookmarks; old `/?token=` bookmarks still work and redirect. The sync API now only accepts the `X-Sync-Token` header
- **Docker healthcheck** in `compose.yaml`
- **Linux:** the patcher finds Steam in `~/.steam/steam`, `~/.steam/root`, `~/.steam/debian-installation`, `~/.local/share/Steam` and the Flatpak folder; new `make_patched_exe.sh`; README section for Steam + Proton with the known balatro-mobile-maker issues on Linux
- README: clearer notes on the first sync and on modded saves

### Upgrading from 1.0.0

- **Server:** replace `server.py`, add the `healthcheck` from the new `compose.yaml`, redeploy, then log in once on the status page
- **PC:** replace `balatro_sync.lua`, `boot.lua` and `lovely.toml` in your `BalatroSync` folder, **keep your `config.lua`**
- **Android:** devices that already synced keep working. Build a new APK for new devices, or to give existing ones the new first-sync check

## 1.0.0 - 2026-11-08

First release.

- **In-game sync** between Balatro on Steam (Lovely mod) and Android builds from balatro-mobile-maker, through a small server in your home network
- Pulls newer saves **before** Balatro reads them, uploads changes within ~10 seconds and again when you quit or switch apps
- Notices newer progress from another device while the game is open: Android reloads itself, the PC shows "Newer save found"
- A game left open without input never uploads, and conflicts are decided by when you last actually played, not by file times
- A device that syncs for the first time adopts the existing state and backs up its old saves
- Safety nets: local backups (last 20) and server history (last 50 versions per file) with restore buttons
- Server: one Python file without dependencies, Docker compose included
- Android patcher that prepares the `Balatro.exe` for balatro-mobile-maker, with its own device name per device

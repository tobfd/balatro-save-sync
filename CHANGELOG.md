# Changelog

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

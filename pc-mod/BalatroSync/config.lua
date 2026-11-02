-- BalatroSync settings.
-- The Android patcher reads this file too and only swaps device_name.
return {
  -- Address of the sync server in your home network (use the IP, not a hostname).
  server_url = "http://192.168.1.10:8765",

  -- Must match SYNC_TOKEN in the server's compose.yaml.
  token = "CHANGE_ME",

  -- Shown on the server status page and in the "newer save found" message.
  device_name = "PC",

  -- Profile slots to sync. If you play modded content on PC only,
  -- you can limit syncing to the slot you also play on mobile, e.g. "1".
  profiles = "1,2,3",

  -- settings.jkr holds window/graphics settings; syncing it between PC and
  -- tablet is usually not wanted.
  sync_settings = false,

  -- Seconds between background checks, and network timeout in seconds.
  interval = 10,
  timeout = 2,

  -- Small status message in the bottom-left corner after start.
  show_toasts = true,

  -- How many local backup snapshots to keep (balatro_sync/backups).
  max_local_backups = 20,
}

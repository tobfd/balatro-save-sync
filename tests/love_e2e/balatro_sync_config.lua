-- Test config driven by environment variables.
if os.getenv("FAKE_ANDROID") then
  love.system.getOS = function() return "Android" end
end
return {
  server_url = os.getenv("SYNC_URL"),
  token = os.getenv("SYNC_TOKEN"),
  device_name = os.getenv("DEVICE") or "Test",
  profiles = "1",
  interval = 3,
  timeout = 1,
}

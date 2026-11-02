-- End-to-end test of the BalatroSync worker logic against a real server.
-- Usage: luajit tests/worker_test.lua <path to balatro_sync.lua> <server url> <token>
local ffi = require("ffi")

local module_path, server_url, token = arg[1], arg[2], arg[3]
local M = dofile(module_path)

---------------------------------------------------------------- helpers ---
local md5
do
  local ok, crypto = pcall(ffi.load, "libcrypto.so.3")
  if ok then
    ffi.cdef("unsigned char *MD5(const unsigned char *d, size_t n, unsigned char *md);")
    md5 = function(s)
      local out = ffi.new("unsigned char[16]")
      crypto.MD5(s, #s, out)
      return ffi.string(out, 16)
    end
  else
    error("libcrypto not found")
  end
end

local function new_channel()
  local q = {}
  return {
    push = function(_, v) q[#q + 1] = v end,
    pop = function() return table.remove(q, 1) end,
    peek = function() return q[1] end,
    demand = function() return table.remove(q, 1) end,
    clear = function() for i = #q, 1, -1 do q[i] = nil end end,
    items = q,
  }
end

local clock_base = os.time() - 10000

local function make_device(name, overrides)
  local files, dirs = {}, {}
  local dev = { name = name, files = files, clock = clock_base, channels = {} }

  local fs = {}
  function fs.getInfo(path, filter)
    local f = files[path]
    if f then
      if filter and filter ~= "file" then return nil end
      return { type = "file", size = #f.content, modtime = f.modtime }
    end
    if dirs[path] then
      if filter and filter ~= "directory" then return nil end
      return { type = "directory" }
    end
    return nil
  end
  function fs.read(path)
    local f = files[path]
    if not f then return nil, "missing" end
    return f.content, #f.content
  end
  function fs.write(path, content)
    files[path] = { content = content, modtime = dev.clock }
    return true
  end
  function fs.remove(path)
    if files[path] then files[path] = nil; return true end
    if dirs[path] then
      local prefix = path .. "/"
      for p in pairs(files) do if p:sub(1, #prefix) == prefix then return false end end
      for d in pairs(dirs) do if d:sub(1, #prefix) == prefix then return false end end
      dirs[path] = nil
      return true
    end
    return false
  end
  function fs.createDirectory(path)
    local acc
    for part in path:gmatch("[^/]+") do
      acc = acc and (acc .. "/" .. part) or part
      dirs[acc] = true
    end
    return true
  end
  function fs.getDirectoryItems(path)
    local set, prefix = {}, path .. "/"
    for p in pairs(files) do
      if p:sub(1, #prefix) == prefix then set[p:sub(#prefix + 1):match("^[^/]+")] = true end
    end
    for d in pairs(dirs) do
      if d:sub(1, #prefix) == prefix then set[d:sub(#prefix + 1):match("^[^/]+")] = true end
    end
    local out = {}
    for k in pairs(set) do out[#out + 1] = k end
    return out
  end

  local data = {
    hash = function(_, s) return md5(s) end,
    encode = function(_, _, s)
      return (s:gsub(".", function(c) return ("%02x"):format(c:byte()) end))
    end,
    decompress = function(_, _, s)
      if s:sub(1, 2) == "Z:" then return s:sub(3) end
      error("not deflate data")
    end,
  }

  local love = {
    filesystem = fs,
    data = data,
    timer = { getTime = os.clock, sleep = function() end },
    thread = {
      getChannel = function(cname)
        dev.channels[cname] = dev.channels[cname] or new_channel()
        return dev.channels[cname]
      end,
    },
  }

  -- unique backup snapshot names even within the same second
  local fake_os = setmetatable({
    date = function()
      dev.stamp = (dev.stamp or 0) + 1
      return ("snap%04d"):format(dev.stamp)
    end,
  }, { __index = os })
  local env = setmetatable({ love = love, os = fake_os }, { __index = _G })
  local cfg = {
    server_url = server_url, token = token, device_name = name,
    profiles = "1", interval = 10, timeout = 1, show_toasts = true, max_local_backups = 3,
  }
  for k, v in pairs(overrides or {}) do cfg[k] = v end
  dev.boot = function()
    local chunk = assert(loadstring(M.WORKER_SOURCE, "=worker_" .. name))
    setfenv(chunk, env)
    dev.api = chunk(cfg, true)
  end
  dev.boot()

  -- convenience
  -- write: the game saves without the player doing anything (idle rewrite)
  function dev.write(path, content, dt)
    dev.clock = dev.clock + (dt or 1)
    fs.write(path, content)
  end
  -- play: the player did something, then the game saved
  function dev.play(path, content, act_offset)
    love.thread.getChannel("balatro_sync_activity"):clear()
    love.thread.getChannel("balatro_sync_activity"):push(os.time() + (act_offset or 1))
    if content == nil then
      files[path] = nil
    else
      dev.write(path, content)
    end
  end
  function dev.read(path) return files[path] and files[path].content end
  function dev.startup()
    dev.channels.balatro_sync_go = dev.channels.balatro_sync_go or new_channel()
    dev.channels.balatro_sync_go:push("go")
    return dev.api.handle("startup:1")
  end
  function dev.tick() return dev.api.handle("tick") end
  function dev.events()
    local out = {}
    local c = dev.channels.balatro_sync_events
    while c and c:peek() do out[#out + 1] = c:pop() end
    return out
  end
  function dev.restart()
    -- game restart: new worker instance, persisted files stay
    dev.channels = {}
    dev.boot()
  end
  return dev
end

local passed, failed = 0, 0
local function check(cond, label)
  if cond then
    passed = passed + 1
    print("  ok   " .. label)
  else
    failed = failed + 1
    print("  FAIL " .. label)
  end
end

local function has_event(events, kind, field, value)
  for _, e in ipairs(events) do
    if e.kind == kind and (not field or e[field] == value) then return true end
  end
  return false
end

local function count_backups(dev)
  local n = 0
  for p in pairs(dev.files) do if p:match("^balatro_sync/backups/") then n = n + 1 end end
  return n
end

---------------------------------------------------------------- tests ---
local PC = make_device("PC")
local TAB = make_device("Tablet")

print("1) PC uploads its existing saves on first start")
PC.write("1/profile.jkr", "return {progress=1}")
PC.write("1/meta.jkr", "return {unlocked={}}")
PC.write("1/save.jkr", "Z:return {run=1}")
local r = PC.startup()
check(r.uploaded == 3, "3 files uploaded")
check(has_event(PC.events(), "toast", "text", "Save sync: uploaded local progress"), "toast says uploaded")

print("2) Fresh tablet pulls everything on start")
r = TAB.startup()
check(r.downloaded == 3, "3 files downloaded")
check(TAB.read("1/profile.jkr") == "return {progress=1}", "profile matches PC")
check(TAB.read("1/save.jkr") == "Z:return {run=1}", "run save matches PC")
check(has_event(TAB.events(), "toast", "text", "Save sync: loaded progress from PC"), "toast names PC")

print("3) Nothing changes -> nothing happens")
r = PC.tick()
check(r.uploaded == 0 and r.downloaded == 0 and not r.stale, "PC idle tick is a no-op")
r = TAB.tick()
check(r.uploaded == 0 and not r.stale, "Tablet idle tick is a no-op")

print("4) Tablet plays, uploads; running PC session becomes stale")
TAB.play("1/profile.jkr", "return {progress=2}")
r = TAB.tick()
check(r.uploaded == 1, "tablet uploaded profile")
r = PC.tick()
check(r.stale == true, "PC detects newer remote progress")
check(has_event(PC.events(), "stale", "from", "Tablet"), "stale event names Tablet")
PC.write("1/profile.jkr", "return {progress=1, stale_write=true}", 5) -- outdated game keeps saving
r = PC.tick()
check(r.uploaded == 0, "stale PC session does not upload")

print("5) PC restarts: force pull replaces the stale local saves")
PC.restart()
r = PC.startup()
check(PC.read("1/profile.jkr") == "return {progress=2}", "PC now has tablet progress")
check(count_backups(PC) >= 1, "replaced local file was backed up")
check(PC.api.state.force_pull == false, "force_pull cleared")
r = PC.tick()
check(r.uploaded == 0 and not r.stale, "PC in sync afterwards")

print("6) Offline conflict: both change meta, newer change wins")
PC.play("1/meta.jkr", "return {unlocked={'j_joker'}}", 10)       -- played earlier
TAB.play("1/meta.jkr", "return {unlocked={'j_blueprint'}}", 400)  -- played later
r = PC.tick()
check(r.uploaded == 1, "PC uploads its change first")
r = TAB.tick()
check(r.conflicts == 1 and r.uploaded == 1, "tablet sees conflict and wins (newer)")
r = PC.tick()
check(r.stale == true, "PC session becomes stale")
PC.restart(); PC.startup()
check(PC.read("1/meta.jkr") == "return {unlocked={'j_blueprint'}}", "PC got the newer meta")

print("7) Run ends on tablet: save.jkr deletion syncs")
TAB.play("1/save.jkr", nil)
r = TAB.tick()
check(r.removed == 1, "tablet sends delete")
PC.restart(); r = PC.startup()
check(PC.read("1/save.jkr") == nil, "PC save.jkr removed on start")
check(r.stale ~= true, "no stale flag at startup")

print("8) Half-written file is skipped, then synced")
PC.restart(); PC.startup(); PC.events()
PC.write("1/profile.jkr", "\0\1garbage", 5)
r = PC.tick()
check(r.retry == true and r.uploaded == 0, "invalid file skipped")
PC.play("1/profile.jkr", "return {progress=3}")
r = PC.tick()
check(r.uploaded == 1, "valid file uploaded next round")

print("9) Startup abort: no files written without the main thread's go")
TAB.restart()
TAB.channels.balatro_sync_go = new_channel() -- no 'go' queued
local before = TAB.read("1/profile.jkr")
r = TAB.api.handle("startup:7")
check(TAB.read("1/profile.jkr") == before, "nothing written after abort")
local replies = TAB.channels.balatro_sync_reply.items
check(replies[1] == "ready_to_write:7" and replies[#replies] == "done:7", "handshake messages carry the request id")
TAB.restart(); TAB.startup()
check(TAB.read("1/profile.jkr") == "return {progress=3}", "next start pulls normally")

print("10) Wrong token and unreachable server")
local BAD = make_device("Bad", { token = "nope" })
r = BAD.startup()
check(r.offline and r.error == "wrong token", "wrong token reported")
check(has_event(BAD.events(), "toast", "text", "Save sync: wrong token"), "toast says wrong token")
local OFF = make_device("Off", { server_url = "http://127.0.0.1:1" })
r = OFF.startup()
check(r.offline == true, "unreachable server reported offline")
OFF.write("1/profile.jkr", "return {offline=1}")
r = OFF.tick()
check(r.offline == true and r.uploaded == 0, "offline tick does nothing")

print("11) Local backups are pruned to max_local_backups")
for i = 1, 6 do
  TAB.write("1/profile.jkr", "return {tablet_local=" .. i .. "}", 1)
  PC.play("1/profile.jkr", "return {pc=" .. i .. "}", 1000)
  PC.tick()
  TAB.restart(); TAB.startup()
end
local snaps = 0
local items = {}
for p in pairs(TAB.files) do
  local snap = p:match("^balatro_sync/backups/([^/]+)/")
  if snap then items[snap] = true end
end
for _ in pairs(items) do snaps = snaps + 1 end
check(snaps == 3, "exactly 3 backup snapshots kept (got " .. snaps .. ")")


print("12) A game left open without input never uploads")
local IDLE = make_device("Idle", { profiles = "3" })
IDLE.startup()
IDLE.write("3/profile.jkr", "return {idle_rewrite=1}")
r = IDLE.tick()
check(r.uploaded == 0, "idle rewrite stays local")
IDLE.play("3/profile.jkr", "return {played=1}", 1)
r = IDLE.tick()
check(r.uploaded == 1, "after input the change is uploaded")

print("13) Idle device vs device being played: the played one wins")
local P2 = make_device("PC", { profiles = "2" })
local T2 = make_device("Tablet", { profiles = "2" })
T2.play("2/profile.jkr", "return {p=1}", -200)  -- last played minutes ago
T2.startup()
P2.startup()
check(P2.read("2/profile.jkr") == "return {p=1}", "PC pulled tablet start state")
T2.write("2/profile.jkr", "return {p=1, idle=1}", 1)  -- tablet just lying around
P2.play("2/profile.jkr", "return {p=2}", 5)           -- player on PC
r = T2.tick()
check(r.uploaded == 0, "idle tablet does not upload")
r = P2.tick()
check(r.uploaded == 1, "PC uploads real progress")
r = T2.tick()
check(r.stale == true and r.uploaded == 0, "idle tablet goes stale instead of overwriting")
T2.restart(); T2.startup()
check(T2.read("2/profile.jkr") == "return {p=2}", "tablet gets PC progress after reload")

print("14) First contact: device with old local saves joins the server state")
-- Real-world case: PC synced first; the tablet still has older saves, but
-- Balatro rewrote its profile.jkr on start, so its file time is NEWER.
local P4 = make_device("PC", { profiles = "4" })
P4.write("4/profile.jkr", "return {pc_progress=50}")
P4.write("4/meta.jkr", "return {pc_unlocks=50}")
P4.startup()
local T4 = make_device("Tablet", { profiles = "4" })
T4.clock = P4.clock + 5000                                -- tablet file times are later
T4.write("4/profile.jkr", "return {tablet_progress=10}")
T4.write("4/meta.jkr", "return {tablet_unlocks=10}")
T4.write("4/save.jkr", "Z:return {old_tablet_run=1}")     -- server has no run file
r = T4.startup()
check(T4.read("4/profile.jkr") == "return {pc_progress=50}", "tablet takes server profile despite newer file time")
check(T4.read("4/meta.jkr") == "return {pc_unlocks=50}", "tablet takes server meta")
check(r.uploaded == 0, "nothing from the joining tablet is uploaded")
check(T4.read("4/save.jkr") == nil, "old tablet run is not mixed into the PC state")
local backed_up = {}
for p, f in pairs(T4.files) do
  local name = p:match("^balatro_sync/backups/.*4/(%w+)%.jkr%.bak$")
  if name then backed_up[name] = f.content end
end
check(backed_up.profile == "return {tablet_progress=10}", "old tablet profile is in the local backup")
check(backed_up.save == "Z:return {old_tablet_run=1}", "old tablet run is in the local backup")
T4.play("4/profile.jkr", "return {pc_progress=50, tablet_played=1}")
r = T4.tick()
check(r.uploaded == 1, "after joining, tablet progress uploads normally")
P4.restart(); P4.startup()
check(P4.read("4/profile.jkr") == "return {pc_progress=50, tablet_played=1}", "PC gets it on next start")

print("14b) A slot the server doesn't know is uploaded, not removed")
local T6 = make_device("Tablet", { profiles = "6" })
T6.write("6/profile.jkr", "return {only_on_tablet=1}")
T6.write("6/save.jkr", "Z:return {run=6}")
r = T6.startup()
check(r.uploaded == 2 and T6.read("6/save.jkr") == "Z:return {run=6}", "new slot uploaded with its run")
r = P4.tick()
check(r.stale ~= true, "PC is not disturbed by the tablet joining")

print("15) First contact while running (startup was offline): session goes stale, then joins")
local P5 = make_device("PC", { profiles = "5" })
P5.write("5/profile.jkr", "return {pc=5}")
P5.startup()
local T5 = make_device("Tablet", { profiles = "5", server_url = "http://127.0.0.1:1" })
T5.write("5/profile.jkr", "return {tablet=5}")
r = T5.startup()
check(r.offline == true, "tablet starts offline")
T5.api = nil
local T5b = make_device("Tablet", { profiles = "5" })      -- same files, network back
for p, f in pairs(T5.files) do T5b.files[p] = f end
r = T5b.tick()
check(r.stale == true and r.uploaded == 0, "running session goes stale instead of uploading")
T5b.restart(); T5b.startup()
check(T5b.read("5/profile.jkr") == "return {pc=5}", "after reload the tablet has the server state")

print(("\n%d passed, %d failed"):format(passed, failed))
os.exit(failed == 0 and 0 or 1)

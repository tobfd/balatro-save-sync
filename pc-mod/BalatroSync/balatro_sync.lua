--[[
BalatroSync - automatic save sync for Balatro (Steam/PC and mobile builds).

How it works
  * On game start - before Balatro reads its save files - newer saves are
    pulled from the sync server.
  * A background thread watches the save files and uploads every change.
  * If another device uploads newer progress while this game is running, the
    progress held in memory here is outdated. Uploads stop, an overlay asks
    to reload, and the next start pulls the newer saves.
  * Every local file that gets replaced is backed up first to
    <save dir>/balatro_sync/backups; the server keeps a version history too.

Usage (the PC mod and the Android patcher already do this):
  require("balatro_sync").install(require("balatro_sync_config"))
It has to run before main.lua defines love.load.
]]

local M = { VERSION = "1.0.0" }

local unpack = unpack or table.unpack

local CHANNELS = {
  cmd = "balatro_sync_cmd",
  reply = "balatro_sync_reply",
  events = "balatro_sync_events",
  go = "balatro_sync_go",
  activity = "balatro_sync_activity",
}

local DEFAULTS = {
  enabled = true,
  server_url = "",
  token = "",
  device_name = "Device",
  profiles = "1,2,3",
  sync_settings = false,
  interval = 10,
  timeout = 2,
  show_toasts = true,
  max_local_backups = 20,
}

local function log(...)
  local parts = {}
  for i = 1, select("#", ...) do
    parts[#parts + 1] = tostring((select(i, ...)))
  end
  print(os.date("%H:%M:%S") .. " [BalatroSync] " .. table.concat(parts, " "))
end

-----------------------------------------------------------------------------
-- Worker thread. Runs all file and network work off the main thread.
-- Started with the (flat) config table as first argument.
-----------------------------------------------------------------------------
local WORKER_SOURCE = [==[
local cfg, test_env = ...
if not test_env then
  require("love.filesystem")
  require("love.data")
  require("love.timer")
end
local http = require("socket.http")
local ltn12 = require("ltn12")

local fs = love.filesystem
local CMD = love.thread.getChannel("balatro_sync_cmd")
local REPLY = love.thread.getChannel("balatro_sync_reply")
local EVENTS = love.thread.getChannel("balatro_sync_events")
local GO = love.thread.getChannel("balatro_sync_go")
-- Latest time (os.time) the player touched/clicked/pressed something.
local ACTIVITY = love.thread.getChannel("balatro_sync_activity")

local STATE_DIR = "balatro_sync"
local STATE_FILE = STATE_DIR .. "/state.txt"
local BACKUP_DIR = STATE_DIR .. "/backups"

local BASE_URL = (tostring(cfg.server_url or ""):gsub("/+$", ""))
local DEVICE = tostring(cfg.device_name or "Device")
local INTERVAL = math.max(3, tonumber(cfg.interval) or 10)
local MAX_BACKUPS = math.max(1, tonumber(cfg.max_local_backups) or 20)
-- A running game uploads only if the player gave input this long ago at most.
local ACTIVE_WINDOW = 90
http.TIMEOUT = math.max(0.5, tonumber(cfg.timeout) or 2)

local TRACKED = {}
for slot in tostring(cfg.profiles or "1,2,3"):gmatch("[1-9]") do
  for _, name in ipairs({ "profile.jkr", "meta.jkr", "save.jkr" }) do
    TRACKED[#TRACKED + 1] = slot .. "/" .. name
  end
end
if cfg.sync_settings then
  TRACKED[#TRACKED + 1] = "settings.jkr"
end

local function now()
  return love.timer.getTime()
end

local function event(kind, fields)
  local ev = { kind = kind }
  for k, v in pairs(fields or {}) do
    ev[k] = v
  end
  EVENTS:push(ev)
end

local function toast(text)
  if cfg.show_toasts ~= false then
    event("toast", { text = text })
  end
end

-- Line for the game log (the main thread prints it).
local function note(text)
  event("log", { text = text })
end

local function md5hex(s)
  return (love.data.encode("string", "hex", love.data.hash("md5", s))):lower()
end

-- Balatro writes .jkr files either as plain Lua ("return {...}") or deflated.
-- A file that is neither is most likely being written right now.
local function looks_valid(content)
  if #content == 0 then
    return false
  end
  if content:sub(1, 6) == "return" then
    return true
  end
  local ok, out = pcall(love.data.decompress, "string", "deflate", content)
  return ok and type(out) == "string" and #out > 0
end

-- How much has been played on a profile? Balatro counts rounds and hands in
-- profile.jkr's career_stats, and both only ever go up, so they show which
-- copy of a profile is further along. Plain text matching, nothing is run.
-- Returns rounds, hands - or nil if the file can't be read.
local function progress_of(content)
  if type(content) ~= "string" or #content == 0 then
    return nil
  end
  local text = content
  if text:sub(1, 6) ~= "return" then
    local ok, out = pcall(love.data.decompress, "string", "deflate", content)
    if not ok or type(out) ~= "string" then
      return nil
    end
    text = out
  end
  local stats = text:match('%["career_stats"%]%s*=%s*(%b{})')
  if not stats then
    return nil
  end
  local rounds = tonumber(stats:match('%["c_rounds"%]%s*=%s*(%d+)'))
  if not rounds then
    return nil
  end
  return rounds, tonumber(stats:match('%["c_hands_played"%]%s*=%s*(%d+)')) or 0
end

---------------------------------------------------------------- state ---
-- state.files[path] = { rev = <server revision>, hash = <md5 or nil>, at = <os.time> }
--   the version both sides agreed on at the last successful sync, and when.
-- state.activity = last time the player actually played on this device.
--   Progress needs input, so this is "when real progress was made". It
--   decides conflicts, and a device without input since the last sync never
--   uploads (an idle game left open can't overwrite real progress).
local state = { files = {}, force_pull = false, stale = false, activity = 0 }

local function load_state()
  state.files = {}
  if not fs.getInfo(STATE_FILE, "file") then
    return
  end
  local content = fs.read(STATE_FILE)
  if type(content) ~= "string" then
    return
  end
  for line in content:gmatch("[^\r\n]+") do
    if line == "force_pull=1" then
      state.force_pull = true
    elseif line:match("^activity=%d+$") then
      state.activity = tonumber(line:match("%d+"))
    else
      local path, rev, hash, at = line:match("^file\t([^\t]+)\t(%d+)\t([^\t]+)\t?(%d*)$")
      if path then
        state.files[path] = {
          rev = tonumber(rev),
          hash = (hash ~= "-") and hash or nil,
          at = tonumber(at) or 0,
        }
      end
    end
  end
end

local function save_state()
  local out = {
    "v2",
    "force_pull=" .. (state.force_pull and "1" or "0"),
    "activity=" .. math.floor(state.activity or 0),
  }
  local paths = {}
  for path in pairs(state.files) do
    paths[#paths + 1] = path
  end
  table.sort(paths)
  for _, path in ipairs(paths) do
    local f = state.files[path]
    out[#out + 1] = ("file\t%s\t%d\t%s\t%d"):format(path, f.rev or 0, f.hash or "-", f.at or 0)
  end
  fs.createDirectory(STATE_DIR)
  fs.write(STATE_FILE, table.concat(out, "\n") .. "\n")
end

local function agreed(rev, hash)
  return { rev = rev, hash = hash, at = os.time() }
end

-- "1/profile.jkr" -> "1"; files outside a profile slot are their own group.
local function slot_of(path)
  return path:match("^(%d)/") or path
end

----------------------------------------------------------- local files ---
-- Returns content and modtime, nil if the file does not exist,
-- or false if it exists but cannot be read.
local function read_local(path)
  local info = fs.getInfo(path, "file")
  if not info then
    return nil
  end
  local content = fs.read(path)
  if type(content) ~= "string" then
    return false
  end
  return content, info.modtime or os.time()
end

local function parent_dir(path)
  return path:match("^(.*)/[^/]*$")
end

local function write_local(path, content)
  local dir = parent_dir(path)
  if dir then
    fs.createDirectory(dir)
  end
  if content then
    fs.write(path, content)
  else
    fs.remove(path)
  end
end

local function remove_tree(path)
  local info = fs.getInfo(path)
  if not info then
    return
  end
  if info.type == "directory" then
    for _, item in ipairs(fs.getDirectoryItems(path)) do
      remove_tree(path .. "/" .. item)
    end
  end
  fs.remove(path)
end

local backup_stamp = nil

local function backup_local(path, content)
  backup_stamp = backup_stamp or os.date("%Y%m%d-%H%M%S")
  local target = BACKUP_DIR .. "/" .. backup_stamp .. "/" .. path .. ".bak"
  fs.createDirectory(parent_dir(target))
  fs.write(target, content)
end

local function finish_backups()
  backup_stamp = nil
  if not fs.getInfo(BACKUP_DIR, "directory") then
    return
  end
  local items = fs.getDirectoryItems(BACKUP_DIR)
  table.sort(items)
  while #items > MAX_BACKUPS do
    remove_tree(BACKUP_DIR .. "/" .. table.remove(items, 1))
  end
end

-------------------------------------------------------------- network ---
local function urlencode(s)
  return (tostring(s):gsub("[^%w%-%._~]", function(c)
    return ("%%%02X"):format(c:byte())
  end))
end

local function request(method, endpoint, query, body)
  local url = BASE_URL .. endpoint
  if query then
    local parts = {}
    for k, v in pairs(query) do
      parts[#parts + 1] = urlencode(k) .. "=" .. urlencode(v)
    end
    table.sort(parts)
    url = url .. "?" .. table.concat(parts, "&")
  end
  local chunks = {}
  local headers = { ["x-sync-token"] = tostring(cfg.token or "") }
  local source = nil
  if body then
    headers["content-type"] = "application/octet-stream"
    headers["content-length"] = tostring(#body)
    source = ltn12.source.string(body)
  elseif method ~= "GET" then
    headers["content-length"] = "0"
  end
  local ok, code = http.request({
    url = url,
    method = method,
    headers = headers,
    source = source,
    sink = ltn12.sink.table(chunks),
  })
  if not ok then
    return nil, tostring(code)
  end
  return code, table.concat(chunks)
end

local function http_error(code, body)
  if code == 401 then
    return "wrong token"
  end
  if code then
    return "HTTP " .. tostring(code)
  end
  return tostring(body)
end

local function fetch_manifest()
  local code, body = request("GET", "/api/manifest")
  if code ~= 200 then
    return nil, http_error(code, body)
  end
  local manifest = {}
  for line in body:gmatch("[^\n]+") do
    local path, rev, hash, mtime, device = line:match("^([^\t]+)\t(%d+)\t([^\t]+)\t(%d+)\t([^\t]*)$")
    if path then
      manifest[path] = {
        rev = tonumber(rev),
        hash = (hash ~= "-") and hash or nil,
        mtime = tonumber(mtime),
        device = device,
      }
    end
  end
  return manifest
end

local function download(path, expected_hash)
  local code, body = request("GET", "/api/file", { path = path })
  if code ~= 200 then
    return nil, http_error(code, body)
  end
  if md5hex(body) ~= expected_hash then
    return nil, "changed during download"
  end
  return body
end

-- Upload (or delete, when content is nil). Returns the new revision.
local function push(path, content, hash, mtime, base_rev)
  local query = {
    path = path,
    base = base_rev,
    mtime = math.floor(mtime),
    device = DEVICE,
  }
  local code, body
  if content then
    query.hash = hash
    code, body = request("PUT", "/api/file", query, content)
  else
    code, body = request("DELETE", "/api/file", query)
  end
  if code == 200 then
    return tonumber(body:match("%d+"))
  end
  if code == 409 then
    return nil, "conflict"
  end
  return nil, http_error(code, body)
end

----------------------------------------------------------------- sync ---
-- mode "startup": the game has not read its saves yet, so newer remote
--                 files can simply be written to disk.
-- mode "periodic": the game holds its progress in memory; newer remote files
--                 make this session stale (it must be reloaded).
local function sync(mode, request_id)
  local startup = (mode == "startup")
  local res = { uploaded = 0, downloaded = 0, removed = 0, conflicts = 0 }
  local manifest, err = fetch_manifest()
  if not manifest then
    res.offline = true
    res.error = err
    return res
  end

  local dirty = false
  local session_activity = tonumber(ACTIVITY:peek()) or 0
  if session_activity > (state.activity or 0) then
    state.activity = session_activity
    dirty = true
  end
  local activity = state.activity or 0
  -- Only a game that is actually being played uploads while running.
  local recently_active = session_activity > 0 and (os.time() - session_activity) <= ACTIVE_WINDOW

  -- Profile slots the server already has data for, and slots this device has
  -- ever synced. A device that meets a slot for the first time joins it.
  local server_slots, synced_slots = {}, {}
  for path, entry in pairs(manifest) do
    if entry.rev > 0 then
      server_slots[slot_of(path)] = true
    end
  end
  for path, entry in pairs(state.files) do
    if (entry.rev or 0) > 0 then
      synced_slots[slot_of(path)] = true
    end
  end

  -- Joining normally means taking the server state. Exception: this device
  -- has clearly played more on that profile - e.g. a fresh phone happened to
  -- sync first and uploaded an empty profile. Then this device keeps the
  -- whole slot and uploads it; the server keeps the old state in its history.
  local keep_local = {}
  for slot in pairs(server_slots) do
    if slot:match("^%d$") and not synced_slots[slot] then
      local profile = slot .. "/profile.jkr"
      local l_rounds, l_hands = progress_of((read_local(profile)))
      if l_rounds then
        local srv = manifest[profile]
        local r_rounds, r_hands = 0, 0
        local readable = true
        if srv and srv.hash then
          local data = download(profile, srv.hash)
          r_rounds, r_hands = progress_of(data)
          readable = r_rounds ~= nil
        end
        if readable and (l_rounds > r_rounds or (l_rounds == r_rounds and l_hands > r_hands)) then
          keep_local[slot] = true
          note(("slot %s: this device played more (%d vs %d rounds), keeping its saves")
            :format(slot, l_rounds, r_rounds))
        end
      end
    end
  end

  local writes = {}
  for _, path in ipairs(TRACKED) do
    local content, lmtime = read_local(path)
    if content == false or (content and not looks_valid(content)) then
      res.retry = true -- half-written or locked, look again soon
    else
      local lhash = content and md5hex(content) or nil
      local base = state.files[path] or { rev = 0, at = 0 }
      local srv = manifest[path] or { rev = 0, mtime = 0, device = "" }
      -- When was the local version really played? Before the first input is
      -- ever recorded, the file time is the best guess.
      local local_time = activity
      if local_time <= 0 and startup then
        local_time = lmtime or os.time()
      end

      if lhash == srv.hash then
        if base.rev ~= srv.rev or base.hash ~= srv.hash then
          state.files[path] = agreed(srv.rev, srv.hash)
          dirty = true
        end
      else
        local local_changed = (lhash ~= base.hash)
        local remote_changed = (srv.rev > base.rev)
        local server_reset = srv.rev < base.rev
        if server_reset then
          -- The server lost data (wiped or replaced): this copy is the reference.
          local_changed, remote_changed = true, false
        end

        -- First contact: this device has never agreed on this file with the
        -- server, but another device already synced it (or its slot). File
        -- times can't tell which progress is newer (Balatro rewrites its
        -- saves on every start), so the device joins the existing state as a
        -- whole - including "no run in progress" - and its own old files go
        -- to the local backup. Unless it played more (see keep_local above).
        local slot = slot_of(path)
        local joining = server_slots[slot] and not synced_slots[slot]
        local first_contact = (base.rev or 0) == 0 and (srv.rev > 0 or joining)

        -- srv.mtime is when the other device was last really played.
        local remote_newer = (srv.mtime or 0) > local_time
        local remote_wins
        if startup and state.force_pull and srv.rev > 0 and srv.rev >= base.rev then
          remote_wins = true -- last session was stale: take the server state
        elseif first_contact then
          remote_wins = not keep_local[slot]
        elseif remote_changed and local_changed then
          res.conflicts = res.conflicts + 1
          remote_wins = remote_newer
        elseif remote_changed then
          -- Nothing new here. At startup just take it. While running, it only
          -- matters if the other device was played after this one.
          remote_wins = startup or remote_newer
        else
          remote_wins = false
        end

        if remote_wins then
          if startup then
            local data, derr = nil, nil
            if srv.hash then
              data, derr = download(path, srv.hash)
            end
            if srv.hash and not data then
              res.retry = true
              res.error = res.error or derr
            else
              writes[#writes + 1] = { path = path, data = data, old = content, srv = srv }
            end
          else
            res.stale_from = res.stale_from or srv.device
          end
        elseif not state.stale and (startup or server_reset or recently_active) then
          local stamp = (activity > 0) and activity or (lmtime or os.time())
          local new_rev, perr = push(path, content, lhash, stamp, srv.rev)
          if new_rev then
            state.files[path] = agreed(new_rev, lhash)
            dirty = true
            note(("uploaded %s (rev %d)"):format(path, new_rev))
            if content then
              res.uploaded = res.uploaded + 1
            else
              res.removed = res.removed + 1
            end
          else
            res.retry = true
            res.error = res.error or perr
          end
        end
      end
    end
  end

  local writes_done = true
  if #writes > 0 then
    -- Ask the main thread for permission first: if it stopped waiting for
    -- us, the game may already be reading its saves.
    REPLY:push("ready_to_write:" .. tostring(request_id or ""))
    if GO:demand(3) == "go" then
      for _, w in ipairs(writes) do
        if w.old then
          backup_local(w.path, w.old)
        end
        write_local(w.path, w.data)
        state.files[w.path] = agreed(w.srv.rev, w.srv.hash)
        note(("%s %s from %s (rev %d)"):format(w.data and "loaded" or "removed", w.path, w.srv.device, w.srv.rev))
        if w.data then
          res.downloaded = res.downloaded + 1
        end
        res.from = w.srv.device
      end
      finish_backups()
      dirty = true
    else
      writes_done = false
    end
  end

  if startup and state.force_pull and writes_done then
    state.force_pull = false
    dirty = true
  end
  if res.stale_from and not state.stale then
    state.stale = true
    state.force_pull = true
    res.stale = true
    dirty = true
  end
  if dirty then
    save_state()
  end
  return res
end

local online = nil

local function report(res, startup)
  if res.offline then
    if startup then
      toast(res.error == "wrong token" and "Save sync: wrong token" or "Save sync: server not reachable")
    end
    if online ~= false then
      event("net", { online = false })
    end
    online = false
    return
  end
  if startup then
    if res.from then
      toast("Save sync: loaded progress from " .. res.from)
    elseif res.uploaded + res.removed > 0 then
      toast("Save sync: uploaded local progress")
    else
      toast("Save sync: up to date")
    end
  elseif online == false then
    toast("Save sync: connected")
  end
  if online ~= true then
    event("net", { online = true })
  end
  online = true
  if res.stale then
    event("stale", { from = res.stale_from or "another device" })
  end
end

-- Commands look like "name:id"; replies carry the same id.
local function handle(msg)
  local name, id = tostring(msg):match("^([%w_]+):?(.*)$")
  if name == "startup" then
    local res = sync("startup", id)
    report(res, true)
    REPLY:push("done:" .. id)
    return res
  elseif name == "resume" or name == "flush" then
    if name == "flush" then
      love.timer.sleep(0.4) -- let the game finish writing its saves
    end
    local res = sync("periodic")
    report(res, false)
    REPLY:push("done:" .. id)
    return res
  end
  local res = sync("periodic")
  report(res, false)
  return res
end

load_state()

if test_env then
  return { handle = handle, sync = sync, state = state, load_state = load_state, tracked = TRACKED }
end

local next_check = now() + INTERVAL
while true do
  local msg = CMD:demand(math.max(0.05, next_check - now()))
  if msg == "quit" then
    break
  end
  local ok, res = pcall(handle, msg or "tick")
  if not ok then
    event("error", { text = tostring(res) })
    if msg then
      local id = tostring(msg):match(":(.*)$") or ""
      REPLY:push("done:" .. id)
    end
    res = nil
  end
  local quick = res and res.retry and not res.offline
  next_check = now() + (quick and 2 or INTERVAL)
end
]==]

M.WORKER_SOURCE = WORKER_SOURCE

-----------------------------------------------------------------------------
-- Main thread side
-----------------------------------------------------------------------------
local thread = nil
local ch = nil
local next_id = 0
local ui = {
  toast = nil,
  toast_start = nil,
  stale = false,
  stale_from = nil,
  stale_at = 0,
  reloading = false,
  fonts = {},
}
M.ui = ui

local function normalize(user)
  local cfg = {}
  for k, v in pairs(DEFAULTS) do
    cfg[k] = v
  end
  if type(user) == "table" then
    for k, v in pairs(user) do
      local t = type(v)
      if type(k) == "string" and (t == "string" or t == "number" or t == "boolean") then
        cfg[k] = v
      end
    end
  end
  cfg.server_url = tostring(cfg.server_url)
  cfg.token = tostring(cfg.token)
  cfg.device_name = tostring(cfg.device_name)
  cfg.profiles = tostring(cfg.profiles)
  cfg.timeout = tonumber(cfg.timeout) or DEFAULTS.timeout
  cfg.interval = tonumber(cfg.interval) or DEFAULTS.interval
  return cfg
end

local function channels()
  if not ch then
    ch = {}
    for key, name in pairs(CHANNELS) do
      ch[key] = love.thread.getChannel(name)
    end
  end
  return ch
end

local function show_toast(text)
  if M.cfg.show_toasts == false then
    return
  end
  ui.toast = text
  ui.toast_start = nil -- the clock starts at the first frame that shows it
end

local function poll_events()
  if not ch then
    return
  end
  while true do
    local ev = ch.events:pop()
    if not ev then
      break
    end
    if ev.kind == "toast" then
      show_toast(ev.text)
    elseif ev.kind == "stale" then
      if not ui.stale then
        ui.stale = true
        ui.stale_from = ev.from
        ui.stale_at = love.timer.getTime()
        log("newer save from " .. tostring(ev.from) .. " - this session is outdated")
      end
    elseif ev.kind == "net" then
      ui.online = ev.online
    elseif ev.kind == "log" then
      log(ev.text)
    elseif ev.kind == "error" then
      log("worker error: " .. tostring(ev.text))
    end
  end
end

-- Send a command to the worker. With wait (seconds) it blocks until the
-- worker answers this exact command, and lets it write files at startup.
local function request(cmd, wait)
  if not thread then
    return "no worker"
  end
  local c = channels()
  next_id = next_id + 1
  local id = tostring(next_id)
  c.go:clear()
  c.cmd:push(cmd .. ":" .. id)
  if not wait then
    return "sent"
  end
  local deadline = love.timer.getTime() + wait
  while true do
    local left = deadline - love.timer.getTime()
    if left <= 0 then
      return "timeout"
    end
    local msg = c.reply:demand(left)
    if msg == nil then
      return "timeout"
    elseif msg == "ready_to_write:" .. id then
      c.go:push("go")
      deadline = love.timer.getTime() + 10
    elseif msg == "done:" .. id then
      return "done"
    end
    -- anything else is a late answer to an older command; ignore it
  end
end

-- Tell the worker when the player last did something (once per second max).
local last_activity_sent = 0
local function mark_activity()
  local t = os.time()
  if ch and t ~= last_activity_sent then
    last_activity_sent = t
    ch.activity:clear()
    ch.activity:push(t)
  end
end
M.mark_activity = mark_activity

local function reload()
  if ui.reloading then
    return
  end
  ui.reloading = true
  if M.mobile then
    love.event.quit("restart")
  else
    love.event.quit()
  end
end

local function font(size)
  size = math.max(8, math.floor(size))
  if not ui.fonts[size] then
    ui.fonts[size] = love.graphics.newFont(size)
  end
  return ui.fonts[size]
end

local function draw_ui()
  if not (ui.stale or ui.toast) then
    return
  end
  local g = love.graphics
  g.push("all")
  g.setCanvas()
  g.origin()
  g.setShader()
  g.setScissor()
  g.setBlendMode("alpha")
  local w, h = g.getDimensions()
  local unit = math.max(12, math.min(w, h) / 30)

  if ui.stale then
    g.setColor(0, 0, 0, 0.85)
    g.rectangle("fill", 0, 0, w, h)
    g.setColor(1, 1, 1, 1)
    local y = h * 0.35
    g.setFont(font(unit * 1.6))
    g.printf("Newer save found", 0, y, w, "center")
    g.setFont(font(unit))
    g.printf(("Progress from %s is newer than this session."):format(ui.stale_from or "another device"),
      w * 0.1, y + unit * 2.6, w * 0.8, "center")
    local hint = M.mobile and "Reloading..."
      or "Click or press any key to close Balatro, then start it again."
    g.printf(hint, w * 0.1, y + unit * 4.4, w * 0.8, "center")
  elseif ui.toast then
    local t = love.timer.getTime()
    ui.toast_start = ui.toast_start or t
    local age = t - ui.toast_start
    if age > 4 then
      ui.toast = nil
    else
      local alpha = math.min(1, (4 - age) / 0.5)
      local f = font(unit * 0.8)
      g.setFont(f)
      local tw = f:getWidth(ui.toast) + unit
      local th = f:getHeight() + unit * 0.6
      local x, y = unit * 0.6, h - th - unit * 0.6
      g.setColor(0, 0, 0, 0.7 * alpha)
      g.rectangle("fill", x, y, tw, th, unit * 0.3, unit * 0.3)
      g.setColor(1, 1, 1, alpha)
      g.print(ui.toast, x + unit * 0.5, y + unit * 0.3)
    end
  end
  g.pop()
end

local function install_threaderror()
  local orig = love.threaderror
  love.threaderror = function(t, err)
    if thread and t == thread then
      log("worker stopped: " .. tostring(err))
      thread = nil
      return
    end
    if orig then
      return orig(t, err)
    end
    error("Thread error (" .. tostring(t) .. ")\n\n" .. tostring(err), 0)
  end
end

local function start_worker()
  local c = channels()
  for _, channel in pairs(c) do
    channel:clear()
  end
  thread = love.thread.newThread(WORKER_SOURCE)
  thread:start(M.cfg)
end

function M._startup()
  if not M.cfg.enabled then
    return
  end
  if M.cfg.server_url == "" or M.cfg.token == "" then
    show_toast("Save sync: not configured")
    return
  end
  install_threaderror()
  start_worker()
  local result = request("startup", M.cfg.timeout * 4 + 6)
  if result ~= "done" then
    log("startup sync did not finish (" .. result .. "), continuing without it")
  end
  poll_events()
end

local function safe(fn, ...)
  local ok, err = pcall(fn, ...)
  if not ok then
    log("error: " .. tostring(err))
  end
end

local function wrap(name, make)
  love[name] = make(love[name])
end

local function install_runtime_hooks()
  wrap("update", function(orig)
    return function(...)
      safe(function()
        poll_events()
        if ui.stale and M.mobile and love.timer.getTime() - ui.stale_at > 3 then
          reload()
        end
      end)
      if orig then
        return orig(...)
      end
    end
  end)

  wrap("draw", function(orig)
    return function(...)
      if orig then
        orig(...)
      end
      safe(draw_ui)
    end
  end)

  wrap("focus", function(orig)
    return function(focused, ...)
      if M.cfg.resume_check and thread then
        safe(function()
          if focused then
            -- Coming back (mobile): check for newer saves before the game
            -- gets a chance to write its outdated in-memory state. Only wait
            -- when the server was reachable last time, so being away from
            -- home doesn't freeze the game on every resume.
            poll_events()
            request("resume", ui.online and (M.cfg.timeout * 2 + 2) or nil)
            poll_events()
          else
            request("flush")
          end
        end)
      end
      if orig then
        return orig(focused, ...)
      end
    end
  end)

  wrap("quit", function(orig)
    return function(...)
      if orig then
        local abort = orig(...)
        if abort then
          return abort
        end
      end
      if thread then
        safe(function()
          if not ui.stale then
            request("flush", M.cfg.timeout * 2 + 1)
          end
          channels().cmd:push("quit")
          if thread:isRunning() then
            thread:wait()
          end
          poll_events()
        end)
      end
      return false
    end
  end)

  -- Input marks real play activity. While the session is outdated, any
  -- input closes / reloads the game instead.
  for _, name in ipairs({ "mousepressed", "touchpressed", "keypressed", "gamepadpressed" }) do
    wrap(name, function(orig)
      return function(...)
        if ui.stale then
          safe(reload)
          return
        end
        safe(mark_activity)
        if orig then
          return orig(...)
        end
      end
    end)
  end
end

local function hook_load()
  local function make_load(orig)
    return function(...)
      safe(M._startup)
      local results = { orig(...) }
      safe(install_runtime_hooks)
      return unpack(results)
    end
  end

  if rawget(love, "load") then
    love.load = make_load(love.load)
    return
  end
  -- main.lua has not defined love.load yet: catch the assignment.
  local mt = getmetatable(love)
  if not mt then
    mt = {}
    setmetatable(love, mt)
  end
  local prev = mt.__newindex
  mt.__newindex = function(t, k, v)
    if k == "load" and type(v) == "function" then
      mt.__newindex = prev
      v = make_load(v)
    end
    if type(prev) == "function" then
      return prev(t, k, v)
    elseif type(prev) == "table" then
      prev[k] = v
    else
      rawset(t, k, v)
    end
  end
end

function M.install(user_cfg)
  if M._installed then
    return M
  end
  M._installed = true
  M.cfg = normalize(user_cfg)
  local ok, os_name = pcall(function()
    return love.system.getOS()
  end)
  M.mobile = ok and (os_name == "Android" or os_name == "iOS")
  if M.cfg.resume_check == nil then
    M.cfg.resume_check = M.mobile
  end
  hook_load()
  log("v" .. M.VERSION .. " installed for device '" .. M.cfg.device_name .. "'")
  return M
end

return M

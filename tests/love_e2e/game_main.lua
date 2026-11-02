-- Fake Balatro: mimics how the real game loads and saves.
-- love.load reads the profile once; progress lives in memory; a separate
-- save thread writes it periodically (like Balatro's save_manager).
local QUIT_AT = tonumber(os.getenv("QUIT_AT") or "0")
local SHOT_AT = tonumber(os.getenv("SHOT_AT") or "0")
local SHOT_FILE = os.getenv("SHOT_FILE")
local save_thread
local progress = 0
local timer = 0
local shot_done = false
local MODE = os.getenv("MODE") or "active" -- active | idle_rewrite | idle
local idle_counter = 0
local FOCUS_AT = tonumber(os.getenv("FOCUS_AT") or "0")

function love.load()
  local content = love.filesystem.read("1/profile.jkr")
  progress = content and tonumber(content:match("progress=(%d+)")) or 0
  print(os.date("%H:%M:%S") .. " [Fake] loaded progress " .. progress)
  io.stdout:flush()
  save_thread = love.thread.newThread([[
    require("love.filesystem")
    local ch = love.thread.getChannel("save_request")
    while true do
      local req = ch:demand()
      if req == "quit" then break end
      love.filesystem.createDirectory("1")
      love.filesystem.write("1/profile.jkr", req)
    end
  ]])
  save_thread:start()
end

function love.update(dt)
  timer = timer + dt
  if timer > 0.5 then
    timer = 0
    if MODE == "active" then
      -- the player clicks something, the game makes progress and saves
      if love.mousepressed then love.mousepressed(10, 10, 1, false, 1) end
      progress = progress + 1
      love.thread.getChannel("save_request"):push("return {progress=" .. progress .. "}")
    elseif MODE == "idle_rewrite" then
      -- nobody plays, but the game rewrites its save with a changed counter
      idle_counter = idle_counter + 1
      love.thread.getChannel("save_request"):push(
        "return {progress=" .. progress .. ", idle=" .. idle_counter .. "}")
    end
  end
  if SHOT_AT > 0 and not shot_done and os.time() >= SHOT_AT and SHOT_FILE then
    shot_done = true
    love.graphics.captureScreenshot(function(img)
      local f = io.open(SHOT_FILE, "wb")
      f:write(img:encode("png"):getString())
      f:close()
    end)
  end
  if FOCUS_AT > 0 and os.time() >= FOCUS_AT then
    -- like Android: app goes to the background and comes back
    FOCUS_AT = 0
    print(os.date("%H:%M:%S") .. " [Fake] focus lost / regained")
    if love.focus then love.focus(false); love.focus(true) end
  end
  if QUIT_AT > 0 and os.time() >= QUIT_AT then
    QUIT_AT = 0
    love.event.quit()
  end
end

function love.draw()
  love.graphics.clear(0.2, 0.35, 0.3)
  love.graphics.print("Fake Balatro - progress " .. progress, 20, 20)
end

function love.quit()
  print(os.date("%H:%M:%S") .. " [Fake] quitting with progress " .. progress)
  io.stdout:flush()
  love.thread.getChannel("save_request"):push("quit")
  if save_thread then save_thread:wait() end
end

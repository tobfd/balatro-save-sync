-- Fake Balatro: conf
function love.conf(t)
  t.identity = os.getenv("FB_IDENTITY") or "fakebalatro"
  t.window.title = "Fake Balatro"
  t.window.width = 800
  t.window.height = 450
  t.modules.audio = false
  t.modules.sound = false
  t.modules.joystick = false
end

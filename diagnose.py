"""Run this to see whether your display supports real brightness control."""
import time
import screen_brightness_control as sbc

print("Library version:", getattr(sbc, "__version__", "?"))
try:
    for m in sbc.list_monitors_info():
        print("Monitor:", m)
except Exception as e:
    print("Could not list monitors:", repr(e))

try:
    cur = sbc.get_brightness()
    print("Current brightness:", cur)
    sbc.set_brightness(40)
    time.sleep(1)
    print("After setting 40:", sbc.get_brightness(), "(did your screen change?)")
    sbc.set_brightness(cur[0])
    print("Restored.")
except Exception as e:
    print("Real brightness control FAILED:", repr(e))
    print("-> The app will automatically use overlay dimming instead.")

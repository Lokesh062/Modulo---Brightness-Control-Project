"""Slingshot Brightness - desktop widget edition (Windows 10/11).

Press  Ctrl+Alt+B  anywhere and the slingshot slides up from the bottom of your
screen. Pull the bird back, let go, and where it lands sets your brightness
(left = dim, right = bright). After the impact animation it slides away again.

  Ctrl+Alt+B   show / hide the slingshot
  Ctrl+Alt+Q   quit the app

Run `python main.py --install` once to create a Desktop shortcut and a Startup
shortcut (starts hidden at login, so the hotkey is always ready).

Brightness strategy:
  1. Real hardware brightness via screen-brightness-control (verified after set).
  2. If that doesn't actually work on your display, a click-through black
     overlay dims the whole screen instead. This works on every monitor.
"""
import ctypes
import math
import os
import random
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk

try:
    import screen_brightness_control as sbc
except Exception:
    sbc = None

# ---------------- settings ----------------
HOTKEY_VK = 0x42          # 'B'  -> Ctrl+Alt+B toggles the widget
QUIT_VK = 0x51            # 'Q'  -> Ctrl+Alt+Q quits
AUTO_HIDE_AFTER_THROW = True
IDLE_HIDE_SECONDS = 15    # hide if you don't touch it
POWER_BOOST = 1.7         # >1 means 100% is reachable without a full pull
MAX_PULL = 130            # max stretch (px)
G = 0.6                   # gravity
R = 22                    # bird radius
MIN_B = 5                 # never go below 5%
MAX_DIM = 0.85            # overlay never darker than this
RING_LIFE = 95            # frames the shockwave lasts
PORT = 48653              # single-instance port
STEP = 1 / 60             # fixed physics step

IS_WIN = sys.platform.startswith("win")
KEY = "#010101"           # transparent colour key
COOL, WARM = "#58b4ff", "#ffd84a"
GAUGE_STOPS = ("#2b3f8f", "#58b4ff", "#ffe36b")

if IS_WIN:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # true pixel sizes
    except Exception:
        pass


# ---------------- helpers ----------------
def _rgb(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _hex(v):
    return "#%02x%02x%02x" % tuple(max(2, min(255, int(round(x)))) for x in v)


def mix(c1, c2, t):
    a, b = _rgb(c1), _rgb(c2)
    t = max(0.0, min(1.0, t))
    return _hex([a[i] + (b[i] - a[i]) * t for i in range(3)])


def shade(c, f):
    return _hex([v * f for v in _rgb(c)])


def hue_for(b):
    return mix(COOL, WARM, (b - MIN_B) / (100 - MIN_B))


def gauge_color(t):
    if t < 0.5:
        return mix(GAUGE_STOPS[0], GAUGE_STOPS[1], t * 2)
    return mix(GAUGE_STOPS[1], GAUGE_STOPS[2], (t - 0.5) * 2)


def ease_out_back(t):
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def click_through(win):
    """Make a window ignore the mouse (Windows)."""
    if not IS_WIN:
        return
    u = ctypes.windll.user32
    hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
    style = u.GetWindowLongW(hwnd, -20)
    u.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80)  # layered|transparent|toolwindow


def work_bottom(default):
    """Bottom edge of the usable desktop (above the taskbar)."""
    if not IS_WIN:
        return default
    try:
        from ctypes import wintypes
        rc = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rc), 0)  # SPI_GETWORKAREA
        return rc.bottom if rc.bottom > 300 else default
    except Exception:
        return default


class Layer(tk.Canvas):
    """Canvas that tags new items with the current layer ('static' or 'dyn')."""
    tagname = "dyn"


def _wrap(name):
    base = getattr(tk.Canvas, name)

    def f(self, *a, **k):
        k.setdefault("tags", self.tagname)
        return base(self, *a, **k)
    return f


for _n in ("create_line", "create_oval", "create_polygon", "create_text", "create_rectangle"):
    setattr(Layer, _n, _wrap(_n))


# ---------------- shortcuts / single instance ----------------
def install_shortcuts():
    if not IS_WIN:
        print("Shortcut creation is only supported on Windows.")
        return
    exe = sys.executable
    pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pw):
        exe = pw
    script = os.path.abspath(__file__)
    folder = os.path.dirname(script)

    def q(s):
        return "'" + s.replace("'", "''") + "'"

    args_desktop = '"%s"' % script
    args_startup = '"%s" --hidden' % script
    ps = (
        "$s = New-Object -ComObject WScript.Shell;"
        "$l = $s.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Slingshot Brightness.lnk'));"
        "$l.TargetPath = %s; $l.Arguments = %s; $l.WorkingDirectory = %s; $l.IconLocation = %s; $l.Save();"
        "$m = $s.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'Slingshot Brightness.lnk'));"
        "$m.TargetPath = %s; $m.Arguments = %s; $m.WorkingDirectory = %s; $m.IconLocation = %s; $m.Save();"
    ) % (q(exe), q(args_desktop), q(folder), q(exe + ",0"),
         q(exe), q(args_startup), q(folder), q(exe + ",0"))
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
    print("Created 'Slingshot Brightness' on your Desktop and in your Startup folder.")
    print("Press Ctrl+Alt+B any time (while the app is running) to summon the slingshot.")


def single_instance():
    """Return a listening socket, or None if another copy is already running
    (in which case that copy is told to show itself)."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
        s.listen(1)
        return s
    except OSError:
        try:
            c = socket.create_connection(("127.0.0.1", PORT), timeout=1)
            c.sendall(b"show")
            c.close()
        except Exception:
            pass
        return None


# ---------------- the app ----------------
class App:
    def __init__(self, start_hidden=False, server=None):
        root = self.root = tk.Tk()
        root.withdraw()
        self.W, self.H = root.winfo_screenwidth(), root.winfo_screenheight()
        W, H = self.W, self.H
        wb = work_bottom(H)
        self.ground = wb - 52                 # the gauge bar is the ground
        self.ax, self.ay = 170, self.ground - 110
        self.x0 = self.ax + 52                # 0% on the gauge
        self.far = W - 150                    # 100% on the gauge
        self.power = POWER_BOOST * math.sqrt((self.far - self.ax) * G) / MAX_PULL

        win = self.win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.geometry(f"{W}x{H}+0+0")
        win.attributes("-topmost", True)
        if IS_WIN:
            win.attributes("-transparentcolor", KEY)
        self.cv = Layer(win, width=W, height=H, bg=KEY, highlightthickness=0)
        self.cv.pack()
        win.withdraw()

        ov = self.ov = tk.Toplevel(root)       # full-screen dimming overlay (overlay mode only)
        ov.overrideredirect(True)
        ov.geometry(f"{W}x{H}+0+0")
        ov.configure(bg="black")
        ov.attributes("-topmost", True)
        ov.attributes("-alpha", 0.0)
        ov.update_idletasks()
        click_through(ov)

        self.pos = [self.ax, self.ay]
        self.vel = [0.0, 0.0]
        self.state = "idle"        # idle | drag | fly | land
        self.ang = 0.0
        self.sq = self.sqv = 0.0   # jelly squash spring
        self.shake = 0.0
        self.bounces = 0
        self.land_age = 0
        self.rest = False
        self.frame = 0
        self.snap = None           # [release_x, release_y, age] band recoil
        self.parts, self.rings = [], []
        self.vis, self.vis_t = "hidden", 0.0
        self.cur_off = (0.0, 0.0)
        self.req = None
        self.hotkey_ok = None
        self.last_active = time.time()
        self.mode = "checking"     # checking | real | overlay
        self.brightness = 100
        self.shown = 100.0
        self.last_alpha = -1.0

        self.cv.bind("<ButtonPress-1>", self.on_press)
        self.cv.bind("<B1-Motion>", self.on_move)
        self.cv.bind("<ButtonRelease-1>", self.on_release)
        win.bind("<Escape>", lambda e: self.hide())

        self.build_static()
        threading.Thread(target=self.probe, daemon=True).start()
        if IS_WIN:
            threading.Thread(target=self.hotkey_loop, daemon=True).start()
        if server is not None:
            threading.Thread(target=self.serve, args=(server,), daemon=True).start()

        if not start_hidden:
            self.show()
        self.last = time.perf_counter()
        self.acc = 0.0
        self.tick()

    # ---------- background threads ----------
    def hotkey_loop(self):
        try:
            from ctypes import wintypes
            u = ctypes.windll.user32
            mods = 0x0001 | 0x0002 | 0x4000          # ALT | CTRL | NOREPEAT
            self.hotkey_ok = bool(u.RegisterHotKey(None, 1, mods, HOTKEY_VK))
            u.RegisterHotKey(None, 2, mods, QUIT_VK)
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:             # WM_HOTKEY
                    self.req = "toggle" if msg.wParam == 1 else "quit"
        except Exception:
            self.hotkey_ok = False

    def serve(self, srv):
        while True:
            try:
                conn, _ = srv.accept()
                conn.close()
                self.req = "show"
            except Exception:
                break

    # ---------- brightness ----------
    def probe(self):
        if not sbc:
            self.mode = "overlay"
            return
        try:
            vals = [v for v in sbc.get_brightness() if v is not None]
            if not vals:
                raise RuntimeError("no displays")
            self.brightness = vals[0]
            self.shown = float(vals[0])
            self.mode = "real"
        except Exception:
            self.mode = "overlay"

    def set_brightness(self, v):
        if self.mode != "real":
            return

        def work():
            try:
                sbc.set_brightness(v)
                time.sleep(0.7)
                now = [x for x in sbc.get_brightness() if x is not None][0]
                if abs(now - v) > 8:          # said OK but nothing changed
                    raise RuntimeError("no effect")
            except Exception:
                self.mode = "overlay"          # fall back to overlay dimming
        threading.Thread(target=work, daemon=True).start()

    def update_overlay(self):
        alpha = 0.0
        if self.mode == "overlay" and IS_WIN:
            alpha = (100 - self.shown) / 100 * MAX_DIM
        if abs(alpha - self.last_alpha) > 0.003:
            self.ov.attributes("-alpha", alpha)
            self.last_alpha = alpha
            if self.vis != "hidden":
                self.win.attributes("-topmost", True)
                self.win.lift()                # keep the bird above the overlay

    def brightness_from_x(self, x):
        pct = (x - self.x0) / (self.far - self.x0) * 100
        return max(MIN_B, min(100, round(pct)))

    # ---------- show / hide ----------
    def reset_bird(self):
        self.pos, self.vel = [self.ax, self.ay], [0.0, 0.0]
        self.state, self.ang, self.rest = "idle", 0.0, False
        self.sq = self.sqv = 0.0
        self.snap = None

    def show(self):
        self.last_active = time.time()
        if self.vis in ("shown", "entering"):
            return
        if self.vis == "hidden":
            if self.state != "idle":
                self.reset_bird()
            self.win.deiconify()
            self.vis_t = 0.0
        self.win.attributes("-topmost", True)
        self.win.lift()
        self.vis = "entering"

    def hide(self):
        if self.vis in ("shown", "entering"):
            self.vis = "leaving"

    def quit(self):
        self.root.destroy()      # also removes the overlay

    # ---------- input ----------
    def on_press(self, e):
        if self.vis != "shown":
            return
        self.last_active = time.time()
        gy = self.ground
        if math.dist((e.x, e.y), (28, gy - 12)) < 14:
            self.hide()
        elif math.dist((e.x, e.y), (60, gy - 12)) < 14:
            self.quit()
        elif self.state == "idle" and math.dist((e.x, e.y), self.pos) < 48:
            self.state = "drag"

    def on_move(self, e):
        if self.state != "drag":
            return
        self.last_active = time.time()
        dx, dy = e.x - self.ax, e.y - self.ay
        d = math.hypot(dx, dy)
        if d > MAX_PULL:
            dx, dy = dx / d * MAX_PULL, dy / d * MAX_PULL
        self.pos = [self.ax + dx, min(self.ay + dy, self.ground - R)]

    def on_release(self, e):
        if self.state != "drag":
            return
        self.last_active = time.time()
        dx, dy = self.ax - self.pos[0], self.ay - self.pos[1]
        if dx > 8:
            self.vel = [dx * self.power, dy * self.power]
            self.snap = [self.pos[0], self.pos[1], 0]
            self.state = "fly"
            self.sq, self.sqv = -0.5, 0.0
        else:
            self.reset_bird()

    # ---------- physics ----------
    def simulate(self, start, vx, vy, limit=900):
        """Same integration as the real flight, used for the aiming guide."""
        x, y = start
        pts = []
        for i in range(limit):
            vy += G
            x += vx
            y += vy
            if i % 3 == 0:
                pts.append((x, y))
            if y >= self.ground - R or x > self.W + 40:
                break
        return pts, min(x, self.W - R)

    def step_fly(self):
        self.vel[1] += G
        self.pos[0] += self.vel[0]
        self.pos[1] += self.vel[1]
        self.ang = math.atan2(self.vel[1], self.vel[0]) * 0.7
        self.trail()
        if self.pos[1] >= self.ground - R or self.pos[0] > self.W + 40:
            self.on_impact()

    def on_impact(self):
        gy = self.ground
        self.pos[1] = min(self.pos[1], gy - R)
        self.pos[0] = min(self.pos[0], self.W - R)
        self.brightness = self.brightness_from_x(self.pos[0])
        self.set_brightness(self.brightness)
        speed = math.hypot(*self.vel)
        self.burst(max(40, self.pos[0]), gy, self.brightness, speed)
        self.sq, self.sqv = min(1.0, 0.45 + speed / 30), 0.0
        self.shake = 5 + 9 * min(1.0, speed / 25)
        vx = self.vel[0] * 0.38
        if self.pos[0] >= self.W - R:
            vx = -abs(vx)
        self.vel = [vx, -abs(self.vel[1]) * 0.42]
        self.state, self.land_age, self.bounces, self.rest = "land", 0, 0, False

    def step_land(self):
        self.land_age += 1
        gy = self.ground
        if not self.rest:
            self.vel[1] += G
            self.pos[0] += self.vel[0]
            self.pos[1] += self.vel[1]
            self.ang += self.vel[0] / R                       # rolls along the ground
            if self.pos[0] > self.W - R:
                self.pos[0], self.vel[0] = self.W - R, -self.vel[0] * 0.4
            if self.pos[0] < self.x0 - 20:
                self.pos[0], self.vel[0] = self.x0 - 20, abs(self.vel[0]) * 0.4
            if self.pos[1] >= gy - R:
                self.pos[1] = gy - R
                vy = self.vel[1]
                if vy > 2.2 and self.bounces < 3:
                    self.bounces += 1
                    self.sq, self.sqv = min(0.75, vy / 22), 0.0
                    self.shake = max(self.shake, 2.5)
                    self.mini_impact(self.pos[0], gy, vy)
                    self.vel = [self.vel[0] * 0.7, -vy * 0.42]
                else:
                    self.vel[1] = 0.0
                    self.vel[0] *= 0.9
                    if abs(self.vel[0]) < 0.15:
                        self.vel[0], self.rest = 0.0, True
        else:
            a = (self.ang + math.pi) % (2 * math.pi) - math.pi
            self.ang = a * 0.88                                 # settle upright

        if self.land_age >= 125 and self.rest:
            if AUTO_HIDE_AFTER_THROW:
                self.hide()
            else:
                self.reset_bird()
                for _ in range(6):
                    self.add("star", self.ax + random.uniform(-26, 26), self.ay + random.uniform(-26, 26),
                             0, -0.3, random.uniform(20, 34), random.uniform(6, 11), "#ffffff")

    def step(self):
        self.frame += 1
        if self.state == "fly":
            self.step_fly()
        elif self.state == "land":
            self.step_land()
        self.sqv += -0.30 * self.sq - 0.12 * self.sqv          # jelly spring
        self.sq += self.sqv
        if abs(self.sq) < 0.002 and abs(self.sqv) < 0.002:
            self.sq = self.sqv = 0.0
        self.shake = self.shake * 0.86 if self.shake > 0.15 else 0.0
        if self.snap:
            self.snap[2] += 1
            if self.snap[2] > 26:
                self.snap = None
        self.shown += (self.brightness - self.shown) * 0.08
        self.update_fx()

        if self.vis == "entering":
            self.vis_t = min(1.0, self.vis_t + 1 / 24)
            if self.vis_t >= 1.0:
                self.vis = "shown"
        elif self.vis == "leaving":
            self.vis_t = max(0.0, self.vis_t - 1 / 18)
            if self.vis_t <= 0.0:
                self.vis = "hidden"
                self.win.withdraw()
                self.reset_bird()
                self.parts, self.rings = [], []
        elif (self.vis == "shown" and self.state == "idle"
              and time.time() - self.last_active > IDLE_HIDE_SECONDS):
            self.hide()

    # ---------- effects ----------
    def add(self, kind, x, y, vx, vy, life, size, color, g=0.0, drag=1.0, rot=0.0, spin=0.0, label=None):
        self.parts.append(dict(kind=kind, x=x, y=y, vx=vx, vy=vy, life=life, max=life, size=size,
                               color=color, g=g, drag=drag, rot=rot, spin=spin, label=label))

    def trail(self):
        x, y = self.pos
        hue = hue_for(self.brightness_from_x(x))
        u = random.uniform
        self.add("trail", x - 6 + u(-3, 3), y + u(-5, 5), -self.vel[0] * 0.08, u(-0.4, 0.4),
                 u(16, 26), u(5, 9), hue)
        if self.frame % 2 == 0:
            self.add("spark", x + u(-4, 4), y + u(-4, 4), u(-1.5, 1.5), u(-1.5, 1.5),
                     u(18, 34), u(1.8, 3.2), mix(hue, "#ffffff", 0.5), g=0.05)

    def mini_impact(self, x, y, power):
        u = random.uniform
        hue = hue_for(self.brightness)
        for _ in range(int(5 + power * 0.4)):
            a = u(-math.pi * 0.9, -math.pi * 0.1)
            s = u(2, 6)
            self.add("spark", x, y - 4, math.cos(a) * s, math.sin(a) * s, u(16, 30), u(2, 3.5),
                     mix(hue, "#ffffff", 0.4), g=0.3, drag=0.98)
        for side in (-1, 1):
            for _ in range(2):
                self.add("dust", x, y - 6, side * u(0.8, 3), u(-1, -0.2), u(22, 36), u(7, 11),
                         "#d8d0c0", drag=0.95)
        self.add("ripple", x, y + 7, 0, 0, 24, 55 + power * 5, hue)

    def burst(self, x, y, b, speed):
        hue = hue_for(b)
        u = random.uniform
        k = 0.7 + min(1.0, speed / 25) * 0.6
        for _ in range(int((22 + b * 0.25) * k)):                 # glowing sparks
            a = u(-math.pi * 0.95, -math.pi * 0.05)
            s = u(3, 11)
            self.add("spark", x, y - 4, math.cos(a) * s, math.sin(a) * s, u(28, 55), u(2.5, 5),
                     mix(hue, "#ffffff", u(0, 0.6)), g=0.35, drag=0.985)
        for side in (-1, 1):                                       # dust puffs
            for _ in range(8):
                self.add("dust", x + u(-6, 6), y - 6, side * u(1, 5) + u(-1, 1), u(-1.6, -0.2),
                         u(35, 60), u(9, 17), "#d8d0c0", g=-0.01, drag=0.95)
        for _ in range(11):                                        # feathers
            self.add("feather", x, y - R, u(-5, 5), u(-9, -3), u(70, 110), u(6, 10),
                     random.choice(["#e0332b", "#b8221c", "#f0604f", "#ffb11f"]),
                     g=0.2, drag=0.985, rot=u(0, 6.28), spin=u(-0.3, 0.3))
        for _ in range(8):                                         # debris
            self.add("rock", x + u(-14, 14), y - 6, u(-5, 5), u(-11, -4), u(60, 95), u(3, 6),
                     random.choice(["#6b625a", "#8a7f73", "#4e4740"]), g=0.5, drag=0.99,
                     rot=u(0, 6.28), spin=u(-0.4, 0.4))
        for _ in range(14):                                        # comic burst lines
            self.add("streak", x, y - 6, 0, 0, u(14, 22), u(26, 60), mix(hue, "#ffffff", 0.5),
                     rot=u(-math.pi * 0.97, -math.pi * 0.03))
        for _ in range(5):                                         # twinkles
            self.add("star", x + u(-30, 30), y - u(8, 50), u(-0.3, 0.3), u(-0.8, -0.1),
                     u(24, 42), u(8, 16), "#ffffff")
        self.add("flash", x, y, 0, 0, 22, 70 + b * 0.6, hue)
        self.add("ripple", x, y + 7, 0, 0, 34, 150 + b * 1.4, hue)
        self.add("ripple", x, y + 7, 0, 0, 46, 240 + b * 2.2, mix(hue, "#ffffff", 0.6))
        label = "100%  MAX!" if b >= 100 else f"{b}%"
        self.add("text", x, y - 74, 0, -0.9, 80, 22 + b * 0.1, mix(hue, "#ffffff", 0.25), drag=0.97, label=label)
        if b >= 100:
            for _ in range(26):
                a = u(-math.pi, 0)
                s = u(4, 13)
                self.add("spark", x, y - 8, math.cos(a) * s, math.sin(a) * s, u(40, 75), u(3, 6),
                         mix("#ffd84a", "#ffffff", u(0, 0.5)), g=0.3, drag=0.985)
        maxr = max(math.hypot(x - cx, y - cy) for cx in (0, self.W) for cy in (0, self.H))
        self.rings.append(dict(x=x, y=y, age=0, color=hue, maxr=maxr))

    def update_fx(self):
        gy = self.ground
        for p in self.parts:
            p["x"] += p["vx"]
            p["y"] += p["vy"]
            p["vy"] += p["g"]
            p["vx"] *= p["drag"]
            if p["g"] == 0:
                p["vy"] *= p["drag"]
            p["rot"] += p["spin"]
            if p["kind"] == "feather" and p["y"] > gy - 2:
                p["y"], p["vy"], p["vx"], p["spin"] = gy - 2, 0, p["vx"] * 0.8, 0
            elif p["kind"] == "rock" and p["y"] > gy - 2:
                p["y"], p["vy"], p["vx"] = gy - 2, -p["vy"] * 0.4, p["vx"] * 0.7
                if abs(p["vy"]) < 1:
                    p["vy"], p["spin"] = 0, 0
            p["life"] -= 1
        self.parts = [p for p in self.parts if p["life"] > 0]
        for rg in self.rings:
            rg["age"] += 1
        self.rings = [rg for rg in self.rings if rg["age"] < RING_LIFE]

    # ---------- main loop ----------
    def poll_requests(self):
        req, self.req = self.req, None
        if req == "quit":
            self.quit()
            raise SystemExit
        if req == "toggle":
            if self.vis in ("shown", "entering"):
                self.hide()
            else:
                self.show()
        elif req == "show":
            self.show()

    def tick(self):
        try:
            self.poll_requests()
        except SystemExit:
            return
        now = time.perf_counter()
        self.acc = min(self.acc + now - self.last, 0.1)
        self.last = now
        if self.vis == "hidden":
            self.acc = 0.0
            self.update_overlay()
            self.root.after(60, self.tick)
            return
        n = 0
        while self.acc >= STEP:
            self.step()
            self.acc -= STEP
            n += 1
        self.update_overlay()
        if n:
            self.draw()
        self.root.after(8, self.tick)

    # ---------- drawing helpers ----------
    def label(self, x, y, text, size=11, anchor="nw", color="#ffffff"):
        f = ("Segoe UI", size, "bold")
        self.cv.create_text(x + 1, y + 1, text=text, fill="#000000", font=f, anchor=anchor)
        self.cv.create_text(x, y, text=text, fill=color, font=f, anchor=anchor)

    @staticmethod
    def ell(cx, cy, rx, ry, n=22, rot=0.0):
        cr, sr = math.cos(rot), math.sin(rot)
        pts = []
        for i in range(n):
            t = i * 2 * math.pi / n
            px, py = rx * math.cos(t), ry * math.sin(t)
            pts.append((cx + px * cr - py * sr, cy + px * sr + py * cr))
        return pts

    @staticmethod
    def star_pts(cx, cy, r, rot=0.0, inner=0.28, n=4):
        pts = []
        for i in range(n * 2):
            rr = r if i % 2 == 0 else r * inner
            t = rot + i * math.pi / n
            pts.append((cx + rr * math.cos(t), cy + rr * math.sin(t)))
        return pts

    def bubble(self, cx, cy, text, color, size=10):
        w, h, r = 8.4 * len(text) + 18, 22, 8
        x0, x1, y0, y1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        self.cv.create_polygon(pts, smooth=True, fill="#10151f", outline=color, width=2)
        self.cv.create_text(cx, cy, text=text, fill="#ffffff", font=("Segoe UI", size, "bold"))

    def wood(self, p1, p2, w, base="#8a5a2b", dark="#3b2210", light="#b98048"):
        c = self.cv
        c.create_line(*p1, *p2, fill=dark, width=w + 4, capstyle="round")
        c.create_line(*p1, *p2, fill=base, width=w, capstyle="round")
        c.create_line(p1[0] - w * 0.22, p1[1], p2[0] - w * 0.22, p2[1], fill=light, width=max(2, w // 4),
                      capstyle="round")

    # ---------- static layer: the gauge ----------
    def build_static(self):
        c = self.cv
        c.tagname = "static"
        gy, x0, far = self.ground, self.x0, self.far
        c.create_rectangle(x0 - 2, gy - 1, far + 2, gy + 15, fill="#0e121a", outline="")
        for i in range(100):
            xa = x0 + (far - x0) * i / 100
            xb = x0 + (far - x0) * (i + 1) / 100 + 1
            col = gauge_color(i / 99)
            c.create_rectangle(xa, gy + 1, xb, gy + 7, fill=mix(col, "#ffffff", 0.28), outline="")
            c.create_rectangle(xa, gy + 7, xb, gy + 13, fill=col, outline="")
        for p in range(0, 101, 5):
            x = x0 + (far - x0) * p / 100
            major = p % 10 == 0
            y1 = gy + (24 if major else 20)
            c.create_line(x, gy + 15, x, y1, fill="#000000", width=4)
            c.create_line(x, gy + 15, x, y1, fill="#ffffff", width=2)
            if major:
                self.label(x, gy + 25, f"{p}%" if p in (0, 50, 100) else f"{p}", size=9, anchor="n")
        sx, sy = far + 50, gy + 7                                  # sun icon = full brightness
        for k in range(8):
            a = k * math.pi / 4
            c.create_line(sx + 12 * math.cos(a), sy + 12 * math.sin(a), sx + 18 * math.cos(a),
                          sy + 18 * math.sin(a), fill="#ffd84a", width=3, capstyle="round")
        c.create_oval(sx - 9, sy - 9, sx + 9, sy + 9, fill="#ffe36b", outline="#a35f00", width=2)
        c.tagname = "dyn"
        c.tag_lower("static")

    # ---------- slingshot ----------
    def tips(self):
        return (self.ax - 19, self.ay - 32), (self.ax + 19, self.ay - 32)

    def band_end(self):
        if self.state in ("idle", "drag"):
            return tuple(self.pos)
        if self.snap:
            rx, ry, age = self.snap
            p = age / 26
            f = math.exp(-4 * p) * math.cos(10 * p)
            return (self.ax + (rx - self.ax) * f, self.ay + (ry - self.ay) * f)
        return (self.ax, self.ay)

    def band(self, tip, end, w, col):
        self.cv.create_line(*tip, *end, fill="#1a0c04", width=w + 3, capstyle="round")
        self.cv.create_line(*tip, *end, fill=col, width=w, capstyle="round")

    def draw_sling_back(self):
        c, ax, ay, gy = self.cv, self.ax, self.ay, self.ground
        jy = ay + 32
        c.create_oval(ax - 44, gy - 9, ax + 44, gy + 12, fill="#5a5148", outline="#2a241f", width=2)   # stone base
        c.create_oval(ax - 30, gy - 12, ax + 20, gy + 4, fill="#7a7065", outline="")
        c.create_polygon(ax - 11, gy - 3, ax + 11, gy - 3, ax + 7, jy + 6, ax - 7, jy + 6,
                         fill="#8a5a2b", outline="#3b2210", width=2)                                    # trunk
        c.create_line(ax - 6, gy - 8, ax - 4, jy + 12, fill="#b98048", width=3)
        for off in (3, 7):
            c.create_line(ax + off, gy - 10, ax + off - 2, jy + 14, fill="#6e4520", width=1)
        for k in range(3):                                                                             # rope wrap
            y = jy + 18 + k * 5
            c.create_line(ax - 8, y, ax + 8, y + 2, fill="#d9c28a", width=2)
        left, _ = self.tips()
        self.wood((ax - 3, jy + 4), left, 10, base="#6e4520", dark="#2e1a0c", light="#8a5a2b")         # back prong
        c.create_oval(left[0] - 7, left[1] - 7, left[0] + 7, left[1] + 7, fill="#3a2414", outline="#1a0e06", width=2)

    def draw_sling_front(self):
        ax, ay = self.ax, self.ay
        _, right = self.tips()
        self.wood((ax + 3, ay + 36), right, 10)
        self.cv.create_oval(right[0] - 7, right[1] - 7, right[0] + 7, right[1] + 7,
                            fill="#3a2414", outline="#1a0e06", width=2)

    # ---------- bird ----------
    def draw_bird(self, x, y, a, look=(0.0, 0.0), sx=1.0, sy=1.0):
        c = self.cv
        ca, sa = math.cos(a), math.sin(a)

        def T(pts):
            out = []
            for px, py in pts:
                out += [x + (px * ca - py * sa) * sx, y + (px * sa + py * ca) * sy]
            return out

        def circ(lx, ly, r, **kw):
            c.create_polygon(T(self.ell(lx, ly, r, r, n=18)), smooth=True, **kw)

        for dy, col in ((-9, "#7d1511"), (0, "#a31d17"), (9, "#7d1511")):               # tail
            c.create_polygon(T([(-R + 6, dy - 5), (-R - 15, dy * 1.6 - 6), (-R - 9, dy * 1.6 + 2), (-R + 6, dy + 5)]),
                             fill=col, outline="#3c0806", width=1, smooth=True)
        c.create_polygon(T([(-6, -R + 4), (-12, -R - 12), (-3, -R - 4), (1, -R - 16), (5, -R - 3),
                            (11, -R - 11), (11, -R + 5)]), fill="#7d1511", outline="#3c0806", width=1)   # tuft
        circ(0, 0, R, fill="#e0332b", outline="#7d1511", width=2)                                         # body
        c.create_polygon(T(self.ell(3, R * 0.42, R * 0.62, R * 0.46)), fill="#f6dcb4", outline="", smooth=True)
        c.create_polygon(T(self.ell(-6, -R * 0.45, 9, 5, rot=-0.6)), fill="#f26b5a", outline="", smooth=True)
        circ(-9, -12, 2.2, fill="#ffffff", outline="")
        c.create_polygon(T([(R - 7, -2), (R + 11, 4), (R - 7, 5)]), fill="#ffd24a", outline="#a35f00", width=1)
        c.create_polygon(T([(R - 7, 5), (R + 8, 5), (R - 4, 12)]), fill="#f0961a", outline="#a35f00", width=1)
        lx, ly = look
        for ex in (2, 12):                                                                                # eyes
            circ(ex, -4, 6.4, fill="#ffffff", outline="#2a0a08", width=1)
            circ(ex + 1.5 + lx, -3.5 + ly, 2.9, fill="#111111", outline="")
        c.create_line(*T([(-5, -15), (8, -8), (20, -13)]), fill="#1b0b0b", width=5, capstyle="round",
                      joinstyle="round")                                                                  # brow

    # ---------- dynamic drawing ----------
    def draw_rings(self):
        c = self.cv
        for rg in self.rings:
            t = rg["age"] / RING_LIFE
            for delay, strength in ((0.0, 1.0), (0.14, 0.55)):
                tt = (t - delay) / (1 - delay)
                if tt <= 0 or tt >= 1:
                    continue
                r = (1 - (1 - tt) ** 3) * rg["maxr"]
                f = (1 - tt) ** 0.8 * strength
                hue = rg["color"]
                for wm, col in ((13, shade(hue, 0.45)), (6, hue), (2.2, mix(hue, "#ffffff", 0.85))):
                    c.create_oval(rg["x"] - r, rg["y"] - r, rg["x"] + r, rg["y"] + r,
                                  outline=col, width=max(1.0, wm * f))

    def draw_particles(self):
        c = self.cv
        for p in self.parts:
            fr = p["life"] / p["max"]
            x, y, s, col, k = p["x"], p["y"], p["size"], p["color"], p["kind"]
            if k == "spark":
                s *= fr
                c.create_oval(x - s * 2.4, y - s * 2.4, x + s * 2.4, y + s * 2.4, fill=shade(col, 0.4), outline="")
                c.create_oval(x - s, y - s, x + s, y + s, fill=col, outline="")
                c.create_oval(x - s * .4, y - s * .4, x + s * .4, y + s * .4, fill="#ffffff", outline="")
            elif k == "trail":
                s *= fr
                c.create_oval(x - s * 1.7, y - s * 1.7, x + s * 1.7, y + s * 1.7, fill=shade(col, 0.4), outline="")
                c.create_oval(x - s, y - s, x + s, y + s, fill=mix(col, "#ffffff", 0.3), outline="")
            elif k == "dust":
                s *= (0.6 + 0.8 * (1 - fr)) * min(1.0, fr * 3)
                c.create_oval(x - s, y - s, x + s, y + s, fill=col, outline="#a69d8c")
            elif k == "feather":
                s *= min(1.0, fr * 4)
                c.create_polygon(self.ell(x, y, s, s * 0.38, n=10, rot=p["rot"]), fill=col, outline="#3c0806",
                                 smooth=True)
            elif k == "rock":
                s *= min(1.0, fr * 4)
                c.create_polygon(self.ell(x, y, s, s * 0.8, n=6, rot=p["rot"]), fill=col, outline="#2a241f", width=1)
            elif k == "star":
                r = s * math.sin(math.pi * (1 - fr))
                c.create_polygon(self.star_pts(x, y, r * 1.6, 0.0, 0.2), fill=shade("#ffe9a0", 0.7), outline="")
                c.create_polygon(self.star_pts(x, y, r, 0.0, 0.22), fill=col, outline="")
            elif k == "flash":
                r = s * math.sin(math.pi * (1 - fr))
                for rr, cc in ((r, shade(col, 0.5)), (r * 0.66, col), (r * 0.32, "#ffffff")):
                    c.create_oval(x - rr, y - rr * 0.55, x + rr, y + rr * 0.55, fill=cc, outline="")
            elif k == "streak":
                r0 = 14 + 40 * (1 - fr) ** 0.6
                r1 = r0 + s * fr
                ca, sa = math.cos(p["rot"]), math.sin(p["rot"])
                c.create_line(x + ca * r0, y + sa * r0, x + ca * r1, y + sa * r1, fill=col,
                              width=max(1.0, 5 * fr), capstyle="round")
            elif k == "ripple":
                rx = s * (1 - fr) ** 0.55
                if rx > 3:
                    c.create_oval(x - rx, y - rx * 0.16, x + rx, y + rx * 0.16, outline=col, width=max(1.0, 5 * fr))
            elif k == "text":
                size = int(s * (1 + 0.6 * max(0.0, (fr - 0.85) / 0.15)) * min(1.0, fr * 4))
                if size >= 7:
                    self.label(x, y, p["label"], size=size, anchor="center", color=col)

    def draw_gauge_live(self):
        c, gy, x0, far = self.cv, self.ground, self.x0, self.far
        xs = x0 + (far - x0) * max(0.0, min(100.0, self.shown)) / 100
        if xs < far:
            c.create_rectangle(xs, gy + 1, far, gy + 13, fill="#1c222d", outline="")
        c.create_rectangle(x0 - 2, gy - 1, far + 2, gy + 15, outline="#0e121a", width=3)
        hue = hue_for(self.shown)
        self.bubble(xs, gy - 28, f"{int(round(self.shown))}%", hue, size=10)
        c.create_polygon(xs, gy - 1, xs - 6, gy - 14, xs + 6, gy - 14, fill="#ffffff", outline="#10151f", width=2)

    def draw_guide(self):
        c, gy = self.cv, self.ground
        vx, vy = (self.ax - self.pos[0]) * self.power, (self.ay - self.pos[1]) * self.power
        if vx <= 0.5:
            return
        pts, lx = self.simulate(self.pos, vx, vy)
        pb = self.brightness_from_x(lx)
        hue = hue_for(pb)
        for i, (px, py) in enumerate(pts):
            r = max(1.8, 4.6 - i * 0.05)
            c.create_oval(px - r, py - r, px + r, py + r, fill="#ffffff", outline="#222222")
        xm = max(self.x0, min(lx, self.far))
        c.create_oval(xm - 10, gy - 4, xm + 10, gy + 9, outline=hue, width=3)
        c.create_line(xm, gy - 6, xm, gy - 52, fill=hue, width=2, dash=(3, 3))
        self.bubble(xm, gy - 66, "MAX 100%" if pb >= 100 else f"{pb}%", hue, size=11)

    def draw(self):
        c = self.cv
        c.delete("dyn")
        gy, ax, ay = self.ground, self.ax, self.ay

        self.draw_rings()
        self.draw_gauge_live()
        self.draw_sling_back()

        left, right = self.tips()
        end = self.band_end()
        stretch = min(1.0, math.dist(end, (ax, ay)) / MAX_PULL)
        w = max(3, round(8 - 4 * stretch))
        col = mix("#4a2410", "#b8431f", stretch)
        self.band(left, end, w, col)
        c.create_polygon(self.ell(end[0], end[1] + 3, R + 7, R - 3), fill="#5a3a20", outline="#2b1608",
                         width=2, smooth=True)                                    # leather pouch

        if self.state == "drag":
            self.draw_guide()

        bx, by = self.pos
        air = max(0.0, gy - (by + R))
        sh = R * 1.15 * max(0.35, 1 - air / 500)                                  # ground shadow
        c.create_oval(bx - sh, gy - 3, bx + sh, gy + 3, fill="#0b0f16", outline="", stipple="gray50")

        if self.state in ("idle", "drag"):
            if self.state == "idle":
                by += 1.6 * math.sin(self.frame * 0.08)
                a, sx, sy = 0.0, 1.0, 1.0
            else:
                a = math.atan2(ay - by, ax - bx)
                sx, sy = 1 + 0.12 * stretch, 1 - 0.1 * stretch
            mx = self.root.winfo_pointerx() - self.win.winfo_rootx() - bx
            my = self.root.winfo_pointery() - self.win.winfo_rooty() - by
            d = math.hypot(mx, my) or 1.0
            l = min(2.2, d / 60 * 2.2)
            ca, sa = math.cos(a), math.sin(a)
            gx, gyy = mx / d * l, my / d * l
            self.draw_bird(bx, by, a, look=(gx * ca + gyy * sa, -gx * sa + gyy * ca), sx=sx, sy=sy)
        elif self.state == "fly":
            st = min(0.18, math.hypot(*self.vel) / 120)
            self.draw_bird(bx, by, self.ang, look=(2.0, 0.0), sx=1 + st, sy=1 - st * 0.7)
        else:
            sx = max(0.7, min(1.4, 1 + 0.30 * self.sq))
            sy = max(0.5, min(1.35, 1 - 0.34 * self.sq))
            if by >= gy - R - 0.5:
                by += R * (1 - sy)                                                # keep feet on the ground
            self.draw_bird(bx, by, self.ang, look=(0.0, 1.0), sx=sx, sy=sy)
            if self.land_age > 20 and self.land_age < 120:                        # dizzy stars
                for i in range(3):
                    t = self.frame * 0.15 + i * 2.094
                    sx2 = bx + 22 * math.cos(t)
                    sy2 = by - R - 18 + 6 * math.sin(t)
                    c.create_polygon(self.star_pts(sx2, sy2, 7, t, 0.4, 5), fill="#ffd84a", outline="#a35f00")

        self.band(right, end, w, col)
        self.draw_sling_front()
        self.draw_particles()

        # HUD + buttons
        modes = {"checking": "Detecting display...", "real": "Real brightness control",
                 "overlay": "Overlay dimming mode"}
        self.label(self.x0 + 10, gy - 92, modes[self.mode], size=11)
        hint = ("Ctrl+Alt+B show/hide  |  Ctrl+Alt+Q quit" if self.hotkey_ok is not False
                else "Hotkey Ctrl+Alt+B is used by another app")
        self.label(self.x0 + 10, gy - 74, hint, size=8)
        for cx, txt in ((28, "-"), (60, "X")):
            c.create_oval(cx - 12, gy - 24, cx + 12, gy, fill="#222733", outline="white", width=2)
            c.create_text(cx, gy - 12, text=txt, fill="white", font=("Segoe UI", 10, "bold"))

        # screen-level offset: slide in/out + impact shake
        if self.vis == "entering":
            e = ease_out_back(self.vis_t)
        elif self.vis == "leaving":
            e = self.vis_t ** 2
        else:
            e = 1.0
        s = self.shake
        self.apply_offset(random.uniform(-s, s) if s else 0.0, (1 - e) * 210 + (random.uniform(-s, s) * 0.7 if s else 0.0))

    def apply_offset(self, ox, oy):
        dx, dy = ox - self.cur_off[0], oy - self.cur_off[1]
        if dx or dy:
            self.cv.move("static", dx, dy)
        self.cur_off = (ox, oy)
        if ox or oy:
            self.cv.move("dyn", ox, oy)


def main():
    if "--install" in sys.argv:
        install_shortcuts()
        return
    srv = single_instance()
    if srv is None:            # already running: it was told to show itself
        return
    app = App(start_hidden="--hidden" in sys.argv, server=srv)
    app.root.mainloop()


if __name__ == "__main__":
    main()

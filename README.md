# Slingshot Brightness (desktop widget)

An angry bird lives on your desktop. Pull it back to the left, let go, and the
**distance it lands** sets your screen brightness: near = dim, far = bright.

## Run (Windows 10/11)
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```
Or double-click `start.bat` to launch it with no console window.

- Drag the bird left, release. The dotted line previews the flight.
- Click the round **X** button (bottom-left) or press Esc to quit.
- The window is transparent and click-through: you can use your desktop normally.
- Autostart: press `Win+R`, run `shell:startup`, and put a shortcut to `start.bat` there.

## How brightness is controlled
1. **Real brightness** through `screen-brightness-control` (laptops, and monitors with DDC/CI on).
   The app reads the value back to verify it changed.
2. If it didn't, it switches automatically to **overlay dimming**: a click-through black layer
   over your whole screen. Works on any monitor.

The status line at the bottom-left shows which mode is active.
Run `python diagnose.py` to see what your hardware supports.

Brightness never goes below 5% (overlay max 85% dark), so the screen can't go black.

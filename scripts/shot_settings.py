"""Screenshot of the settings window in a given language (a visual check of the layout).

    python scripts/shot_settings.py en D:\temp\shot-en.png
"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biraflow import config, i18n, settings_ui  # noqa: E402
from biraflow.config import Paths  # noqa: E402
from biraflow.store import History  # noqa: E402


class Stub:
    pass


def main() -> int:
    lang = sys.argv[1] if len(sys.argv) > 1 else "en"
    out = Path(sys.argv[2] if len(sys.argv) > 2 else f"shot-{lang}.png")
    page = sys.argv[3] if len(sys.argv) > 3 else "general"

    home = Path(f"%TEMP%") if False else Path.home() / "AppData" / "Local" / "Temp" / f"bf-shot-{lang}"
    home.mkdir(parents=True, exist_ok=True)
    paths = Paths(home, home)
    paths.ensure()
    settings, _ = config.load_settings(paths)
    settings.ui.language = lang
    config.save_settings(paths, settings)
    i18n.set_language(lang)

    root = tk.Tk()
    root.withdraw()
    app = Stub()
    app.root, app.paths, app.enabled = root, paths, True
    app.controller = Stub()
    app.controller.settings, _ = config.load_settings(paths)
    app.controller.history = History(paths.history)
    app.settings_window = None
    app.state = {}
    app.engine_summary = lambda: "Transcription: local · Cleanup: none"
    app.start_capture = app.stop_capture = app.open_settings_file = lambda *a: None
    app.open_data_folder = app.open_log = app.open_readme = lambda *a: None
    app.apply_settings = lambda *a: None

    win = settings_ui.SettingsWindow(app, background=True, layout=True)
    win.show(page)
    win.top.update_idletasks()
    win.top.update()

    import ctypes
    from PIL import ImageGrab

    hwnd = int(win.top.wm_frame(), 16)
    # PrintWindow draws the window even when it is transparent and off-screen
    image = ImageGrab.grab(window=hwnd)
    image.save(out)
    print(f"saved {out} ({image.width}x{image.height})")
    win.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bira Flow logo assets: the "b∞" mark, the wordmark and the app icon, tinted on demand."""
from __future__ import annotations

import winreg
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

APP_NAME = "Bira Flow"
TAGLINE = "Ditado por voz em qualquer aplicativo"
ASSETS = Path(__file__).resolve().parent / "assets"
APP_ICO = ASSETS / "app.ico"
APP_USER_MODEL_ID = "BiraFlow.Dictation"


@lru_cache(maxsize=None)
def _alpha(name: str) -> Image.Image:
    return Image.open(ASSETS / name).getchannel("A")


def _tint(alpha: Image.Image, color: str) -> Image.Image:
    img = Image.new("RGBA", alpha.size, color)
    img.putalpha(alpha)
    return img


def _fit_height(name: str, height: int) -> Image.Image:
    a = _alpha(name)
    width = max(1, round(a.width * height / a.height))
    return a.resize((width, height), Image.LANCZOS)


def mark(size: int, color: str) -> Image.Image:
    """The square "b∞" mark."""
    return _tint(_alpha("mark.png").resize((size, size), Image.LANCZOS), color)


def logo(height: int, color: str) -> Image.Image:
    """Mark + "bira flow" wordmark, as in the brand artwork."""
    return _tint(_fit_height("logo.png", height), color)


def taskbar_uses_light_theme() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False


TRAY_BADGES = {"recording": "#ef4444", "busy": "#f59e0b"}
# the sizes Windows asks for a small icon at 100%…300% scaling
ICON_SIZES = (16, 20, 24, 28, 32, 40, 48, 64)


@lru_cache(maxsize=None)
def _mark_trimmed() -> Image.Image:
    """The mark's alpha cropped to the glyph (no transparent margin), in a square."""
    a = _alpha("mark.png")
    glyph = a.crop(a.getbbox())
    side = max(glyph.size)
    sq = Image.new("L", (side, side), 0)
    sq.paste(glyph, ((side - glyph.width) // 2, (side - glyph.height) // 2))
    return sq


def crisp_mark(size: int) -> Image.Image:
    """The mark's alpha at `size` px, edges tightened at small sizes: a plain downscale
    of the thin strokes leaves them half-transparent, which reads as blurry."""
    a = _mark_trimmed().resize((size, size), Image.LANCZOS)
    if size <= 48:
        lo, hi = (48, 168) if size <= 24 else (32, 200)
        a = a.point(lambda v: 0 if v <= lo else 255 if v >= hi else round((v - lo) * 255 / (hi - lo)))
    return a


def tray_icon(state: str, light_taskbar: bool, size: int = 64) -> Image.Image:
    """Idle: the bare mark in the taskbar's text color, like native tray icons.
    Recording / busy: the mark in white on a colored disc. Disabled: a gray mark."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if state in TRAY_BADGES:
        ImageDraw.Draw(img).ellipse((0, 0, size - 1, size - 1), fill=TRAY_BADGES[state])
        inner = max(8, round(size * 0.72))
        m = _tint(crisp_mark(inner), "#ffffff")
    else:
        color = "#8a8a8a" if state == "disabled" else ("#1c1c1c" if light_taskbar else "#ffffff")
        m = _tint(crisp_mark(size), color)
    img.alpha_composite(m, ((size - m.width) // 2, (size - m.height) // 2))
    return img


def tray_icon_sizes(state: str, light_taskbar: bool) -> list[Image.Image]:
    """One hand-sized image per icon size, so Windows never scales the icon itself."""
    return [tray_icon(state, light_taskbar, s) for s in ICON_SIZES]


def small_icon_size() -> int:
    """Pixel size of a notification-area icon on the main screen (16 at 100% scaling)."""
    try:
        import ctypes

        u = ctypes.windll.user32
        return int(u.GetSystemMetricsForDpi(49, u.GetDpiForSystem())) or 16  # SM_CXSMICON
    except Exception:
        return 16


_icon_cache: dict[int, int] = {}


def _icon_handle(size: int) -> int:
    """HICON of app.ico at exactly `size` px (loaded once; lives as long as the app)."""
    if size not in _icon_cache:
        import ctypes

        load = ctypes.windll.user32.LoadImageW
        load.restype = ctypes.c_void_p
        load.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        _icon_cache[size] = load(None, str(APP_ICO), 1, size, size, 0x10) or 0  # IMAGE_ICON, LR_LOADFROMFILE
    return _icon_cache[size]


def apply_window_icon(window) -> None:
    """Give a Tk window icons at the exact sizes the taskbar (24 px at 100%) and title bar
    (16 px) draw. Tk hands Windows a 32 px icon, which the taskbar shrinks and blurs."""
    try:
        import ctypes

        u = ctypes.windll.user32
        u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        window.update_idletasks()
        hwnd = int(window.wm_frame(), 16)
        dpi = u.GetDpiForWindow(ctypes.c_void_p(hwnd)) or 96
        for which, base in ((1, 24), (0, 16)):  # ICON_BIG, ICON_SMALL
            handle = _icon_handle(round(base * dpi / 96))
            if handle:
                u.SendMessageW(hwnd, 0x0080, which, handle)  # WM_SETICON
    except Exception:
        pass


def set_app_user_model_id() -> None:
    """Group our windows under the Bira Flow icon in the taskbar, not under Python's."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def app_icon(size: int) -> Image.Image:
    """The app icon (white mark on a dark rounded square), drawn at `size` px.
    Used to (re)build assets/app.ico: `python -c "from biraflow import branding; branding.write_app_ico()"`."""
    scale = 4 if size < 64 else 1  # draw the rounded corners supersampled
    big = size * scale
    bg = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(bg).rounded_rectangle((0, 0, big - 1, big - 1), radius=round(big * 0.22), fill=(24, 24, 27, 255))
    img = bg.resize((size, size), Image.LANCZOS) if scale > 1 else bg
    m = _tint(crisp_mark(max(8, round(size * 0.72))), "#ffffff")
    img.alpha_composite(m, ((size - m.width) // 2, (size - m.height) // 2))
    return img


def write_app_ico(path: Path = APP_ICO) -> None:
    # every size Windows uses from 100% to 300% scaling (title bar, taskbar, Alt+Tab, Start)
    sizes = (16, 20, 24, 30, 32, 36, 40, 48, 60, 64, 72, 96, 128, 256)
    images = [app_icon(s) for s in sizes]
    images[-1].save(path, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[:-1])

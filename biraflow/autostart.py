"""Launch at login via HKCU\\...\\Run (per-user, no admin rights needed)."""
from __future__ import annotations

import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "BiraFlow"
OLD_VALUE = "Whisperflow"


def command() -> str:
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return f'"{exe}"'
    launcher = exe.parent / "biraflow.exe"  # gui-script launcher pip puts in Scripts\
    if launcher.is_file():
        return f'"{launcher}"'
    pythonw = exe.with_name("pythonw.exe")
    return f'"{pythonw if pythonw.is_file() else exe}" -m biraflow'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE)
            return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, VALUE)
            except FileNotFoundError:
                pass


def migrate() -> None:
    """Carry "start with Windows" over from the old name, pointing at the new launcher."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            try:
                winreg.QueryValueEx(key, OLD_VALUE)
            except FileNotFoundError:
                return
            winreg.DeleteValue(key, OLD_VALUE)
            winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, command())
    except OSError:
        pass

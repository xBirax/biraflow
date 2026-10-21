"""Notification-area (system tray) icon and menu."""
from __future__ import annotations

import os
import tempfile
import threading
from typing import TYPE_CHECKING

import pystray

from .i18n import t as _t
from . import autostart, branding
from .hotkey import display_hotkey

if TYPE_CHECKING:
    from .app import App

STATES = ("idle", "recording", "busy", "disabled")
TOOLTIPS = {
    "idle": "Bira Flow: pronto",
    "recording": "Bira Flow: gravando",
    "busy": "Bira Flow: transcrevendo",
    "disabled": "Bira Flow: desativado",
}


class _CrispIcon(pystray.Icon):
    """pystray loads the icon at the large-icon size (32 px) and Windows shrinks it to
    16 px for the tray, smearing the thin strokes. Load an exact, pre-rendered size."""

    def _assert_icon_handle(self):
        if self._icon_handle:
            return
        from pystray._util import win32

        images = getattr(self.icon, "all_sizes", None) or [self.icon]
        size = branding.small_icon_size()
        fd, path = tempfile.mkstemp(suffix=".ico")
        os.close(fd)
        try:
            big = images[-1]
            big.save(path, format="ICO", sizes=[im.size for im in images], append_images=images[:-1])
            self._icon_handle = win32.LoadImage(None, path, win32.IMAGE_ICON, size, size, win32.LR_LOADFROMFILE)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class Tray:
    def __init__(self, app: "App"):
        self.app = app
        self._light = branding.taskbar_uses_light_theme()
        self._images = self._render()
        self._state = "idle"
        self.icon = _CrispIcon("BiraFlow", self._images["idle"], TOOLTIPS["idle"], menu=self._menu())

    def _menu(self) -> pystray.Menu:
        a = self.app
        Item = pystray.MenuItem
        return pystray.Menu(
            Item(_t("Configurações…"), lambda: a.open_settings(), default=True),
            Item(_t("Histórico…"), lambda: a.open_settings("history")),
            Item(_t("Dicionário…"), lambda: a.open_settings("dictionary")),
            pystray.Menu.SEPARATOR,
            Item(_t("Ativado"), lambda: a.set_enabled(not a.enabled), checked=lambda _: a.enabled),
            Item(lambda _: _t('Retomar agora (pausado até {0})').format(a.pause_text()) if a.paused_until
                 else _t("Pausar por 1 hora"), lambda: a.resume() if a.paused_until else a.pause(60),
                 enabled=lambda _: bool(a.paused_until) or a.enabled),
            Item(_t("Iniciar com o Windows"), lambda: a.toggle_autostart(),
                 checked=lambda _: autostart.is_enabled()),
            pystray.Menu.SEPARATOR,
            Item(lambda _: _t('Atalho: {0}').format(display_hotkey(a.hotkey_text)), None, enabled=False),
            Item(lambda _: a.engine_summary(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            Item(lambda _: _t('Colar último ditado ({0})').format(display_hotkey(a.paste_last_text))
                 if a.paste_last_text else _t("Colar último ditado"), lambda: a.paste_last(),
                 enabled=lambda _: a.last_text is not None),
            Item(_t("Copiar último ditado"), lambda: a.copy_last(),
                 enabled=lambda _: a.last_text is not None),
            Item(_t("Transcrever de novo o último áudio"), lambda: a.retry_failed(),
                 enabled=lambda _: a.has_failed_audio),
            Item(_t("Transcrever áudio…"), lambda: a.open_audio()),
            Item(lambda _: a.meeting_menu_text(), lambda: a.toggle_meeting(),
                 enabled=lambda _: not a._meeting_busy),
            Item(_t("Iniciar reunião presencial"), lambda: a.toggle_meeting(in_person=True),
                 enabled=lambda _: not a._meeting_busy, visible=lambda _: a.meeting is None),
            Item(_t("Abrir reuniões…"), lambda: a.open_meetings()),
            Item(_t("Aprender com o print copiado"), lambda: a.learn_from_screenshot()),
            Item(lambda _: _t("Desfazer aprendizado: ") + a.last_learned_text() if a.last_learned_text()
                 else _t("Desfazer último aprendizado"), lambda: a.undo_learned(),
                 enabled=lambda _: bool(a.last_learned_text())),
            Item(_t("Abrir pasta de dados"), lambda: a.open_data_folder()),
            pystray.Menu.SEPARATOR,
            Item(_t("Sair"), lambda: a.quit()),
        )

    def start(self) -> None:
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    def stop(self) -> None:
        self.icon.stop()

    def _render(self) -> dict:
        images = {}
        for state in STATES:
            sizes = branding.tray_icon_sizes(state, self._light)
            images[state] = sizes[-1]
            sizes[-1].all_sizes = sizes
        return images

    def set_state(self, state: str) -> None:
        light = branding.taskbar_uses_light_theme()
        if light != self._light:  # the user switched the Windows theme
            self._light = light
            self._images = self._render()
            self._state = ""
        if state != self._state:
            self._state = state
            self.icon.icon = self._images[state]
            self.icon.title = self._tooltip(state)

    def _tooltip(self, state: str) -> str:
        text = _t(TOOLTIPS[state])
        if state == "idle":
            try:
                count, words = self.app.controller.stats.today()
            except Exception:
                count = words = 0
            if count:
                text += "\n" + _t("Hoje: {0} {1}, {2} palavras").format(
                    count, _t("ditado") if count == 1 else _t("ditados"), words)
        if state == "disabled" and getattr(self.app, "paused_until", None):
            text = _t("Bira Flow: pausado até {0}").format(self.app.pause_text())
        session = getattr(self.app, "meeting", None)
        if session is not None:
            from .meeting import clock_text

            text += "\n" + _t("Reunião gravando: {0}").format(clock_text(session.elapsed))
        return text[:127]

    def update_tooltip(self) -> None:
        self.icon.title = self._tooltip(self._state)

    def refresh(self) -> None:
        self.icon.update_menu()

    def notify(self, text: str) -> None:
        try:
            self.icon.notify(text, "Bira Flow")
        except Exception:
            pass

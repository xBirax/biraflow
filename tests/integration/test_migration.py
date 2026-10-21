"""S1.8 of the plan: the 1.1 auto-stop migration on a copy of the user's real settings.toml."""
import re

import pytest

from biraflow.config import Paths, load_settings, load_state, read_text

pytestmark = pytest.mark.integration


def _copy(user, tmp_path, value: int) -> Paths:
    src = user[0].settings
    if not src.is_file():
        pytest.skip("sem settings.toml do usuário")
    p = Paths(tmp_path, tmp_path)
    p.ensure()
    text = re.sub(r"(?m)^max_seconds = \d+", f"max_seconds = {value}", read_text(src))
    p.settings.write_text(text, encoding="utf-8")
    return p


def test_real_settings_300_becomes_1800_once(user, tmp_path):
    before = user[0].settings.read_bytes() if user[0].settings.is_file() else b""
    p = _copy(user, tmp_path, 300)
    assert load_settings(p)[0].recording.max_seconds == 1800
    assert load_settings(p)[0].recording.max_seconds == 1800
    assert load_state(p)["max_seconds_1_1"] is True
    assert user[0].settings.read_bytes() == before          # the user's own file is never touched


def test_real_settings_other_value_kept(user, tmp_path):
    p = _copy(user, tmp_path, 600)
    assert load_settings(p)[0].recording.max_seconds == 600

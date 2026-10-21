from biraflow.config import DEFAULT_SETTINGS_TOML, Paths, Settings, load_env, parse_env, parse_settings


def test_default_template_matches_defaults():
    settings, errors = parse_settings(DEFAULT_SETTINGS_TOML)
    assert errors == []
    assert settings == Settings()


def test_overrides_and_bad_values_reported():
    text = """
[hotkey]
keys = "rctrl"
tap_max_ms = "fast"
[recording]
min_seconds = 1
[transcription]
engine = "GROQ"
vocabulary = ["Kubernetes"]
[output]
method = "teleport"
bogus = 1
[nope]
x = 1
"""
    s, errors = parse_settings(text)
    assert s.hotkey.keys == "rctrl"
    assert s.hotkey.tap_max_ms == 250          # bad value keeps default
    assert s.recording.min_seconds == 1.0 and isinstance(s.recording.min_seconds, float)
    assert s.transcription.engine == "groq"
    assert s.transcription.vocabulary == ["Kubernetes"]
    assert s.output.method == "paste"
    assert len(errors) == 4


def test_invalid_toml():
    s, errors = parse_settings("[hotkey\nkeys=")
    assert s == Settings() and len(errors) == 1


def test_parse_env():
    env = parse_env("""
# comment
GROQ_API_KEY = gsk_123
export OPENAI_API_KEY="sk-abc # not a comment"
ANTHROPIC_API_KEY=sk-ant  # trailing comment
EMPTY=
""")
    assert env == {"GROQ_API_KEY": "gsk_123", "OPENAI_API_KEY": "sk-abc # not a comment",
                   "ANTHROPIC_API_KEY": "sk-ant"}


def test_load_env_user_file_and_process_env(tmp_path, monkeypatch):
    paths = Paths(tmp_path, tmp_path)
    paths.env.write_text("GROQ_API_KEY=from-file\nOPENAI_API_KEY=file\n", encoding="utf-8")
    monkeypatch.setattr("biraflow.config.PROJECT_ROOT", tmp_path / "noproject")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "from-process")
    env = load_env(paths)
    assert env["GROQ_API_KEY"] == "from-file"
    assert env["OPENAI_API_KEY"] == "from-process"


def test_render_roundtrip_keeps_comments_and_values(tmp_path):
    from biraflow.config import render_settings, save_settings, load_settings

    assert render_settings(Settings()) == DEFAULT_SETTINGS_TOML
    s = Settings()
    s.hotkey.keys = "rctrl"
    s.recording.silence_threshold = 0.0005
    s.transcription.vocabulary = ['Kubernetes', 'Ubiratan "Bira"', "ção"]
    s.cleanup.extra_instructions = "Use português do Brasil.\nSem emojis.\x7f"   # TOML forbids a raw DEL
    s.output.restore_clipboard = True
    paths = Paths(tmp_path, tmp_path)
    save_settings(paths, s)
    loaded, errors = load_settings(paths)
    assert errors == [] and loaded == s
    assert "# Segure para falar" in paths.settings.read_text(encoding="utf-8")


def test_save_user_env_updates_in_place(tmp_path):
    from biraflow.config import read_user_env, save_user_env

    paths = Paths(tmp_path, tmp_path)
    paths.env.write_text("# minhas chaves\nGROQ_API_KEY=old\nOTHER=1\n", encoding="utf-8")
    save_user_env(paths, {"GROQ_API_KEY": "new", "OPENAI_API_KEY": "sk-x"})
    text = paths.env.read_text(encoding="utf-8")
    assert text.startswith("# minhas chaves\nGROQ_API_KEY=new\nOTHER=1\n")
    assert read_user_env(paths) == {"GROQ_API_KEY": "new", "OTHER": "1", "OPENAI_API_KEY": "sk-x"}


def test_save_user_env_keeps_a_backup_and_replaces_duplicates(tmp_path):
    """Uma chave repetida em duas linhas: as duas são atualizadas (o app lê a última) e o
    arquivo anterior fica em .env.bak — errar uma chave não pode custar o acesso que funcionava."""
    from biraflow.config import backup_env_path, read_user_env, save_user_env

    paths = Paths(tmp_path, tmp_path)
    paths.env.write_text("GROQ_API_KEY=velha\nOPENAI_API_KEY=sk\nGROQ_API_KEY=velha2\n", encoding="utf-8")
    save_user_env(paths, {"GROQ_API_KEY": "nova"})
    text = paths.env.read_text(encoding="utf-8")
    assert text.count("GROQ_API_KEY=nova") == 2 and "velha" not in text
    assert read_user_env(paths)["GROQ_API_KEY"] == "nova"
    assert backup_env_path(paths).read_text(encoding="utf-8").count("velha") == 2


def test_migrate_from_old_name_moves_folders_once(tmp_path):
    from biraflow.config import legacy_dirs

    old = Paths(tmp_path / "Roaming" / "Whisperflow", tmp_path / "Local" / "Whisperflow")
    new = Paths(tmp_path / "Roaming" / "BiraFlow", tmp_path / "Local" / "BiraFlow")
    old.ensure()
    (old.data / "settings.toml").write_text("x", encoding="utf-8")
    (old.data / "whisperflow.log").write_text("log", encoding="utf-8")
    (old.models_dir / "ggml-small.bin").write_bytes(b"model")
    moved = new.migrate_from(old)
    assert len(moved) == 2
    assert (new.data / "settings.toml").read_text(encoding="utf-8") == "x"
    assert (new.data / "biraflow.log").exists()
    assert (new.models_dir / "ggml-small.bin").read_bytes() == b"model"
    assert not old.data.exists() and not old.local.exists()
    assert new.migrate_from(old) == []          # nothing left to move

    old.ensure()                                 # both exist: keep both, report the old ones
    assert new.migrate_from(old) == []
    assert set(legacy_dirs(new)) == {old.data, old.local}


def test_migrate_from_adopts_the_keys_of_the_old_folder(tmp_path):
    """A pasta nova já existia (uma instalação nova veio antes): a antiga fica onde está, mas as
    chaves que só ela tem passam para a que está em uso — atualizar não pode custar o acesso."""
    from biraflow.config import read_user_env

    old = Paths(tmp_path / "Roaming" / "Whisperflow", tmp_path / "Local" / "Whisperflow")
    new = Paths(tmp_path / "Roaming" / "BiraFlow", tmp_path / "Local" / "BiraFlow")
    old.ensure()
    new.ensure()
    (old.data / ".env").write_text("GROQ_API_KEY=gsk_da_antiga\nOPENAI_API_KEY=sk_antiga\n", encoding="utf-8")
    (new.data / ".env").write_text("OPENAI_API_KEY=sk_nova\n", encoding="utf-8")
    moved = new.migrate_from(old)
    env = read_user_env(new)
    assert env["GROQ_API_KEY"] == "gsk_da_antiga"     # adotada da pasta antiga
    assert env["OPENAI_API_KEY"] == "sk_nova"         # a que estava em uso manda
    assert moved == [str(new.env)]
    assert (old.data / ".env").is_file()              # a pasta antiga não é tocada
    assert new.migrate_from(old) == []                # e não falta mais nada


def test_migrate_from_adopts_the_keys_when_the_new_folder_has_no_env(tmp_path):
    """A mudança de nome falhou (pasta antiga presa) e a nova nasceu vazia: as chaves vêm mesmo assim."""
    from biraflow.config import read_user_env

    old = Paths(tmp_path / "Roaming" / "Whisperflow", tmp_path / "Local" / "Whisperflow")
    new = Paths(tmp_path / "Roaming" / "BiraFlow", tmp_path / "Local" / "BiraFlow")
    old.ensure()
    new.ensure()
    (old.data / ".env").write_text("GROQ_API_KEY=gsk_da_antiga\n", encoding="utf-8")
    assert new.migrate_from(old) == [str(new.env)]
    assert read_user_env(new)["GROQ_API_KEY"] == "gsk_da_antiga"


def test_parse_env_quoted_value_with_comment():
    assert parse_env('GROQ_API_KEY="gsk_abc" # minha chave\nB=\'x y\'') == {"GROQ_API_KEY": "gsk_abc", "B": "x y"}



def test_settings_backup_recovers_a_broken_file(tmp_path):
    from biraflow.config import backup_path, load_settings, save_settings

    paths = Paths(tmp_path, tmp_path)
    s = Settings()
    s.hotkey.keys = "f9"
    s.hotkey.ignore_apps = ["cs2.exe"]
    save_settings(paths, s)
    loaded, errors = load_settings(paths)        # a good file becomes the backup
    assert loaded.hotkey.ignore_apps == ["cs2.exe"] and not errors
    assert backup_path(paths).is_file()
    paths.settings.write_text("", encoding="utf-8")   # emptied by hand: valid TOML, but not a backup
    load_settings(paths)
    paths.settings.write_text("[hotkey\nkeys = ", encoding="utf-8")   # broken by hand
    loaded, errors = load_settings(paths)
    assert loaded.hotkey.keys == "f9" and "inválido" in errors[0]


def test_new_options_are_in_the_template():
    s, errors = parse_settings(DEFAULT_SETTINGS_TOML)
    assert not errors and s.hotkey.ignore_apps == [] and s.output.smart_insert is True


def test_email_style_default_bumped_once(tmp_path):
    from biraflow.config import load_settings

    paths = Paths(tmp_path, tmp_path)
    old = DEFAULT_SETTINGS_TOML.replace('email = "email"', 'email = "formal"')
    paths.settings.write_text(old, encoding="utf-8")
    assert load_settings(paths)[0].styles.email == "email"     # the old default becomes the e-mail layout
    paths.settings.write_text(old, encoding="utf-8")            # chosen again later: the user's choice
    assert load_settings(paths)[0].styles.email == "formal"

"""Settings (settings.toml), API keys (.env) and on-disk locations."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "BiraFlow"
OLD_APP_NAME = "Whisperflow"  # name before the rename; its folders are migrated
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Paths:
    data: Path    # %APPDATA%\BiraFlow: settings.toml, .env, state, log
    local: Path   # %LOCALAPPDATA%\BiraFlow: whisper.cpp, models, temp audio

    @classmethod
    def default(cls) -> "Paths":
        home = os.environ.get("BIRAFLOW_HOME") or os.environ.get("WHISPERFLOW_HOME")
        if home:
            return cls(Path(home), Path(home))
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        paths = cls(Path(appdata) / APP_NAME, Path(localappdata) / APP_NAME)
        paths.migrate_from(cls(Path(appdata) / OLD_APP_NAME, Path(localappdata) / OLD_APP_NAME))
        return paths

    def migrate_from(self, old: "Paths") -> list[str]:
        """The app used to be called Whisperflow: move its folders (settings, keys,
        history, whisper.cpp and the downloaded model) over, once. A rename on the
        same drive is instant, so the 0.5–1.5 GB model is not copied.

        When the new folder already exists (a fresh install came first), the old folder stays
        where it is — but the keys only it has are adopted: an upgrade must not cost the user
        the access that was working."""
        moved = []
        for src, dst in ((old.data, self.data), (old.local, self.local)):
            if src.is_dir() and not dst.exists():
                try:
                    os.replace(src, dst)
                    moved.append(str(dst))
                    log_file = dst / "whisperflow.log"
                    if log_file.exists():
                        os.replace(log_file, dst / "biraflow.log")
                except OSError as e:
                    log.warning("could not migrate %s -> %s: %s", src, dst, e)
            elif src.is_dir() and dst.is_dir() and src.resolve() != dst.resolve():
                moved += self._adopt_keys(src, dst)
        return moved

    def _adopt_keys(self, src: Path, dst: Path) -> list[str]:
        """Keys the old folder has and the one in use does not. Never overwrites a working key."""
        try:
            old_env = parse_env(read_text(src / ".env"))
        except OSError:
            return []
        try:
            new_env = parse_env(read_text(dst / ".env"))
        except OSError:   # the new folder has no keys yet: adopt them all
            new_env = {}
        missing = {k: v for k, v in old_env.items() if v and not new_env.get(k)}
        if not missing:
            return []
        save_user_env(self, missing)   # keeps the current file and adds only what is missing
        log.info("migrated %d key(s) from %s", len(missing), src / ".env")
        return [str(self.env)]

    @property
    def settings(self) -> Path:
        return self.data / "settings.toml"

    @property
    def env(self) -> Path:
        return self.data / ".env"

    @property
    def state(self) -> Path:
        return self.data / "state.json"

    @property
    def log(self) -> Path:
        return self.data / "biraflow.log"

    @property
    def dictionary(self) -> Path:
        return self.data / "dictionary.json"

    @property
    def history(self) -> Path:
        return self.data / "history.json"

    @property
    def stats(self) -> Path:
        return self.data / "stats.json"

    @property
    def sounds_dir(self) -> Path:
        return self.local / "sounds"

    @property
    def whisper_dir(self) -> Path:
        return self.local / "whisper"

    @property
    def models_dir(self) -> Path:
        return self.local / "models"

    @property
    def tmp_dir(self) -> Path:
        return self.local / "tmp"

    def ensure(self) -> None:
        for d in (self.data, self.local, self.whisper_dir, self.models_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)


def legacy_dirs(current: Paths) -> list[Path]:
    """Folders left from before the rename, if the current ones are separate from them."""
    if os.environ.get("BIRAFLOW_HOME") or os.environ.get("WHISPERFLOW_HOME"):
        return []
    out = []
    for new in (current.data, current.local):
        old = new.with_name(OLD_APP_NAME)
        if old.is_dir() and new.is_dir() and old.resolve() != new.resolve():
            out.append(old)
    return out


# --- settings -----------------------------------------------------------------

@dataclass
class HotkeyCfg:
    keys: str = "ctrl+win"
    tap_max_ms: int = 250
    double_tap_ms: int = 400
    suppress_keys: bool = True
    command_keys: str = "ctrl+win+alt"
    paste_last_keys: str = "shift+alt+z"
    ignore_apps: list = field(default_factory=list)


@dataclass
class RecordingCfg:
    max_seconds: int = 1800
    min_seconds: float = 0.3
    input_device: str = ""
    silence_threshold: float = 0.003
    keep_mic_ready: int = 0
    handsfree_silence_stop: int = 60
    normalize: bool = True


@dataclass
class TranscriptionCfg:
    engine: str = "auto"
    language: str = "auto"
    vocabulary: list = field(default_factory=list)
    local_model: str = "small"
    whisper_cli: str = ""
    threads: int = 0
    local_fast: bool = True
    keep_loaded: bool = True
    groq_model: str = "whisper-large-v3-turbo"
    openai_model: str = "whisper-1"
    gateway_model: str = "spacexai/grok-stt"
    fallback_to_local: bool = True
    timeout_seconds: int = 20
    # Set per dictation, never saved: the previous sentence, as context for Whisper.
    context: str = ""


@dataclass
class CleanupCfg:
    provider: str = "auto"
    model: str = ""
    timeout_seconds: int = 8
    extra_instructions: str = ""
    ollama_url: str = "http://localhost:11434/v1/chat/completions"
    local_rules: bool = True
    spoken_punctuation: bool = True


@dataclass
class OutputCfg:
    method: str = "paste"
    restore_clipboard: bool = False
    paste_keys: str = "ctrl+v"
    trailing_space: bool = True
    smart_insert: bool = True
    send_command: bool = True
    send_phrases: list = field(default_factory=lambda: ["pressione enter", "aperte enter", "press enter"])


@dataclass
class UiCfg:
    show_pill: bool = True
    position: str = "bottom"
    pill_size: str = "medium"
    show_preview: bool = True
    sounds: bool = True
    mute_media: bool = False
    # Language of the interface: "auto" follows the language of Windows.
    language: str = "auto"


@dataclass
class StylesCfg:
    enabled: bool = True
    personal: str = "casual"
    work: str = "casual"
    email: str = "email"
    code: str = "technical"
    other: str = "neutral"


@dataclass
class HistoryCfg:
    keep: str = "always"
    max_items: int = 500
    auto_learn: bool = True
    learn_from_edits: bool = True


@dataclass
class UpdatesCfg:
    auto: bool = True
    url: str = ""


@dataclass
class WakeCfg:
    enabled: bool = False
    phrase: str = "Ei Bira"
    end_silence: float = 1.5
    confirm: bool = True

    def normalize(self) -> None:
        self.phrase = " ".join(self.phrase.split())[:40] or "Ei Bira"
        self.end_silence = min(5.0, max(0.8, float(self.end_silence)))


@dataclass
class MeetingCfg:
    system_audio: bool = True
    keep_audio: bool = True
    auto_notes: bool = True
    max_hours: int = 4

    def normalize(self) -> None:
        self.max_hours = min(8, max(1, int(self.max_hours)))


@dataclass
class Settings:
    hotkey: HotkeyCfg = field(default_factory=HotkeyCfg)
    recording: RecordingCfg = field(default_factory=RecordingCfg)
    transcription: TranscriptionCfg = field(default_factory=TranscriptionCfg)
    cleanup: CleanupCfg = field(default_factory=CleanupCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    ui: UiCfg = field(default_factory=UiCfg)
    styles: StylesCfg = field(default_factory=StylesCfg)
    history: HistoryCfg = field(default_factory=HistoryCfg)
    updates: UpdatesCfg = field(default_factory=UpdatesCfg)
    wake: WakeCfg = field(default_factory=WakeCfg)
    meeting: MeetingCfg = field(default_factory=MeetingCfg)


STYLE_CHOICES = ("neutral", "formal", "casual", "very_casual", "technical", "email")

_CHOICES = {
    ("transcription", "engine"): ("local", "groq", "openai", "gateway", "auto"),
    ("cleanup", "provider"): ("auto", "groq", "openai", "anthropic", "vercel", "ollama", "none"),
    ("output", "method"): ("paste", "type"),
    ("ui", "position"): ("bottom", "top", "bottom-left", "bottom-right", "top-left", "top-right"),
    ("ui", "pill_size"): ("small", "medium", "large"),
    ("history", "keep"): ("always", "24h", "never"),
    ("ui", "language"): ("auto", "pt-BR", "en", "es", "fr", "de", "it", "zh-Hans", "ja", "ko"),
    **{("styles", c): STYLE_CHOICES for c in ("personal", "work", "email", "code", "other")},
}

DEFAULT_SETTINGS_TOML = """\
# Bira Flow — configurações
# As mudanças são aplicadas sozinhas alguns segundos depois de salvar.
# Chaves de API ficam no arquivo .env desta mesma pasta, nunca aqui.

[hotkey]
# Segure para falar; toque duas vezes para o modo mãos livres; Esc cancela.
# Exemplos: "ctrl+win", "rctrl", "ralt", "f9", "capslock", "ctrl+alt+space", "vkE9"
# A tecla Fn não chega ao Windows na maioria dos teclados (veja o README).
keys = "ctrl+win"
# Apertar e soltar mais rápido que isto (ms) é um "toque", não um "segurar".
tap_max_ms = 250
# Tempo máximo (ms) entre os dois toques do toque duplo.
double_tap_ms = 400
# Impede que teclas comuns do atalho cheguem ao app (ex.: CapsLock não alterna).
# Modificadores (Ctrl, Shift, Alt, Win) nunca são bloqueados. Esc nunca é bloqueado.
suppress_keys = true
# Modo comando: selecione um texto, segure isto e diga o que fazer com ele
# ("deixe mais formal", "traduza para o inglês"). Sem seleção, escreve o que você pedir.
# Requer um provedor de LLM. Vazio desativa.
command_keys = "ctrl+win+alt"
# Cola de novo o último ditado. Vazio desativa.
paste_last_keys = "shift+alt+z"
# O atalho não faz nada com estes apps em foco (ex.: jogos). Nomes do .exe:
# ["valorant.exe", "cs2.exe"]
ignore_apps = []

[recording]
# Parada automática (segundos), também no modo mãos livres. Até 3600 (1 hora).
max_seconds = 1800
# Gravações mais curtas que isto são descartadas.
min_seconds = 0.3
# Vazio = microfone padrão do Windows. Ou parte do nome, ex.: "USB".
input_device = ""
# Volume (RMS, 0–1) abaixo do qual a gravação conta como silêncio. 0 desliga.
silence_threshold = 0.003
# Aumenta o volume de falas baixas (sussurro, microfone distante) antes de transcrever.
normalize = true
# Segundos que o microfone fica aberto depois de um ditado, para o próximo não cortar a
# primeira sílaba (o ícone de microfone do Windows fica aceso nesse tempo). 0 desliga.
keep_mic_ready = 0
# No modo mãos livres, para sozinho depois de tantos segundos de silêncio. 0 desliga.
handsfree_silence_stop = 60

[transcription]
# "auto" (padrão): Groq, OpenAI ou Vercel AI Gateway conforme a chave no .env; sem nenhuma, o
# modelo local. "groq" / "openai" / "gateway" fixam um motor. "local": whisper.cpp, offline.
engine = "auto"
# "auto" detecta o idioma, mas custa ~2 s a mais por ditado no modelo local.
# Fixe um código para ficar rápido: "pt", "en", "es"...
language = "auto"
# Nomes e termos que o reconhecimento deve acertar, ex.: ["Kubernetes", "Ubiratan"]
vocabulary = []
# Modelo local: "small", "medium", "large-v3-turbo"... ou caminho para um .bin
local_model = "small"
# Vazio = %LOCALAPPDATA%\\BiraFlow\\whisper\\whisper-cli.exe
whisper_cli = ""
# 0 = automático
threads = 0
# Decodificação gulosa + janela do codificador do tamanho da fala: ~2,5x mais rápido.
local_fast = true
# Com placa de vídeo (whisper.cpp CUDA), mantém o modelo carregado na GPU entre os
# ditados: resposta em ~0,3 s. Sem GPU não tem efeito.
keep_loaded = true
groq_model = "whisper-large-v3-turbo"
openai_model = "whisper-1"
# Vercel AI Gateway (chave AI_GATEWAY_API_KEY): o time pode ter uma Model Allowlist — se o
# modelo abaixo não estiver liberado, o app tenta o openai/whisper-1 sozinho.
gateway_model = "spacexai/grok-stt"
# Se a API falhar (sem internet, fora do ar, limite de uso ou demora), usa o modelo local.
fallback_to_local = true
# Segundos esperando a API antes de desistir (a Groq costuma responder em menos de 1 s).
timeout_seconds = 20

[cleanup]
# Limpeza do texto por LLM (vícios de fala, pontuação, maiúsculas).
# "auto" usa a primeira chave do .env: Groq, OpenAI, Anthropic, Vercel AI Gateway.
# Sem chave, nada é enviado.
# "ollama" roda 100% local. "none" desliga.
provider = "auto"
# Vazio = padrão do provedor.
model = ""
# Se o LLM demorar mais que isto, o texto bruto é inserido.
timeout_seconds = 8
# Instruções extras, ex.: "Use português do Brasil." ou "Write in British English."
extra_instructions = ""
ollama_url = "http://localhost:11434/v1/chat/completions"
# Limpeza local, sem internet: tira "hã", "hum", palavras repetidas e ajusta maiúsculas.
local_rules = true
# Converte comandos falados: "vírgula", "ponto final", "ponto de interrogação",
# "nova linha", "novo parágrafo", "abre/fecha aspas", "abre/fecha parênteses".
spoken_punctuation = true

[output]
# "paste" (área de transferência + Ctrl+V) ou "type" (digita caractere por caractere)
method = "paste"
# false: o ditado fica na área de transferência para colar de novo.
# true: devolve o que estava na área de transferência antes.
restore_clipboard = false
# Teclas de colar. Alguns terminais preferem "shift+insert".
paste_keys = "ctrl+v"
# Acrescenta um espaço ao final para ditados seguidos não grudarem.
trailing_space = true
# Olha o texto antes do cursor: no meio de uma frase, começa com minúscula
# ("vou levar o arquivo") e põe o espaço que faltar.
smart_insert = true
# Termine o ditado com uma destas frases para apertar Enter depois de colar (enviar a mensagem).
send_command = true
send_phrases = ["pressione enter", "aperte enter", "press enter"]

[ui]
show_pill = true
# Onde a pílula aparece, na tela da janela ativa:
# "bottom", "top", "bottom-left", "bottom-right", "top-left", "top-right"
position = "bottom"
# "small", "medium" ou "large"
pill_size = "medium"
# Mostra por alguns segundos o texto que acabou de ser inserido.
show_preview = true
# Sons curtos ao começar, terminar e cancelar.
sounds = true
# Silencia a música e outros apps enquanto você dita.
mute_media = false
# Idioma da interface: "auto" segue o idioma do Windows.
# Outros: "pt-BR", "en", "es", "fr", "de", "it", "zh-Hans", "ja", "ko".
language = "auto"

[styles]
# Tom do texto conforme o app em que você dita (aplicado pelo LLM).
# Opções: "neutral", "formal", "casual", "very_casual", "technical", "email"
# ("email": saudação, parágrafos curtos com linha em branco entre eles e despedida em linhas próprias)
enabled = true
# WhatsApp, Telegram, Signal, Messenger, Discord, Instagram
personal = "casual"
# Slack, Teams, Google Chat, Zoom
work = "casual"
# Outlook, Thunderbird, Gmail e outros e-mails no navegador
email = "email"
# VS Code, Cursor, JetBrains, terminais
code = "technical"
# Todo o resto
other = "neutral"

[history]
# "always" (guarda os últimos max_items), "24h" (apaga depois de um dia) ou "never".
keep = "always"
max_items = 500
# Ao corrigir um ditado no histórico, as trocas viram regras do dicionário.
auto_learn = true
# Ao ajustar no próprio app um texto que acabou de ser ditado ("ese" -> "esse"), a troca
# vira uma correção automaticamente. Lê só o campo onde o texto foi colado, por até 5 min.
learn_from_edits = true

[updates]
# Na versão instalada, baixa e instala novas versões sozinho (só quando você não está
# ditando). O instalador só é aceito se tiver a assinatura digital do Bira Flow.
auto = true
# Vazio = o canal de releases definido no build.
url = ""

[wake]
# Ativação por voz: diga a frase (ex.: "Ei Bira") e o ditado começa, sem tocar no teclado.
# Um detector local (Vosk, ~32 MB) escuta o microfone o tempo todo. Quando ele ouve algo
# parecido com a frase, só esse trecho (~2,5 s) é conferido pelo Whisper: no seu PC, se o
# modelo local estiver instalado; senão, pela API de transcrição.
enabled = false
phrase = "Ei Bira"
# Segundos de silêncio que encerram o ditado por voz.
end_silence = 1.5
# Confirma a frase com o Whisper antes de começar (evita disparar com "ei, vira aqui").
# false: decide só com o detector local (dispara por engano mais vezes).
confirm = true

[meeting]
# Grava também o que o PC toca (o outro lado da chamada).
system_audio = true
# Guarda o áudio (Opus estéreo, ~21 MB/h) na pasta da reunião. false: apaga depois de transcrever.
keep_audio = true
# Gera as notas (resumo, decisões, próximos passos) ao encerrar.
auto_notes = true
# Encerra sozinho depois de tantas horas (1 a 8).
max_hours = 4
"""


def _coerce(section: str, key: str, default, value):
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
    elif isinstance(default, int):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif isinstance(default, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    elif isinstance(default, str):
        if isinstance(value, str):
            choices = _CHOICES.get((section, key))
            if choices and value.strip().lower() not in choices:
                raise ValueError(f"[{section}] {key} deve ser um de: {', '.join(choices)}")
            return value.strip().lower() if choices else value
    elif isinstance(default, list):
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return value
    raise ValueError(f"[{section}] {key}: valor inválido {value!r}")


def parse_settings(text: str) -> tuple[Settings, list[str]]:
    """Parse settings TOML over the defaults. Bad values keep their default and are reported."""
    settings = Settings()
    errors: list[str] = []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return settings, [f"settings.toml inválido: {e}"]
    for name, raw in data.items():
        section = getattr(settings, name, None)
        if section is None or not isinstance(raw, dict):
            errors.append(f"Seção desconhecida em settings.toml: [{name}]")
            continue
        known = {f.name for f in fields(section)}
        for key, value in raw.items():
            if key not in known:
                errors.append(f"Opção desconhecida: [{name}] {key}")
                continue
            try:
                setattr(section, key, _coerce(name, key, getattr(section, key), value))
            except ValueError as e:
                errors.append(str(e))
    settings.wake.normalize()
    settings.meeting.normalize()
    return settings, errors


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    # JSON strings are valid TOML basic strings, except that TOML also forbids a raw DEL
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def render_settings(settings: Settings) -> str:
    """The commented default template with every value replaced by `settings`'."""
    lines = []
    section = None
    for line in DEFAULT_SETTINGS_TOML.splitlines():
        if m := re.match(r"^\[(\w+)\]\s*$", line):
            section = getattr(settings, m.group(1), None)
        elif (m := re.match(r"^(\w+) = ", line)) and section is not None and hasattr(section, m.group(1)):
            line = f"{m.group(1)} = {_toml_value(getattr(section, m.group(1)))}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def read_text(path: Path) -> str:
    """UTF-8 (with or without BOM); a file saved as ANSI by an old editor is read as
    Windows-1252 instead of crashing the app on its first accent."""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        log.warning("%s não está em UTF-8; lendo como Windows-1252", path.name)
        text = data.decode("cp1252", errors="replace")
    return text.replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))  # like text mode: no stray CR on rewrite



def write_atomic(path: Path, text: str) -> None:
    """Write through a temp file + rename: a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:   # antivirus, the search indexer or a reader has it open for a moment
            if attempt == 19:
                raise
            time.sleep(0.05)


def backup_path(paths: Paths) -> Path:
    return paths.settings.with_name(paths.settings.name + ".bak")


def backup_env_path(paths: Paths) -> Path:
    return paths.env.with_name(paths.env.name + ".bak")


def save_settings(paths: Paths, settings: Settings) -> None:
    paths.settings.parent.mkdir(parents=True, exist_ok=True)
    text = render_settings(settings)
    _backup(paths)
    write_atomic(paths.settings, text)


def _backup(paths: Paths) -> None:
    """Keep the last settings file that parsed cleanly, to recover from a broken edit."""
    try:
        text = read_text(paths.settings)
        if tomllib.loads(text):   # an emptied file parses too, and must not replace the good backup
            write_atomic(backup_path(paths), text)
    except (OSError, tomllib.TOMLDecodeError):
        pass


def load_settings(paths: Paths) -> tuple[Settings, list[str]]:
    if not paths.settings.exists():
        write_atomic(paths.settings, DEFAULT_SETTINGS_TOML)
    text = read_text(paths.settings)
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        bak = backup_path(paths)
        if bak.is_file():
            settings, errors = parse_settings(read_text(bak))
            return settings, [f"settings.toml inválido ({e}); usando a última versão boa "
                              f"({bak.name}) até você corrigir o arquivo."] + errors
    else:
        _backup(paths)
        text = _migrate(paths, text)
    return parse_settings(text)


# (state flag, old default, new default): 1.1 raised the auto-stop from 5 to 30 min and lays
# e-mail out as an e-mail instead of only making it formal
_BUMPS = (("max_seconds_1_1", "max_seconds = 300", f"max_seconds = {RecordingCfg.max_seconds}"),
          ("email_style_1_1", 'email = "formal"', f'email = "{StylesCfg.email}"'))


def _migrate(paths: Paths, text: str) -> str:
    """settings.toml stores every value, so an old default sits in everyone's file: bump it
    once. Any other value was the user's choice (and so is the old one set again later)."""
    state = load_state(paths)
    pending = [b for b in _BUMPS if not state.get(b[0])]
    if not pending:
        return text
    new = text
    for flag, old, value in pending:
        new, n = re.subn(rf"(?m)^{re.escape(old)}[ \t]*$", value, new)
        if n:
            log.info("settings: %s -> %s (1.1 default)", old, value)
        state[flag] = True
    if new != text:
        write_atomic(paths.settings, new)
    save_state(paths, state)
    return new


# --- .env ---------------------------------------------------------------------

ENV_KEYS = ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AI_GATEWAY_API_KEY")


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ("\"", "'") and value.find(value[0], 1) > 0:
            value = value[1:value.find(value[0], 1)]  # quoted: anything after the quote is a comment
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and value:
            values[key] = value
    return values


def env_files(paths: Paths) -> list[Path]:
    # Later files win: the project .env, then the per-user one.
    return [PROJECT_ROOT / ".env", paths.env]


def load_env(paths: Paths) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in env_files(paths):
        try:
            values.update(parse_env(read_text(path)))
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Não foi possível ler %s: %s", path, e)
    for key in ENV_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def read_user_env(paths: Paths) -> dict[str, str]:
    """Only the per-user .env (the one the settings window edits)."""
    try:
        return parse_env(read_text(paths.env))
    except OSError:
        return {}


def save_user_env(paths: Paths, updates: dict[str, str]) -> None:
    """Set keys in the per-user .env, keeping its other lines and comments. The file as it was
    goes to `.env.bak` first: a mistyped key must never cost the access that was working."""
    try:
        text = read_text(paths.env)
    except FileNotFoundError:
        text = ""
    else:
        write_atomic(backup_env_path(paths), text)   # the bytes we are about to edit
    seen: set[str] = set()
    out = []
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in line else ""
        if key in updates and not line.lstrip().startswith("#"):
            out.append(f"{key}={updates[key]}")   # every occurrence: a duplicate would win with the old value
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    write_atomic(paths.env, "\n".join(out) + "\n")   # a torn .env would lose the API keys


# --- runtime state ------------------------------------------------------------

def load_state(paths: Paths) -> dict:
    try:
        return json.loads(paths.state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_state_lock = threading.Lock()


def save_state(paths: Paths, state: dict) -> None:
    """Atomic (temp file + replace) and serialized: the tray and the settings window both
    write it, and a torn file would silently turn dictation back on after a restart."""
    try:
        with _state_lock:
            tmp = paths.state.with_suffix(".tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, paths.state)
    except OSError as e:
        log.warning("Não foi possível salvar o estado: %s", e)


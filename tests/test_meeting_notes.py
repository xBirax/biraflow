"""Meeting notes (Fase 4 of docs/PLANO-REUNIOES.md): Cleaner.meeting_notes and meeting.write_notes.
A fake LLM (Cleaner._request), no network. Ids T4.xx follow the plan."""
import json

import pytest

from biraflow import cleanup
from biraflow import meeting as mt
from biraflow.cancel import CancelToken
from biraflow.cleanup import Cleaner, CleanupError, RateLimited
from biraflow.config import CleanupCfg, Paths, Settings

GOOD = ("# Orçamento\n## Resumo\nFalamos do orçamento.\n## Decisões\n- Aprovado [00:01:02]\n"
        "## Próximos passos\n- [ ] Ana — enviar planilha — sexta\n## Pontos em aberto\nNenhum.")
ENV = {"GROQ_API_KEY": "k"}


@pytest.fixture
def llm(monkeypatch):
    """Replies are popped in order (a string, or an exception to raise); calls are recorded."""
    calls, replies = [], []

    def request(self, system, user, timeout_s, max_tokens=2048):
        calls.append({"system": system, "user": user, "timeout": timeout_s, "max_tokens": max_tokens})
        reply = replies.pop(0) if replies else GOOD
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(Cleaner, "_request", request)
    return calls, replies


@pytest.fixture
def slept(monkeypatch):
    out = []
    monkeypatch.setattr(cleanup.time, "sleep", out.append)
    return out


def notes(text, cfg=CleanupCfg()):
    return Cleaner(cfg, ENV).meeting_notes(text, CancelToken())


def transcript(chars):
    line = "**[00:00:05] Eu:** " + "falamos do orçamento e do prazo. " * 3
    return "\n\n".join([line] * (chars // len(line) + 1))[:chars]


def test_meeting_notes_single_call_when_short(llm):                       # T4.01
    calls, _ = llm
    assert notes(transcript(3000)) == GOOD
    assert len(calls) == 1 and calls[0]["system"] == cleanup.MEETING_PROMPT
    assert calls[0]["max_tokens"] == 2048 and calls[0]["timeout"] == 60
    assert notes("**[00:00:01] Eu:** oi", CleanupCfg(timeout_seconds=90)) and calls[1]["timeout"] == 90


def test_meeting_notes_map_reduce(llm):                                   # T4.02
    calls, replies = llm
    replies += [GOOD.replace("Orçamento", f"Parte {i}") for i in range(1, 5)]
    assert notes(transcript(40000)) == GOOD
    assert len(calls) == 5
    assert all(c["system"] == cleanup.MEETING_PROMPT and len(c["user"]) <= 12100 for c in calls[:4])
    merge = calls[4]
    assert merge["system"] == cleanup.MEETING_MERGE_PROMPT
    assert all(f"Parte {i}" in merge["user"] for i in range(1, 5))


def test_meeting_notes_waits_retry_after(llm, slept):                     # T4.03
    calls, replies = llm
    cleanup._limit("m", 45)
    replies.append(RateLimited("429"))
    assert notes("**[00:00:01] Eu:** oi") == GOOD
    assert len(calls) == 2 and len(slept) == 1 and 40 < slept[0] <= 45

    cleanup._limit("m", 300)                                    # Retry-After is waited 60 s at most
    replies += [RateLimited("429"), RateLimited("429")]         # ...and only once
    with pytest.raises(CleanupError, match="Gerar notas"):         # not the dictation's wording
        notes("**[00:00:01] Eu:** oi")
    assert slept[1] == 60 and len(calls) == 4


def test_meeting_notes_structure_retry(llm):                              # T4.04 [R26]
    calls, replies = llm
    replies += ["Resumo: falamos do orçamento.", GOOD]
    assert notes("**[00:00:01] Eu:** oi") == GOOD
    assert calls[1]["system"].endswith(cleanup.MEETING_STRUCTURE)

    replies += ["só um parágrafo", "outro parágrafo"]                 # still wrong: kept, with a warning
    assert notes("**[00:00:01] Eu:** oi") == cleanup.NOTES_WARNING + "outro parágrafo"
    assert len(calls) == 4


def test_meeting_notes_needs_provider():                                  # T4.05
    with pytest.raises(CleanupError, match="provedor de LLM"):
        Cleaner(CleanupCfg(), {}).meeting_notes("**[00:00:01] Eu:** oi", CancelToken())


def test_meeting_notes_truncated_is_a_clear_error(llm):
    _, replies = llm
    replies.append(CleanupError("resposta truncada (limite de tokens)"))
    with pytest.raises(CleanupError, match="longas demais.*transcrição continua salva"):
        notes("**[00:00:01] Eu:** oi")


def test_anthropic_rate_limit_is_rate_limited(monkeypatch):
    from biraflow import http

    class R:
        status_code, headers, text = 429, {"retry-after": "7"}, ""

    monkeypatch.setattr(http, "post", lambda url, **kw: R())
    c = Cleaner(CleanupCfg(provider="anthropic"), {"ANTHROPIC_API_KEY": "a"})
    with pytest.raises(RateLimited):
        c._request("s", "u", 10)
    assert 6 < cleanup._cooldown_left() <= 7


# --- the meeting folder ------------------------------------------------------------------

@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path, tmp_path)
    p.ensure()
    return p


def said(paths):
    m = mt.MeetingFolder.create(mt.meetings_dir(paths))
    m.append([{"t0": 5.0, "t1": 9.0, "who": "Eu", "text": "Vamos aprovar o orçamento."}])
    return m


def test_write_notes_saves_notas_md_and_prompt_version(paths, llm):
    calls, _ = llm
    m = said(paths)
    path = mt.write_notes(m, Settings(), ENV)
    assert path == m.folder / "notas.md" and path.read_text(encoding="utf-8") == GOOD + "\n"
    assert "aprovar o orçamento" in calls[0]["user"]
    state = json.loads((m.folder / "reuniao.json").read_text(encoding="utf-8"))
    assert state["prompt_version"] == cleanup.MEETING_PROMPT_VERSION and state["notes_error"] is None


def test_write_notes_failure_keeps_transcript(paths, llm):
    _, replies = llm
    m = said(paths)
    before = m.transcript.read_text(encoding="utf-8")
    assert mt.write_notes(m, Settings(), {}) is None                  # no LLM: an error, not an exception
    assert "provedor de LLM" in m.state["notes_error"]
    replies.append(RuntimeError("rede caiu"))
    assert mt.write_notes(m, Settings(), ENV) is None
    assert "RuntimeError" in m.state["notes_error"]
    assert not (m.folder / "notas.md").exists() and m.transcript.read_text(encoding="utf-8") == before


def test_write_notes_skips_a_silent_meeting(paths, llm):
    calls, _ = llm
    m = mt.MeetingFolder.create(mt.meetings_dir(paths))
    assert mt.write_notes(m, Settings(), ENV) is None and calls == []


def test_recover_writes_notes_once(paths, llm):
    calls, _ = llm
    m = said(paths)
    assert mt.recover(paths, Settings(), ENV) and (m.folder / "notas.md").exists()
    m.set(status="interrompida")
    mt.recover(paths, Settings(), ENV)
    assert len(calls) == 1                                            # notas.md already there
    m.set(status="interrompida")
    (m.folder / "notas.md").unlink()
    mt.recover(paths, Settings(), ENV, auto_notes=False)
    assert len(calls) == 1

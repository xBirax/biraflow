import pytest

from biraflow import cleanup
from biraflow.cancel import Cancelled, CancelToken
from biraflow.cleanup import Cleaner, resolve_provider, sanitize
from biraflow.config import CleanupCfg


def test_sanitize_strips_whisper_artifacts():
    assert sanitize(" [BLANK_AUDIO] ") == ""
    assert sanitize("Olá [Música] mundo (risos)\n tudo bem") == "Olá mundo tudo bem"
    assert sanitize("Obrigado por assistir!") == ""
    assert sanitize("Obrigado.") == "Obrigado."          # a real short reply survives
    assert sanitize("(isso fica) aqui") == "(isso fica) aqui"


def test_resolve_provider():
    auto = CleanupCfg()
    assert resolve_provider(auto, {}) == "none"
    assert resolve_provider(auto, {"OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "y"}) == "openai"
    assert resolve_provider(auto, {"GROQ_API_KEY": "x", "OPENAI_API_KEY": "y"}) == "groq"
    assert resolve_provider(CleanupCfg(provider="anthropic"), {}) == "none"
    assert resolve_provider(CleanupCfg(provider="ollama"), {}) == "ollama"
    assert resolve_provider(CleanupCfg(provider="none"), {"GROQ_API_KEY": "x"}) == "none"


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def fake_post(monkeypatch):
    calls = []
    replies = []

    def post(url, **kw):
        calls.append((url, kw))
        return replies.pop(0)

    from biraflow import http
    monkeypatch.setattr(http, "post", post)
    return calls, replies


def chat(text):
    return FakeResponse(200, {"choices": [{"message": {"content": text}}]})


def test_openai_compatible_request(fake_post):
    calls, replies = fake_post
    replies.append(chat("<dictation>Então, vamos começar.</dictation>"))
    c = Cleaner(CleanupCfg(), {"GROQ_API_KEY": "gsk"}, ["Bira Flow"])
    out = c.clean("é então tipo vamos começar", CancelToken())
    assert out == "Então, vamos começar."
    url, kw = calls[0]
    assert "groq.com" in url
    assert kw["headers"]["Authorization"] == "Bearer gsk"
    assert kw["json"]["model"] == "qwen/qwen3.8-27b"
    assert kw["json"]["reasoning_effort"] == "none"      # no thinking for a quick rewrite
    assert "Bira Flow" in kw["json"]["messages"][0]["content"]
    assert "<dictation>" in kw["json"]["messages"][1]["content"]


def test_temperature_rejected_is_retried_without(fake_post):
    calls, replies = fake_post
    replies += [FakeResponse(400, {"error": "Unsupported value: 'temperature'"}), chat("Oi.")]
    out = Cleaner(CleanupCfg(provider="openai"), {"OPENAI_API_KEY": "k"}).clean("oi", CancelToken())
    assert out == "Oi."
    assert "temperature" not in calls[1][1]["json"]


def test_anthropic_request(fake_post):
    calls, replies = fake_post
    replies.append(FakeResponse(200, {"content": [{"type": "text", "text": "Hello there."}]}))
    out = Cleaner(CleanupCfg(provider="anthropic"), {"ANTHROPIC_API_KEY": "a"}).clean("uh hello there", CancelToken())
    assert out == "Hello there."
    url, kw = calls[0]
    assert kw["headers"]["x-api-key"] == "a"
    assert kw["json"]["model"] == cleanup.PROVIDERS["anthropic"]["model"]


def test_vercel_gateway_request(fake_post):
    assert resolve_provider(CleanupCfg(), {"AI_GATEWAY_API_KEY": "v"}) == "vercel"
    calls, replies = fake_post
    replies.append(chat("Oi."))
    assert Cleaner(CleanupCfg(), {"AI_GATEWAY_API_KEY": "v"}).clean("oi", CancelToken()) == "Oi."
    url, kw = calls[0]
    assert url == "https://ai-gateway.vercel.sh/v1/chat/completions"
    assert kw["headers"]["Authorization"] == "Bearer v"
    body = kw["json"]
    assert body["model"] == "google/gemini-2.5-flash-lite"
    replies.append(chat("Oi."))
    Cleaner(CleanupCfg(provider="vercel", model="openai/gpt-oss-120b"), {"AI_GATEWAY_API_KEY": "v"}).clean(
        "oi", CancelToken())
    body = calls[1][1]["json"]
    assert body["reasoning"] == {"effort": "low"} and "reasoning_effort" not in body   # the gateway's field


def test_vercel_model_off_the_allowlist_says_so(fake_post):
    _, replies = fake_post
    replies.append(FakeResponse(403, {"error": {"message": "Your team has restricted access to this model."}}))
    c = Cleaner(CleanupCfg(provider="vercel"), {"AI_GATEWAY_API_KEY": "v"})
    with pytest.raises(cleanup.CleanupError, match="Allowlist"):
        c._request("s", "u", 5)


def test_failures_and_answers_fall_back_to_raw(fake_post):
    calls, replies = fake_post
    c = Cleaner(CleanupCfg(), {"GROQ_API_KEY": "k"})
    replies.append(FakeResponse(500, {}))
    assert c.clean("texto bruto", CancelToken()) == "texto bruto"
    replies.append(chat("Claro! Aqui está uma resposta longa que o modelo inventou em vez de limpar o ditado. " * 3))
    assert c.clean("qual a capital da França", CancelToken()) == "qual a capital da França"
    replies.append(chat(""))
    assert c.clean("algo", CancelToken()) == "algo"
    # a refusal (or a summary) in place of a long dictation is not a cleanup either
    long_raw = "então hã a gente precisa revisar o contrato com o fornecedor antes de sexta-feira " * 6
    replies.append(chat("Desculpe, não posso ajudar com isso."))
    assert c.clean(long_raw, CancelToken()) == long_raw
    # heavy filler removal and a self-correction still pass
    replies.append(chat("Vou na quarta."))
    assert c.clean("vou na segunda, não, na terça, não, hã, na quarta", CancelToken()) == "Vou na quarta."


def test_cancel_during_cleanup_raises(fake_post):
    _, replies = fake_post
    replies.append(chat("Oi."))
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        Cleaner(CleanupCfg(), {"GROQ_API_KEY": "k"}).clean("oi", token)


def test_no_provider_is_passthrough():
    assert Cleaner(CleanupCfg(), {}).clean("sem chave", CancelToken()) == "sem chave"


def test_rate_limited_model_falls_back_and_cools_down(fake_post, monkeypatch):
    monkeypatch.setattr(cleanup, "_cooldown", {})
    calls, replies = fake_post
    replies += [FakeResponse(429, {"error": "rate limit"}), chat("Manda o arquivo.")]
    c = Cleaner(CleanupCfg(), {"GROQ_API_KEY": "gsk"})
    assert c.clean("manda o arquivo", CancelToken()) == "Manda o arquivo."
    assert [kw["json"]["model"] for _, kw in calls] == ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
    # while the default model cools down, the next dictation goes straight to the fallback
    replies.append(chat("Oi."))
    assert Cleaner(CleanupCfg(), {"GROQ_API_KEY": "gsk"}).clean("oi", CancelToken()) == "Oi."
    assert calls[-1][1]["json"]["model"] == "openai/gpt-oss-20b"


def test_all_models_rate_limited_returns_raw_with_message(fake_post, monkeypatch):
    monkeypatch.setattr(cleanup, "_cooldown", {})
    calls, replies = fake_post
    replies += [FakeResponse(429, {}), FakeResponse(429, {}), FakeResponse(429, {})]
    c = Cleaner(CleanupCfg(), {"GROQ_API_KEY": "gsk"})
    assert c.clean("manda o arquivo", CancelToken()) == "manda o arquivo"
    assert "limite de uso" in c.last_error
    assert len(calls) == 3


def test_chosen_model_has_no_fallback(fake_post, monkeypatch):
    monkeypatch.setattr(cleanup, "_cooldown", {})
    calls, replies = fake_post
    replies.append(FakeResponse(429, {}))
    c = Cleaner(CleanupCfg(model="meu-modelo"), {"GROQ_API_KEY": "gsk"})
    assert c.clean("oi", CancelToken()) == "oi"
    assert len(calls) == 1



def test_sanitize_drops_nonspeech_anywhere_and_noise():
    assert sanitize("(dramatic music)") == ""
    assert sanitize("Oi (música de fundo) tudo bem") == "Oi tudo bem"
    assert sanitize("[Música] Valeu") == "Valeu"
    assert sanitize("M") == "" and sanitize(" - ") == ""
    assert sanitize("Ok") == "Ok"                        # two letters is a real reply
    assert sanitize("(Risos) Boa!") == "Boa!"
    assert sanitize("Sous-titres réalisés par la communauté d'Amara.org") == ""


def test_check_key(monkeypatch):
    from biraflow import http

    class R:
        def __init__(self, code):
            self.status_code = code

    codes = [200, 401, 429]

    class S:
        def get(self, url, headers, timeout):
            assert "Authorization" in headers or "x-api-key" in headers
            return R(codes.pop(0))

    monkeypatch.setattr(http, "session", lambda: S())
    assert cleanup.check_key("GROQ_API_KEY", "gsk")[0] is True
    ok, msg = cleanup.check_key("OPENAI_API_KEY", "sk")
    assert not ok and "recusada" in msg
    assert cleanup.check_key("ANTHROPIC_API_KEY", "a")[0] is True
    assert cleanup.check_key("GROQ_API_KEY", "  ") == (False, "vazia")


def test_every_style_has_a_prompt_and_a_label():
    from biraflow.config import STYLE_CHOICES
    from biraflow.settings_ui import STYLE_LABELS

    assert set(STYLE_CHOICES) == set(cleanup.STYLE_PROMPTS) | {"neutral"} == set(STYLE_LABELS)
    prompt = Cleaner(CleanupCfg(provider="none"), {}).system_prompt("email")
    assert "alone on the first line" in prompt and "Never add a greeting" in prompt


def test_a_stalled_cleanup_is_asked_again(monkeypatch):
    import requests

    c = Cleaner(CleanupCfg(provider="groq", timeout_seconds=8), {"GROQ_API_KEY": "x"})
    clock, calls = [0.0], []
    monkeypatch.setattr(cleanup.time, "monotonic", lambda: clock[0])

    def request(system, user, timeout_s, max_tokens=2048):
        calls.append(timeout_s)
        if len(calls) == 1:
            clock[0] += timeout_s            # the first request hangs until its read timeout
            raise requests.ReadTimeout("read timed out")
        return "Olá, tudo bem?"

    monkeypatch.setattr(c, "_request", request)
    assert c.clean("olá tudo bem", CancelToken()) == "Olá, tudo bem?"
    assert calls[0] < 3 and 5 < calls[1] <= 8          # a short first try, the rest of the budget for the second
    calls.clear()
    c.cfg.timeout_seconds = 3                            # under 1.5 s left after the first: the raw text stays
    assert c.clean("olá tudo bem", CancelToken()) == "olá tudo bem" and len(calls) == 1


def test_prompt_keeps_questions_and_word_order():
    prompt = Cleaner(CleanupCfg(provider="none"), {}).system_prompt()
    # measured 21/10/2026: without it gemini-2.5-flash-lite turned "está sem promoção ativa esse
    # anuncio" (a question) into "Este anúncio não tem promoção ativa." and dropped "Qual ...?"
    assert "A question stays a question" in prompt and "never \"Este anúncio não tem promoção\"" in prompt


def test_llm_http_error_is_clear_and_never_shows_the_key(fake_post):
    _, replies = fake_post
    replies.append(FakeResponse(401, {"error": {"message": "Incorrect API key provided: sk-proj-abc1****wxyz."}}))
    c = Cleaner(CleanupCfg(provider="vercel", model="google/gemini-2.5-flash-lite"), {"AI_GATEWAY_API_KEY": "k"})
    assert c.clean("um texto qualquer para limpar", CancelToken()) == "um texto qualquer para limpar"
    assert c.last_error.startswith("HTTP 401 Incorrect API key") and "sk-" not in c.last_error

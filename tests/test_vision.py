from PIL import Image

from biraflow import vision
from biraflow.config import CleanupCfg


class Reply:
    def __init__(self, status, content="", headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._content = content
        self.text = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_parse_keeps_only_sane_pairs():
    reply = '```json\n{"corrections": [{"heard": "TASP", "write": "Taspio"}, {"heard": "x", "write": "x"},' \
            ' {"heard": "tasp", "write": "taspio"}, {"heard": "a b c d e f", "write": "g"}, "junk"]}\n```'
    assert vision._parse(reply) == [("TASP", "Taspio")]
    assert vision._parse("não há nada") == []
    assert vision._parse('{"corrections": "nope"}') == []


def test_corrections_from_image_sends_screenshot_and_dictations(monkeypatch):
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        return Reply(200, '{"corrections": [{"heard": "Kate", "write": "Kayth"}]}')

    monkeypatch.setattr(vision.http, "post", post)
    big = Image.new("RGB", (3200, 1800), "white")
    found = vision.corrections_from_image(big, ["Oi Kate, bom dia!"], {"GROQ_API_KEY": "gsk"}, CleanupCfg())
    assert [(f.heard, f.write, f.seen) for f in found] == [("Kate", "Kayth", True)]
    url, kw = calls[0]
    assert "groq.com" in url
    body = kw["json"]
    assert body["model"] == vision.VISION_MODELS["groq"]
    parts = body["messages"][0]["content"]
    assert "Oi Kate, bom dia!" in parts[0]["text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_rate_limit_waits_once_then_retries(monkeypatch):
    replies = [Reply(429, headers={"retry-after": "0"}), Reply(200, '{"corrections": []}')]
    monkeypatch.setattr(vision.http, "post", lambda url, **kw: replies.pop(0))
    assert vision.corrections_from_image(Image.new("RGB", (10, 10)), [], {"GROQ_API_KEY": "k"}, CleanupCfg()) == []
    assert not replies


def test_needs_a_cloud_provider():
    import pytest

    from biraflow.cleanup import CleanupError

    with pytest.raises(CleanupError):
        vision.corrections_from_image(Image.new("RGB", (10, 10)), [], {}, CleanupCfg())

import asyncio
import importlib
import json

import httpx
from fastapi.testclient import TestClient

from app_engine.speech import SpeechService, SpeechSettings, SpeechUnavailable


def load_engine(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("APP_ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENGINE_APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.delenv("APP_ENGINE_TTS_URL", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import engine
    return importlib.reload(engine)


def fake_voice_service(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/voices":
            return httpx.Response(200, json={"voices": ["ryan", "serena"]})
        seen.append(request.read())
        return httpx.Response(200, content=b"RIFF-fake-wav", headers={"content-type": "audio/wav"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_settings_come_from_the_environment():
    assert SpeechSettings.from_env({}) is None, "no service configured means the browser speaks"
    settings = SpeechSettings.from_env({"APP_ENGINE_TTS_URL": "http://192.168.1.173:8895/", "APP_ENGINE_TTS_VOICE": "Ryan"})
    assert settings == SpeechSettings(url="http://192.168.1.173:8895", voice="ryan", language="English")


def test_synthesize_sends_one_bounded_chunk_with_the_configured_voice():
    seen = []
    service = SpeechService(SpeechSettings("http://tts", "ryan", "English"), fake_voice_service(seen))
    audio = asyncio.run(service.synthesize("Hello   there.\n" + "x" * 900))
    assert audio == b"RIFF-fake-wav"
    body = json.loads(seen[0])
    assert body["voice"] == "ryan" and body["language"] == "English"
    assert body["text"].startswith("Hello there. ") and len(body["text"]) == 500


def test_synthesize_reports_a_failing_service():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    service = SpeechService(SpeechSettings("http://tts", "", "English"), client)
    try:
        asyncio.run(service.synthesize("Hello."))
    except SpeechUnavailable:
        pass
    else:
        raise AssertionError("a 500 from the voice service must surface")


def test_without_a_service_the_launcher_uses_the_browser_voice(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        assert client.get("/api/speech/status").json() == {"engine": "browser", "voices": []}
        assert client.post("/api/speech", json={"text": "Hello."}).status_code == 404


def test_with_a_service_replies_are_rendered_as_wav(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_TTS_URL="http://tts", APP_ENGINE_TTS_VOICE="serena")
    seen = []
    module._speech.client = fake_voice_service(seen)
    with TestClient(module.app) as client:
        status = client.get("/api/speech/status").json()
        assert status == {"engine": "qwen", "voice": "serena", "voices": ["ryan", "serena"]}
        spoken = client.post("/api/speech", json={"text": "Photosynthesis makes sugar."})
        assert spoken.status_code == 200 and spoken.headers["content-type"] == "audio/wav"
        assert spoken.content == b"RIFF-fake-wav"
        assert client.post("/api/speech", json={"text": "  "}).status_code == 400
        assert client.post("/api/speech", json=["not", "an", "object"]).status_code == 400


def test_a_foreign_page_cannot_drive_the_voice_service(tmp_path, monkeypatch):
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_TTS_URL="http://tts")
    seen = []
    module._speech.client = fake_voice_service(seen)
    body = '{"text": "Say something embarrassing."}'
    with TestClient(module.app) as client:
        assert client.post("/api/speech", content=body, headers={"content-type": "text/plain"}).status_code == 415
        cross = client.post("/api/speech", content=body, headers={"content-type": "application/json", "sec-fetch-site": "cross-site"})
        assert cross.status_code == 403
    assert seen == [], "a refused request never reaches the voice service"

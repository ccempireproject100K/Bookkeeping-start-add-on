"""Voice transcription cascade (POST /api/voice/transcribe): the
fallback path for browsers with no built-in speech recognition.
Rung order is Cloudflare free pool → Groq free tier → the bank gate.
Providers are mocked throughout: these tests never touch a network,
and the bank rung must prove it debits nothing in this build.

Python port of the Compliance Finder reference tests
(test/voice-transcribe.test.js) — same behaviors, same numbers.

Run from the backend directory with:
    python -m pytest tests/test_voice_transcribe.py
"""
import base64
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.voice_transcribe as voice_router
from lib.voice_transcribe import (BANK_RATES_CENTS_PER_MINUTE, CF_MODEL_FALLBACK,
                                  CF_MODEL_PRIMARY, GROQ_MODEL, VoiceTranscriber)

AUDIO = b"fake-audio-bytes"
CF_ENV = {"CF_ACCOUNT_ID": "acct-1", "CF_API_TOKEN": "cf-token"}
FULL_ENV = {**CF_ENV, "GROQ_API_KEY": "groq-key"}


# behavior: {turbo: 'ok'|'fail'|'empty', base: ..., groq: ...}
def fake_http(behavior, calls=None):
    calls = calls if calls is not None else []

    def http_post(url, *, headers, json_body=None, form_fields=None,
                  file_part=None, timeout=20):
        calls.append({"url": str(url), "headers": headers or {},
                      "json_body": json_body, "form_fields": form_fields,
                      "file_part": file_part})
        is_groq = "api.groq.com" in str(url)
        kind = "groq" if is_groq else (
            "turbo" if "whisper-large-v3-turbo" in str(url) else "base")
        mode = behavior.get(kind, "ok")
        if mode == "fail":
            status = behavior.get("groq_status", 500) if is_groq else 500
            return status, {"success": False, "errors": [{"message": "boom"}]}
        if is_groq:
            return 200, {"text": "" if mode == "empty" else " groq words "}
        return 200, {"success": True,
                     "result": {"text": "" if mode == "empty" else " cloudflare words "}}

    return http_post


# ---------------- cascade (unit) ----------------

def test_turbo_answers_first_and_debits_neurons_at_the_turbo_rate():
    calls = []
    t = VoiceTranscriber(env=FULL_ENV, http_post=fake_http({"turbo": "ok"}, calls))
    result = t.transcribe(AUDIO, content_type="audio/webm", lang="en-US", duration_ms=6000)
    assert result["ok"] is True
    assert result["text"] == "cloudflare words"
    assert result["provider"] == "cloudflare"
    assert result["rung"] == 2
    assert result["model"] == CF_MODEL_PRIMARY
    assert len(calls) == 1
    assert "accounts/acct-1/ai/run/@cf/openai/whisper-large-v3-turbo" in calls[0]["url"]
    assert calls[0]["headers"]["Authorization"] == "Bearer cf-token"
    # Turbo receives the clip as a base64 JSON string plus the language
    # hint (the live-proven shape; multipart is rejected by the model).
    assert calls[0]["json_body"]["audio"] == base64.b64encode(AUDIO).decode("ascii")
    assert calls[0]["json_body"]["language"] == "en"
    assert abs(t.get_status()["cf_neurons_used"] - 0.1 * 46.63) < 0.01


def test_turbo_failure_falls_back_to_base_model_on_the_same_rung():
    calls = []
    t = VoiceTranscriber(env=FULL_ENV,
                         http_post=fake_http({"turbo": "fail", "base": "ok"}, calls))
    result = t.transcribe(AUDIO, content_type="audio/webm", lang="en", duration_ms=6000)
    assert result["ok"] is True
    assert result["model"] == CF_MODEL_FALLBACK
    assert len(calls) == 2
    # The base model receives the JSON byte-array shape.
    assert calls[1]["json_body"]["audio"] == list(AUDIO)
    assert abs(t.get_status()["cf_neurons_used"] - 0.1 * 41.14) < 0.01


def test_cloudflare_failure_fails_over_to_groq():
    calls = []
    t = VoiceTranscriber(env=FULL_ENV,
                         http_post=fake_http({"turbo": "fail", "base": "fail",
                                              "groq": "ok"}, calls))
    result = t.transcribe(AUDIO, content_type="audio/ogg", lang="es-ES", duration_ms=3000)
    assert result["ok"] is True
    assert result["provider"] == "groq"
    assert result["rung"] == 3
    assert result["model"] == GROQ_MODEL
    groq_call = next(c for c in calls if "api.groq.com" in c["url"])
    assert groq_call["headers"]["Authorization"] == "Bearer groq-key"
    assert groq_call["form_fields"]["language"] == "es"
    assert groq_call["file_part"][0] == "audio.ogg"
    assert t.get_status()["groq_seconds_used"] == 3


def test_nothing_configured_answers_voice_not_configured_and_calls_nobody():
    calls = []
    t = VoiceTranscriber(env={}, http_post=fake_http({}, calls))
    result = t.transcribe(AUDIO, content_type="audio/webm", duration_ms=1000)
    assert result["ok"] is False
    assert result["error"] == "voice_not_configured"
    assert calls == []
    assert t.configured()["any_free_rung"] is False


def test_free_rung_exhaustion_with_bank_gate_off_is_blocked_at_the_bank():
    t = VoiceTranscriber(env=CF_ENV,
                         http_post=fake_http({"turbo": "fail", "base": "fail"}))
    result = t.transcribe(AUDIO, content_type="audio/webm", duration_ms=6000)
    assert result["ok"] is False
    assert result["error"] == "voice_unavailable"
    assert result["bank"]["enabled"] is False  # the gate is off by default
    assert result["bank"]["balance_cents"] == 0
    assert len(result["attempts"]) >= 2  # the failed free attempts are reported


def test_neuron_guard_switches_to_groq_before_the_cloudflare_wall():
    calls = []
    env = {**FULL_ENV, "CF_VOICE_DAILY_NEURON_BUDGET": "100",
           "CF_VOICE_GUARD_FRACTION": "0.9"}
    t = VoiceTranscriber(env=env,
                         http_post=fake_http({"turbo": "ok", "base": "ok",
                                              "groq": "ok"}, calls))
    # 30 s clips = 0.5 min: turbo costs 23.315 neurons a call. Three
    # calls fit under the 90-neuron guard (69.9 used); the fourth would
    # cross it for BOTH models, so it must go straight to Groq.
    for _ in range(3):
        r = t.transcribe(AUDIO, content_type="audio/webm", duration_ms=30000)
        assert r["provider"] == "cloudflare"
    fourth = t.transcribe(AUDIO, content_type="audio/webm", duration_ms=30000)
    assert fourth["ok"] is True
    assert fourth["provider"] == "groq"
    assert len([c for c in calls if "api.cloudflare.com" in c["url"]]) == 3


def test_groq_429_marks_the_free_tier_spent_for_the_day():
    t = VoiceTranscriber(env={**CF_ENV, "GROQ_API_KEY": "k"},
                         http_post=fake_http({"turbo": "fail", "base": "fail",
                                              "groq": "fail", "groq_status": 429}))
    first = t.transcribe(AUDIO, content_type="audio/webm", duration_ms=1000)
    assert first["error"] == "voice_unavailable"
    second = t.transcribe(AUDIO, content_type="audio/webm", duration_ms=1000)
    assert second["error"] == "voice_unavailable"
    groq_attempt = next(a for a in second["attempts"] if a["provider"] == "groq")
    assert groq_attempt["result"] == "budget_guard"  # skipped, not retried


def test_bank_gate_on_reports_not_wired_and_debits_nothing():
    env = {**CF_ENV, "VOICE_BANK_ENABLED": "true", "VOICE_BANK_BALANCE_CENTS": "250"}
    t = VoiceTranscriber(env=env,
                         http_post=fake_http({"turbo": "fail", "base": "fail"}))
    result = t.transcribe(AUDIO, content_type="audio/webm", duration_ms=30000)
    assert result["ok"] is False
    assert result["error"] == "voice_bank_not_wired"
    assert result["bank"]["enabled"] is True
    assert result["bank"]["balance_cents"] == 250  # untouched: nothing was bought
    assert abs(result["would_cost_cents"]
               - BANK_RATES_CENTS_PER_MINUTE["cloudflare"] * 0.5) < 0.002


def test_bank_drip_ledger_math_and_low_water_flag():
    env = {"VOICE_BANK_ENABLED": "true", "VOICE_BANK_BALANCE_CENTS": "250",
           "VOICE_BANK_LOW_WATER_CENTS": "50"}
    t = VoiceTranscriber(env=env, http_post=fake_http({}))
    assert t.quote_bank_debit("groq", 10) == BANK_RATES_CENTS_PER_MINUTE["groq"] * 10
    after = t.debit_bank("groq", 10)
    assert after["debited_cents"] == 1.85
    assert after["balance_cents"] == 248.15
    assert after["low_water"] is False
    drained = t.debit_bank("cloudflare", 100000)
    assert drained["balance_cents"] == 0  # the float can never go negative
    assert drained["low_water"] is True


def test_budget_counters_reset_at_the_utc_day_boundary():
    clock = [datetime(2026, 11, 1, 23, 59, tzinfo=timezone.utc).timestamp()]
    t = VoiceTranscriber(env=CF_ENV, http_post=fake_http({"turbo": "ok"}),
                         now=lambda: clock[0])
    t.transcribe(AUDIO, content_type="audio/webm", duration_ms=6000)
    assert t.get_status()["cf_neurons_used"] > 0
    clock[0] += 2 * 60  # 2026-11-02 00:01 UTC
    assert t.get_status()["cf_neurons_used"] == 0  # a new day refills the guard


# ---------------- HTTP surface ----------------

class _FakeTranscriber:
    def __init__(self, configured_dict, result):
        self._configured = configured_dict
        self._result = result
        self.seen = None

    def configured(self):
        return self._configured

    def transcribe(self, audio, content_type="audio/webm", lang=None, duration_ms=None):
        self.seen = {"audio": audio, "content_type": content_type,
                     "lang": lang, "duration_ms": duration_ms}
        return self._result


def _client(monkeypatch, fake):
    monkeypatch.setattr(voice_router, "_transcriber", fake)
    app = FastAPI()
    app.include_router(voice_router.router, prefix="/api")
    return TestClient(app)


def test_http_transcribe_passes_the_clip_through_and_answers_the_transcript(monkeypatch):
    fake = _FakeTranscriber(
        {"cloudflare": True, "groq": False, "any_free_rung": True, "bank_enabled": False},
        {"ok": True, "text": "plumbing", "provider": "cloudflare", "rung": 2})
    client = _client(monkeypatch, fake)
    response = client.post("/api/voice/transcribe?lang=en-US",
                           content=b"clip-bytes",
                           headers={"Content-Type": "audio/webm",
                                    "X-Audio-Duration-Ms": "4200"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "text": "plumbing",
                               "provider": "cloudflare", "rung": 2}
    assert fake.seen["content_type"] == "audio/webm"
    assert fake.seen["lang"] == "en-US"
    assert fake.seen["duration_ms"] == 4200
    assert fake.seen["audio"] == b"clip-bytes"
    assert response.headers["cache-control"] == "no-store"


def test_http_transcribe_rejects_bad_clips_before_any_provider_work(monkeypatch):
    fake = _FakeTranscriber(
        {"cloudflare": True, "groq": False, "any_free_rung": True, "bank_enabled": False},
        {"ok": True, "text": "x", "provider": "cloudflare", "rung": 2})
    client = _client(monkeypatch, fake)
    wrong_type = client.post("/api/voice/transcribe", content=b"{}",
                             headers={"Content-Type": "application/json"})
    assert wrong_type.status_code == 415
    empty = client.post("/api/voice/transcribe", content=b"",
                        headers={"Content-Type": "audio/webm"})
    assert empty.status_code == 400
    oversize = client.post("/api/voice/transcribe",
                           content=b"\x01" * (2 * 1024 * 1024 + 1),
                           headers={"Content-Type": "audio/webm"})
    assert oversize.status_code == 413
    assert fake.seen is None  # the transcriber was never invoked


def test_http_unconfigured_cascade_answers_503_voice_not_configured(monkeypatch):
    fake = _FakeTranscriber(
        {"cloudflare": False, "groq": False, "any_free_rung": False,
         "bank_enabled": False},
        {"ok": False, "error": "voice_not_configured",
         "bank": {"enabled": False, "balance_cents": 0, "low_water": True,
                  "low_water_cents": 50,
                  "rates_cents_per_minute": {"cloudflare": 0.045, "groq": 0.185}}})
    client = _client(monkeypatch, fake)
    response = client.post("/api/voice/transcribe", content=b"clip",
                           headers={"Content-Type": "audio/webm"})
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "voice_not_configured"


def test_http_config_reports_the_fallback_configuration_for_the_note_variants(monkeypatch):
    on = _FakeTranscriber(
        {"cloudflare": True, "groq": False, "any_free_rung": True, "bank_enabled": False},
        {"ok": False, "error": "voice_unavailable"})
    client = _client(monkeypatch, on)
    assert client.get("/api/voice/config").json()["voice"]["fallback"] is True

    off = _FakeTranscriber(
        {"cloudflare": False, "groq": False, "any_free_rung": False,
         "bank_enabled": False},
        {"ok": False, "error": "voice_not_configured"})
    client = _client(monkeypatch, off)
    body = client.get("/api/voice/config").json()
    assert body["ok"] is True
    assert body["voice"]["fallback"] is False

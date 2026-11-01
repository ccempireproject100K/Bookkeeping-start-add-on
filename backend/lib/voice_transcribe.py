"""Voice transcription cascade for POST /api/voice/transcribe — the
server half of the guided voice fill's fallback path (owner cascade
design, 2026-11-01; Python port of the Compliance Finder reference
implementation, src/voice/transcribe.js, with identical semantics).
The browser built-in recognizer stays the primary rung and never
reaches this module; this serves browsers with NO built-in recognizer
(Firefox), in this order:

  rung 2  Cloudflare Workers AI Whisper (free daily neuron pool)
  rung 3  Groq Whisper free tier (config-gated on GROQ_API_KEY)
  rung 4  THE BANK — a prepaid float paid minutes drip from. The
          accounting and the gate live here (VOICE_BANK_ENABLED,
          default off), but NO paid transport is wired in this build:
          with the gate off the rung is simply blocked, and with it
          on the rung reports that it is not wired yet. Either way
          nothing is debited and nothing is spent.

AUDIO IS NEVER WRITTEN TO DISK AND NEVER STORED. The clip bytes exist
only in memory for the duration of the provider call, and the
transcript is the only thing that leaves this module.

The daily Cloudflare guard is an ESTIMATE: the account's real neuron
balance is not readable from here, so successful calls debit a local
counter at the published per-minute neuron rates and the cascade
moves to Groq at a configurable fraction (default 90%) of the daily
budget — switching BEFORE the wall instead of on errors alone.
Counters are per-process and reset at the UTC day boundary; on a
multi-instance or restarting host they under-count, which only ever
means the real provider quota answers first and the cascade fails
over on the error exactly as designed.

The neuron pool is the Cloudflare ACCOUNT's, shared with every other
empire surface plugged into this cascade (Compliance Finder first) —
the env token is the same account credential, so the guard here and
the guard there both estimate against the same real pool.
"""
import base64
import math
import os
import re
import time
from datetime import datetime, timezone

import requests

CF_MODEL_PRIMARY = "@cf/openai/whisper-large-v3-turbo"
CF_MODEL_FALLBACK = "@cf/openai/whisper"
GROQ_MODEL = "whisper-large-v3"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Published Workers AI neuron rates (per audio minute) and the free
# daily allocation. Turbo is the primary model, so its rate governs
# the guard math; the base model is debited at its own rate.
CF_NEURONS_PER_MINUTE = {CF_MODEL_PRIMARY: 46.63, CF_MODEL_FALLBACK: 41.14}
CF_DAILY_NEURON_BUDGET_DEFAULT = 10000
CF_GUARD_FRACTION_DEFAULT = 0.9
# Groq's free tier allows 28,800 audio-seconds per day for Whisper.
GROQ_DAILY_SECONDS = 28800
# One voice answer is short; the endpoint caps clips at 30 seconds.
MAX_CLIP_MS = 30000
PROVIDER_TIMEOUT_SEC = 20

# Bank drip rates in USD cents per audio minute (operator config
# values, from published paid pricing: Cloudflare Workers Paid
# ≈ $0.011 per 1,000 neurons; Groq whisper-large-v3 ≈ $0.111/hour).
BANK_RATES_CENTS_PER_MINUTE = {"cloudflare": 0.045, "groq": 0.185}
BANK_LOW_WATER_CENTS_DEFAULT = 50

EXTENSIONS = {
    "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a",
    "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/flac": "flac",
}

_LANG_PRIMARY_RE = re.compile(r"^[a-z]{2,3}$")


def _round_half_up(value, places):
    """Math.round-style rounding (half away from zero for the positive
    quantities used here), so the port's figures match the reference
    implementation's exactly instead of Python's banker's rounding."""
    factor = 10 ** places
    return math.floor(value * factor + 0.5) / factor


def _primary_lang(lang):
    if not isinstance(lang, str):
        return None
    primary = lang.strip().lower().split("-")[0]
    return primary if _LANG_PRIMARY_RE.match(primary) else None


def _env_str(env, name):
    value = env.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _env_number(env, name):
    try:
        parsed = float(env.get(name))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _default_http_post(url, *, headers, json_body=None, form_fields=None,
                       file_part=None, timeout=PROVIDER_TIMEOUT_SEC):
    """Default provider transport (requests). Returns
    (status_code | None, parsed_json | None) and never raises: a
    transport failure is just a failed cascade attempt."""
    try:
        if file_part is not None:
            filename, data, content_type = file_part
            response = requests.post(
                url, headers=headers, data=form_fields or {},
                files={"file": (filename, data, content_type)}, timeout=timeout)
        else:
            response = requests.post(url, headers=headers, json=json_body,
                                     timeout=timeout)
    except Exception:
        return None, None
    try:
        payload = response.json()
    except Exception:
        payload = None
    return response.status_code, payload


class _ProviderError(Exception):
    def __init__(self, message, rate_limited=False):
        super().__init__(message)
        self.rate_limited = rate_limited


class VoiceTranscriber:
    """The cascade. Construct with an env mapping (defaults to
    os.environ), an injectable http_post transport (signature:
    http_post(url, *, headers, json_body=None, form_fields=None,
    file_part=None, timeout=...) -> (status | None, json | None)),
    and an injectable clock (callable returning epoch seconds)."""

    def __init__(self, env=None, http_post=None, now=None):
        env = os.environ if env is None else env
        self._http_post = http_post or _default_http_post
        self._now = now or time.time

        self._account_id = _env_str(env, "CF_ACCOUNT_ID")
        self._cf_token = _env_str(env, "CF_API_TOKEN")
        self._groq_key = _env_str(env, "GROQ_API_KEY")
        self._cloudflare_configured = bool(self._account_id and self._cf_token)
        self._groq_configured = bool(self._groq_key)

        budget = _env_number(env, "CF_VOICE_DAILY_NEURON_BUDGET")
        self._daily_neuron_budget = budget if budget and budget > 0 \
            else CF_DAILY_NEURON_BUDGET_DEFAULT
        guard = _env_number(env, "CF_VOICE_GUARD_FRACTION")
        self._guard_fraction = guard if guard and 0 < guard <= 1 \
            else CF_GUARD_FRACTION_DEFAULT

        self._bank_enabled = env.get("VOICE_BANK_ENABLED") in ("true", "1")
        balance = _env_number(env, "VOICE_BANK_BALANCE_CENTS")
        self._bank_balance_cents = balance if balance and balance > 0 else 0.0
        low_water = _env_number(env, "VOICE_BANK_LOW_WATER_CENTS")
        self._bank_low_water_cents = low_water if low_water is not None and low_water >= 0 \
            else BANK_LOW_WATER_CENTS_DEFAULT

        self._day = self._day_key(self._now())
        self._cf_neurons_used = 0.0
        self._groq_seconds_used = 0.0

    # ---- state helpers -------------------------------------------------
    @staticmethod
    def _day_key(epoch_seconds):
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%d")

    def _roll_day(self):
        today = self._day_key(self._now())
        if self._day != today:
            self._day = today
            self._cf_neurons_used = 0.0
            self._groq_seconds_used = 0.0

    def _bank_status(self):
        return {
            "enabled": self._bank_enabled,
            "balance_cents": _round_half_up(self._bank_balance_cents, 3),
            "low_water": self._bank_balance_cents <= self._bank_low_water_cents,
            "low_water_cents": self._bank_low_water_cents,
            "rates_cents_per_minute": dict(BANK_RATES_CENTS_PER_MINUTE),
        }

    # What a paid minute would drip from the bank. Pure accounting: this
    # build never executes the paid call, so nothing calls the debit
    # half of this pair in production flow yet — both halves are pinned
    # by tests so the drip math is proven before money is attached.
    def quote_bank_debit(self, provider, minutes):
        rate = BANK_RATES_CENTS_PER_MINUTE.get(provider, 0)
        return _round_half_up(rate * max(0.0, minutes), 3)

    def debit_bank(self, provider, minutes):
        cost = self.quote_bank_debit(provider, minutes)
        self._bank_balance_cents = max(0.0, self._bank_balance_cents - cost)
        return {"debited_cents": cost, **self._bank_status()}

    def configured(self):
        return {
            "cloudflare": self._cloudflare_configured,
            "groq": self._groq_configured,
            "any_free_rung": self._cloudflare_configured or self._groq_configured,
            "bank_enabled": self._bank_enabled,
        }

    def get_status(self):
        self._roll_day()
        return {
            **self.configured(),
            "cf_neurons_used": _round_half_up(self._cf_neurons_used, 2),
            "cf_neuron_budget": self._daily_neuron_budget,
            "cf_guard_neurons": self._daily_neuron_budget * self._guard_fraction,
            "groq_seconds_used": _round_half_up(self._groq_seconds_used, 0),
            "groq_seconds_budget": GROQ_DAILY_SECONDS,
            "bank": self._bank_status(),
        }

    # ---- provider calls ------------------------------------------------
    def _call_cloudflare(self, model, audio, lang):
        url = ("https://api.cloudflare.com/client/v4/accounts/" + self._account_id
               + "/ai/run/" + model)
        # Both request shapes were proven live against the real account
        # in the Compliance Finder prototype (2026-11-01, real speech
        # fixture): turbo takes the clip as a base64 JSON string
        # (multipart and byte-array bodies are rejected with 400), the
        # base model as a JSON byte array.
        if model == CF_MODEL_PRIMARY:
            payload = {"audio": base64.b64encode(audio).decode("ascii")}
            if lang:
                payload["language"] = lang
        else:
            payload = {"audio": list(audio)}
        status, data = self._http_post(
            url,
            headers={"Authorization": "Bearer " + self._cf_token,
                     "Content-Type": "application/json"},
            json_body=payload)
        if status is None or not (200 <= status < 300):
            raise _ProviderError("cloudflare_http_" + (str(status) if status else "none"))
        if (not isinstance(data, dict) or data.get("success") is False
                or not isinstance(data.get("result"), dict)
                or not isinstance(data["result"].get("text"), str)):
            raise _ProviderError("cloudflare_bad_response")
        return data["result"]["text"].strip()

    def _call_groq(self, audio, content_type, lang):
        form_fields = {"model": GROQ_MODEL, "response_format": "json"}
        if lang:
            form_fields["language"] = lang
        file_part = ("audio." + EXTENSIONS.get(content_type, "webm"),
                     audio, content_type or "audio/webm")
        status, data = self._http_post(
            GROQ_URL,
            headers={"Authorization": "Bearer " + self._groq_key},
            form_fields=form_fields, file_part=file_part)
        if status is None or not (200 <= status < 300):
            raise _ProviderError(
                "groq_http_" + (str(status) if status else "none"),
                rate_limited=(status == 429))
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise _ProviderError("groq_bad_response")
        return data["text"].strip()

    # ---- the cascade ---------------------------------------------------
    def transcribe(self, audio, content_type="audio/webm", lang=None, duration_ms=None):
        """Resolves to {"ok": True, "text", "provider", "rung", "model"}
        or {"ok": False, "error", ...}. Never raises for provider
        failures — every failure is a cascade step."""
        if not audio:
            return {"ok": False, "error": "voice_empty_audio"}
        self._roll_day()
        lang = _primary_lang(lang)
        if isinstance(duration_ms, (int, float)) and math.isfinite(duration_ms) \
                and duration_ms > 0:
            duration_ms = min(duration_ms, MAX_CLIP_MS)
        else:
            duration_ms = min(MAX_CLIP_MS, max(500.0, len(audio) / 16))
        minutes = duration_ms / 60000
        seconds = duration_ms / 1000
        attempts = []

        if not self._cloudflare_configured and not self._groq_configured:
            return {"ok": False, "error": "voice_not_configured",
                    "bank": self._bank_status()}

        # Rung 2 — Cloudflare free pool, behind the daily neuron guard.
        if self._cloudflare_configured:
            guard = self._daily_neuron_budget * self._guard_fraction
            for model in (CF_MODEL_PRIMARY, CF_MODEL_FALLBACK):
                neurons = minutes * CF_NEURONS_PER_MINUTE[model]
                if self._cf_neurons_used + neurons > guard:
                    attempts.append({"provider": "cloudflare", "model": model,
                                     "result": "budget_guard"})
                    continue
                try:
                    text = self._call_cloudflare(model, audio, lang)
                except _ProviderError as error:
                    attempts.append({"provider": "cloudflare", "model": model,
                                     "result": str(error)})
                    continue
                self._cf_neurons_used += neurons
                if text:
                    return {"ok": True, "text": text, "provider": "cloudflare",
                            "rung": 2, "model": model}
                attempts.append({"provider": "cloudflare", "model": model,
                                 "result": "empty_text"})

        # Rung 3 — Groq free tier, behind its daily audio-seconds budget.
        if self._groq_configured:
            if self._groq_seconds_used + seconds > GROQ_DAILY_SECONDS:
                attempts.append({"provider": "groq", "model": GROQ_MODEL,
                                 "result": "budget_guard"})
            else:
                try:
                    text = self._call_groq(audio, content_type, lang)
                except _ProviderError as error:
                    if error.rate_limited:
                        self._groq_seconds_used = GROQ_DAILY_SECONDS
                    attempts.append({"provider": "groq", "model": GROQ_MODEL,
                                     "result": str(error)})
                else:
                    self._groq_seconds_used += seconds
                    if text:
                        return {"ok": True, "text": text, "provider": "groq",
                                "rung": 3, "model": GROQ_MODEL}
                    attempts.append({"provider": "groq", "model": GROQ_MODEL,
                                     "result": "empty_text"})

        # Rung 4 — the bank. Gate first: off means the paid rung does
        # not exist for this deployment and the visitor degrades to
        # typing, exactly as before this build. On means a paid call
        # WOULD be placed here once a paid transport is wired; this
        # build wires none, debits nothing, and says so.
        if not self._bank_enabled:
            return {"ok": False, "error": "voice_unavailable",
                    "attempts": attempts, "bank": self._bank_status()}
        return {"ok": False, "error": "voice_bank_not_wired",
                "attempts": attempts, "bank": self._bank_status(),
                "would_cost_cents": self.quote_bank_debit("cloudflare", minutes)}

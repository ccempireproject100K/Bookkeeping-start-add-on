"""Voice transcription fallback for the public forms (guided voice fill).

POST /voice/transcribe — raw audio body, transcribed in flight by the
cascade in lib/voice_transcribe.py (Cloudflare free pool → Groq free
tier → the bank gate). Serves browsers with NO built-in speech
recognition (Firefox); browsers with one never call this. The clip is
never written to disk and never stored. Every failure answers a clean
error and the visitor simply keeps typing — the form never depends on
this endpoint.

GET /voice/config — the configuration surface the browser note beside
the voice controls is keyed to: {voice: {fallback: <a free rung is
configured>}}. Derived live from the transcriber's configuration and
never cached, so the note can claim Firefox support only when it is
actually true, and flips automatically the moment the env is wired.

This is a separate router from routers/voice.py on purpose: that one
is the authenticated "talk to your books" copilot (LLM-key-gated,
stores its clips); this one is public, pre-account (the signup form),
and stores nothing. The app's global RateLimitMiddleware covers both
routes, and CORS is the app's global middleware as usual.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from lib.voice_transcribe import VoiceTranscriber

router = APIRouter(prefix="/voice", tags=["voice-transcribe"])

ALLOWED_CONTENT_TYPES = {
    "audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg",
    "audio/wav", "audio/x-wav", "audio/flac",
}
MAX_BODY_BYTES = 2 * 1024 * 1024

# Constructed from env at import; with no provider configured it
# answers voice_not_configured and the public forms degrade to typing.
_transcriber = VoiceTranscriber()


def _respond(payload, status=200):
    return JSONResponse(payload, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _fail(status, error, message, extra=None):
    return _respond({"ok": False, "error": error, "message": message,
                     **(extra or {})}, status)


@router.get("/config")
def voice_config():
    return _respond({"ok": True,
                     "voice": {"fallback": _transcriber.configured()["any_free_rung"]}})


@router.post("/transcribe")
async def voice_transcribe(request: Request):
    content_type = (request.headers.get("content-type") or "") \
        .split(";")[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        return _fail(415, "invalid_request",
                     "An audio clip (webm, ogg, mp4, mpeg, wav or flac) is required.")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return _fail(413, "invalid_request", "The audio clip exceeds the size limit.")
    if not body:
        return _fail(400, "invalid_request", "The audio clip was empty.")
    try:
        duration_header = float(request.headers.get("x-audio-duration-ms"))
        if duration_header <= 0:
            duration_header = None
    except (TypeError, ValueError):
        duration_header = None
    result = await run_in_threadpool(
        _transcriber.transcribe, body,
        content_type=content_type,
        lang=request.query_params.get("lang"),
        duration_ms=duration_header)
    if not result["ok"]:
        extra = None
        if result.get("bank"):
            extra = {"bank": {"enabled": result["bank"]["enabled"],
                              "low_water": result["bank"]["low_water"]}}
        return _fail(
            503, result["error"],
            "Voice transcription is not configured on this service."
            if result["error"] == "voice_not_configured"
            else "Voice transcription is unavailable right now.",
            extra)
    return _respond({"ok": True, "text": result["text"],
                     "provider": result["provider"], "rung": result["rung"]})

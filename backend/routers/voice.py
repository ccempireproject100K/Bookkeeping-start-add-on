"""Voice-first books: talk to your books, get a voice-note answer.

POST /voice/ask — multipart: audio file (webm/mp3/wav/m4a, <= 10MB) and/or text.
  1. Transcribe audio via Whisper (needs AI_API_KEY).
  2. Answer with the copilot's grounded chat (workspace snapshot + LLM).
  3. Speak the answer back via TTS; audio served at /voice/audio/{file}.
Without an LLM key, transcription and Q&A are unavailable (503 with hint),
matching the copilot chat behavior.
"""
import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import requests
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from db import db
from lib.audit import audit
from lib.auth import get_current_user
from lib.pii import scrub_secrets
from lib.base import new_id, now_iso
from routers.copilot import _snapshot, _llm_chat, _llm_key, CHAT_SYSTEM

router = APIRouter(prefix="/voice", tags=["voice"])
logger = logging.getLogger(__name__)

AUDIO_ROOT = Path(__file__).parent / "uploads" / "voice"
ALLOWED = {"audio/webm", "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav",
           "audio/mp4", "audio/m4a", "audio/ogg"}
MAX_BYTES = 10 * 1024 * 1024


def _transcribe(path: Path, key: str) -> str:
    with open(path, "rb") as f:
        r = requests.post("https://api.openai.com/v1/audio/transcriptions",
                          headers={"Authorization": f"Bearer {key}"},
                          files={"file": (path.name, f, "audio/webm")},
                          data={"model": "whisper-1"}, timeout=90)
    if r.status_code != 200:
        logger.error("whisper error %s: %s", r.status_code, scrub_secrets(r.text[:300]))
        raise HTTPException(502, "Speech transcription failed — try again")
    return r.json().get("text", "").strip()


def _speak(text: str, key: str, out: Path):
    voice = os.environ.get("AI_VOICE", "alloy").strip()
    r = requests.post("https://api.openai.com/v1/audio/speech",
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": "tts-1", "voice": voice,
                            "input": text[:4000]},
                      timeout=90)
    if r.status_code != 200:
        logger.error("tts error %s: %s", r.status_code, scrub_secrets(r.text[:300]))
        raise RuntimeError("tts failed")
    out.write_bytes(r.content)


@router.post("/ask")
async def ask(audio: Optional[UploadFile] = File(None),
              text: Optional[str] = Form(None),
              user: dict = Depends(get_current_user)):
    key = _llm_key()
    if not key:
        raise HTTPException(503, "Voice books need AI_API_KEY (same key as the copilot chat)")
    transcript = (text or "").strip()
    if audio is not None:
        if audio.content_type not in ALLOWED:
            raise HTTPException(415, f"Unsupported audio type: {audio.content_type}")
        data = await audio.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "Audio too large (10MB max)")
        d = AUDIO_ROOT / user["tenant_id"]
        d.mkdir(parents=True, exist_ok=True)
        in_path = d / f"q-{new_id()}.webm"
        in_path.write_bytes(data)
        transcript = await asyncio.to_thread(_transcribe, in_path, key)
        try:
            in_path.unlink()
        except OSError:
            pass
    if not transcript:
        raise HTTPException(422, "No question heard — try again")
    import json as _json
    snap = await _snapshot(user)
    reply = await _llm_chat(
        CHAT_SYSTEM + _json.dumps(snap, default=str),
        f"Today is {snap['today']}. The user asked by voice: {transcript}. "
        "Answer conversationally and briefly, as if speaking — no lists longer than 3 items, no markdown.")
    audio_url = None
    try:
        out = AUDIO_ROOT / user["tenant_id"] / f"a-{new_id()}.mp3"
        await asyncio.to_thread(_speak, reply, key, out)
        audio_url = f"/voice/audio/{user['tenant_id']}/{out.name}"
    except Exception as exc:
        logger.error("voice TTS failed: %s", exc)
    await audit(user, "voice.ask", "tenant", user["tenant_id"],
                {"transcript": transcript[:200], "audio": bool(audio_url)})
    return {"transcript": transcript, "reply": reply, "audio_url": audio_url}


@router.get("/audio/{tenant_id}/{name}")
async def get_audio(tenant_id: str, name: str, user: dict = Depends(get_current_user)):
    if tenant_id != user["tenant_id"]:
        raise HTTPException(403, "Not your audio")
    p = AUDIO_ROOT / tenant_id / name
    if not p.is_file() or p.suffix != ".mp3":
        raise HTTPException(404, "Audio not found")
    return FileResponse(p, media_type="audio/mpeg")

"""Fade Rush — public waiting-room game. Leaderboard per shop per ISO week; high scores earn a single-use discount code."""

import random
import string
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from lib.dates import now_iso, today_iso
from lib.db import db

router = APIRouter(prefix="/game")
TARGET, PCT, TOP_PCT, MAX_SCORE = 600, 10, 20, 5000


class ScoreIn(BaseModel):
    name: str = Field(min_length=1, max_length=20)
    score: int = Field(ge=0, le=MAX_SCORE)
    duration_ms: int = Field(ge=25_000, le=40_000)  # a round is 30s; rejects scripted instant posts


class ScoreRow(BaseModel):
    name: str
    score: int


class GameInfo(BaseModel):
    shop: str
    week: str
    target: int
    reward_pct: int
    top_reward_pct: int
    leaderboard: list[ScoreRow]


class ScoreOut(BaseModel):
    rank: int
    best: bool
    code: str | None
    pct: int
    message: str


def _week(tz: str | None = None) -> str:
    y, w, _ = date.fromisoformat(today_iso(tz)).isocalendar()
    return f"{y}-W{w:02d}"


async def _tenant(tid: str) -> dict:
    t = await db.tenants.find_one({"id": tid}, {"_id": 0, "id": 1, "name": 1, "settings": 1})
    if not t or not (t.get("settings") or {}).get("inventory_enabled"):
        raise HTTPException(404, "Game not found")
    return t


async def _board(tid: str) -> list[ScoreRow]:
    t = await _tenant(tid)
    tz = (t.get("settings") or {}).get("timezone")
    rows = await db.game_scores.aggregate([{"$match": {"tenant_id": tid, "week": _week(tz)}}, {"$group": {"_id": "$name_key", "name": {"$first": "$name"}, "score": {"$max": "$score"}}},
                                           {"$sort": {"score": -1}}, {"$limit": 10}]).to_list(10)
    return [ScoreRow(name=r["name"], score=r["score"]) for r in rows]


@router.get("/{tenant_id}", response_model=GameInfo)
async def info(tenant_id: str):
    t = await _tenant(tenant_id)
    return GameInfo(shop=t["name"], week=_week((t.get("settings") or {}).get("timezone")), target=TARGET, reward_pct=PCT, top_reward_pct=TOP_PCT, leaderboard=await _board(tenant_id))


@router.post("/{tenant_id}/score", response_model=ScoreOut)
async def submit(tenant_id: str, body: ScoreIn):
    t = await _tenant(tenant_id)
    tz = (t.get("settings") or {}).get("timezone")
    name = " ".join(body.name.split())[:20]
    key, week = name.lower(), _week(tz)
    if await db.game_scores.count_documents({"tenant_id": tenant_id, "name_key": key, "created_at": {"$gte": now_iso()[:13]}}) >= 15:
        raise HTTPException(429, "Take a breather — try again in a bit")
    await db.game_scores.insert_one({"id": str(uuid.uuid4()), "tenant_id": tenant_id, "week": week, "name": name, "name_key": key, "score": body.score, "created_at": now_iso()})
    board = await _board(tenant_id)
    rank = next((i + 1 for i, r in enumerate(board) if r.name.lower() == key and r.score == body.score), len(board) + 1)
    code, pct, msg = None, 0, f"Score {TARGET}+ to win {PCT}% off retail."
    if body.score >= TARGET:
        pct = TOP_PCT if rank == 1 else PCT
        existing = await db.discount_codes.find_one({"tenant_id": tenant_id, "week": week, "name_key": key, "used_at": None})
        if existing and existing["pct"] >= pct:
            code, pct, msg = existing["code"], existing["pct"], "You already have a code this week — show it at checkout."
        else:
            if existing:
                await db.discount_codes.delete_one({"code": existing["code"], "tenant_id": tenant_id})
            code = f"FADE{pct}-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
            await db.discount_codes.insert_one({"id": str(uuid.uuid4()), "tenant_id": tenant_id, "code": code, "pct": pct, "week": week, "name_key": key,
                                                "issued_to": name, "used_at": None, "expires": (date.fromisoformat(today_iso(tz)) + timedelta(days=14)).isoformat(),
                                                "created_at": now_iso()})
            msg = f"You won {pct}% off any retail product! Show this code at the chair." + (" (#1 this week bonus)" if pct == TOP_PCT else "")
    return ScoreOut(rank=rank, best=rank == 1, code=code, pct=pct, message=msg)

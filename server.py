from __future__ import annotations

import hashlib
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import asyncpg
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
MEDIA_ROOT = Path(os.getenv("VOICE_GACHA_MEDIA_DIR", str(ROOT / "data")))
VIDEO_ROOT = MEDIA_ROOT / "videos"
MAX_VIDEO_BYTES = int(os.getenv("VOICE_GACHA_MAX_VIDEO_BYTES", str(500 * 1024 * 1024)))
SSR_RATE_BPS = max(0, min(10_000, int(os.getenv("VOICE_GACHA_SSR_RATE_BPS", "500"))))
SERIAL_PREFIX = "VG"
_redeem_attempts: dict[str, list[float]] = {}


class RedeemRequest(BaseModel):
    session: str = Field(default="", max_length=300)
    code: str = Field(min_length=8, max_length=100)


class SpinRequest(BaseModel):
    session: str = Field(min_length=8, max_length=300)
    gacha_type: Literal["boys", "girls"]
    request_id: str = Field(min_length=8, max_length=100)


class AdminAuthRequest(BaseModel):
    password: str = Field(max_length=300)


class IssueCodesRequest(AdminAuthRequest):
    count: int = Field(default=1, ge=1, le=1)
    spins: int = Field(ge=1, le=1000)
    memo: str = Field(default="", max_length=300)


class PrizeToggleRequest(AdminAuthRequest):
    prize_id: int
    active: bool


class PrizeDeleteRequest(AdminAuthRequest):
    prize_id: int


class RateUpdateRequest(AdminAuthRequest):
    gacha_type: Literal["boys", "girls"]
    r_percent: float = Field(ge=0, le=100)
    ssr_percent: float = Field(ge=0, le=100)


def normalize_code(value: str) -> str:
    return "".join(ch for ch in value.upper().strip() if ch.isalnum())


def hash_code(value: str) -> str:
    return hashlib.sha256(normalize_code(value).encode("utf-8")).hexdigest()


def make_serial_code() -> str:
    raw = secrets.token_hex(10).upper()
    return f"{SERIAL_PREFIX}-{raw[:5]}-{raw[5:10]}-{raw[10:15]}-{raw[15:]}"


def choose_rarity(
    has_r: bool,
    has_ssr: bool,
    draw: int | None = None,
    ssr_rate_bps: int = SSR_RATE_BPS,
) -> str:
    if not has_r and not has_ssr:
        raise ValueError("No active prizes")
    if not has_ssr:
        return "R"
    if not has_r:
        return "SSR"
    value = secrets.randbelow(10_000) if draw is None else draw
    return "SSR" if value < ssr_rate_bps else "R"


def check_admin(password: str) -> None:
    expected = os.getenv("VOICE_GACHA_ADMIN_PASSWORD", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Admin password is not configured")
    if not secrets.compare_digest(password or "", expected):
        raise HTTPException(status_code=403, detail="Invalid admin password")


def check_redeem_rate(key: str) -> None:
    now = time.monotonic()
    recent = [stamp for stamp in _redeem_attempts.get(key, []) if now - stamp < 300]
    if len(recent) >= 10:
        raise HTTPException(status_code=429, detail="Too many serial attempts")
    recent.append(now)
    _redeem_attempts[key] = recent


async def ensure_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_gacha_accounts (
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                spin_credits INTEGER NOT NULL DEFAULT 0 CHECK (spin_credits >= 0),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS voice_gacha_sessions (
                token_hash TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                user_name TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '10 years'
            );
            CREATE INDEX IF NOT EXISTS voice_gacha_sessions_user_idx
                ON voice_gacha_sessions(guild_id,user_id);

            CREATE TABLE IF NOT EXISTS voice_gacha_settings (
                gacha_type TEXT PRIMARY KEY CHECK (gacha_type IN ('boys','girls')),
                ssr_rate_bps INTEGER NOT NULL CHECK (ssr_rate_bps BETWEEN 0 AND 10000),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS voice_gacha_serial_codes (
                code_hash TEXT PRIMARY KEY,
                code_hint TEXT NOT NULL,
                spins INTEGER NOT NULL CHECK (spins > 0),
                memo TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                redeemed_guild_id TEXT,
                redeemed_user_id TEXT,
                redeemed_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS voice_gacha_serial_created_idx
                ON voice_gacha_serial_codes(created_at DESC);

            CREATE TABLE IF NOT EXISTS voice_gacha_prizes (
                id BIGSERIAL PRIMARY KEY,
                gacha_type TEXT NOT NULL CHECK (gacha_type IN ('boys','girls')),
                title TEXT NOT NULL,
                rarity TEXT NOT NULL CHECK (rarity IN ('R','SSR')),
                video_filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                video_mime TEXT NOT NULL DEFAULT 'video/mp4',
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS voice_gacha_prize_draw_idx
                ON voice_gacha_prizes(gacha_type, active, rarity);

            CREATE TABLE IF NOT EXISTS voice_gacha_collections (
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                prize_id BIGINT NOT NULL REFERENCES voice_gacha_prizes(id) ON DELETE CASCADE,
                owned_count INTEGER NOT NULL DEFAULT 1 CHECK (owned_count > 0),
                first_obtained_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_obtained_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, user_id, prize_id)
            );

            CREATE TABLE IF NOT EXISTS voice_gacha_pulls (
                request_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                gacha_type TEXT NOT NULL,
                prize_id BIGINT NOT NULL REFERENCES voice_gacha_prizes(id),
                rarity TEXT NOT NULL,
                was_new BOOLEAN NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS voice_gacha_pulls_user_idx
                ON voice_gacha_pulls(guild_id, user_id, created_at DESC);
            """
        )
        await conn.execute(
            """
            INSERT INTO voice_gacha_settings(gacha_type,ssr_rate_bps)
            VALUES ('boys',$1),('girls',$1)
            ON CONFLICT(gacha_type) DO NOTHING
            """,
            SSR_RATE_BPS,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    VIDEO_ROOT.mkdir(parents=True, exist_ok=True)
    app.state.pool = await asyncpg.create_pool(database_url, min_size=1, max_size=10)
    await ensure_schema(app.state.pool)
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="OASIS Voice Gachapon", lifespan=lifespan)


@app.middleware("http")
async def prevent_stale_admin_assets(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/admin/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


async def require_session(pool: asyncpg.Pool, token: str):
    if not token or len(token) > 300:
        raise HTTPException(status_code=403, detail="Invalid session")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE voice_gacha_sessions
            SET expires_at=NOW() + INTERVAL '10 years'
            WHERE token_hash=$1 AND expires_at>NOW()
            RETURNING user_id,guild_id,user_name
            """,
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
        )
    if not row:
        raise HTTPException(status_code=403, detail="Invalid session")
    return row


async def create_session(conn, guild_id: str, user_id: str, user_name: str) -> str:
    token = secrets.token_urlsafe(32)
    await conn.execute(
        """
        INSERT INTO voice_gacha_sessions(token_hash,guild_id,user_id,user_name)
        VALUES($1,$2,$3,$4)
        """,
        hashlib.sha256(token.encode("utf-8")).hexdigest(),
        guild_id,
        user_id,
        user_name,
    )
    return token


async def account_credits(conn, guild_id: str, user_id: str) -> int:
    return int(
        await conn.fetchval(
            """
            INSERT INTO voice_gacha_accounts(guild_id,user_id,spin_credits)
            VALUES($1,$2,0)
            ON CONFLICT(guild_id,user_id) DO UPDATE SET updated_at=NOW()
            RETURNING spin_credits
            """,
            guild_id,
            user_id,
        )
        or 0
    )


@app.get("/api/me")
async def me(session: str):
    data = await require_session(app.state.pool, session)
    async with app.state.pool.acquire() as conn:
        credits = await account_credits(conn, str(data["guild_id"]), str(data["user_id"]))
        counts = await conn.fetchrow(
            """
            SELECT COUNT(*)::INTEGER AS unique_count,
                   COALESCE(SUM(owned_count),0)::INTEGER AS total_count
            FROM voice_gacha_collections
            WHERE guild_id=$1 AND user_id=$2
            """,
            str(data["guild_id"]),
            str(data["user_id"]),
        )
        settings = await conn.fetch(
            "SELECT gacha_type,ssr_rate_bps FROM voice_gacha_settings ORDER BY gacha_type"
        )
    gacha_rates = {
        str(row["gacha_type"]): {
            "R": (10_000 - int(row["ssr_rate_bps"])) / 100,
            "SSR": int(row["ssr_rate_bps"]) / 100,
        }
        for row in settings
    }
    return {
        "user_id": str(data["user_id"]),
        "user_name": str(data["user_name"]),
        "credits": credits,
        "unique_count": int(counts["unique_count"] or 0),
        "total_count": int(counts["total_count"] or 0),
        "rates": gacha_rates.get(
            "girls", {"R": (10_000 - SSR_RATE_BPS) / 100, "SSR": SSR_RATE_BPS / 100}
        ),
        "gacha_rates": gacha_rates,
    }


@app.get("/api/rates")
async def public_rates():
    async with app.state.pool.acquire() as conn:
        settings = await conn.fetch(
            "SELECT gacha_type,ssr_rate_bps FROM voice_gacha_settings ORDER BY gacha_type"
        )
    return {
        "rates": {
            str(row["gacha_type"]): {
                "R": (10_000 - int(row["ssr_rate_bps"])) / 100,
                "SSR": int(row["ssr_rate_bps"]) / 100,
            }
            for row in settings
        }
    }


@app.post("/api/redeem")
async def redeem(req: RedeemRequest, request: Request):
    data = await require_session(app.state.pool, req.session) if req.session else None
    guild_id = str(data["guild_id"]) if data else "standalone"
    user_id = str(data["user_id"]) if data else ""
    check_redeem_rate(f"{request.client.host if request.client else 'unknown'}:{user_id}")
    digest = hash_code(req.code)

    async with app.state.pool.acquire() as conn:
        async with conn.transaction():
            code = await conn.fetchrow(
                "SELECT * FROM voice_gacha_serial_codes WHERE code_hash=$1 FOR UPDATE",
                digest,
            )
            if not code:
                raise HTTPException(status_code=404, detail="Invalid serial code")
            if code["redeemed_at"]:
                if data and str(code["redeemed_guild_id"]) == guild_id and str(code["redeemed_user_id"]) == user_id:
                    credits = await account_credits(conn, guild_id, user_id)
                    return {"ok": True, "already_redeemed": True, "added": 0, "credits": credits, "session": None}
                if not data:
                    restored_guild_id = str(code["redeemed_guild_id"])
                    restored_user_id = str(code["redeemed_user_id"])
                    account = await conn.fetchrow(
                        "SELECT spin_credits FROM voice_gacha_accounts WHERE guild_id=$1 AND user_id=$2",
                        restored_guild_id,
                        restored_user_id,
                    )
                    if not account:
                        raise HTTPException(status_code=409, detail="Serial account is unavailable")
                    user_name = f"コレクター-{restored_user_id[-6:].upper()}"
                    session_token = await create_session(
                        conn, restored_guild_id, restored_user_id, user_name
                    )
                    return {
                        "ok": True,
                        "already_redeemed": True,
                        "restored": True,
                        "added": 0,
                        "credits": int(account["spin_credits"]),
                        "session": session_token,
                    }
                raise HTTPException(status_code=409, detail="Serial code already redeemed")

            session_token = None
            if not data:
                user_id = uuid.uuid4().hex
                session_token = await create_session(
                    conn, guild_id, user_id, f"コレクター-{user_id[-6:].upper()}"
                )

            await conn.execute(
                """
                INSERT INTO voice_gacha_accounts(guild_id,user_id,spin_credits,updated_at)
                VALUES($1,$2,$3,NOW())
                ON CONFLICT(guild_id,user_id) DO UPDATE SET
                    spin_credits=voice_gacha_accounts.spin_credits + EXCLUDED.spin_credits,
                    updated_at=NOW()
                """,
                guild_id,
                user_id,
                int(code["spins"]),
            )
            await conn.execute(
                """
                UPDATE voice_gacha_serial_codes
                SET redeemed_guild_id=$2,redeemed_user_id=$3,redeemed_at=NOW()
                WHERE code_hash=$1
                """,
                digest,
                guild_id,
                user_id,
            )
            credits = await conn.fetchval(
                "SELECT spin_credits FROM voice_gacha_accounts WHERE guild_id=$1 AND user_id=$2",
                guild_id,
                user_id,
            )
    return {
        "ok": True,
        "already_redeemed": False,
        "added": int(code["spins"]),
        "credits": int(credits),
        "session": session_token,
    }


def prize_payload(row, session: str) -> dict:
    prize_id = int(row["id"] if "id" in row else row["prize_id"])
    return {
        "id": prize_id,
        "title": row["title"],
        "rarity": row["rarity"],
        "gacha_type": row["gacha_type"],
        "owned_count": int(row.get("owned_count", 1) if hasattr(row, "get") else 1),
        "is_new": bool(row.get("was_new", False) if hasattr(row, "get") else False),
        "video_url": f"/api/media/{prize_id}?session={session}",
        "download_url": f"/api/media/{prize_id}?session={session}&download=true",
    }


@app.post("/api/spin")
async def spin(req: SpinRequest):
    data = await require_session(app.state.pool, req.session)
    guild_id = str(data["guild_id"])
    user_id = str(data["user_id"])

    async with app.state.pool.acquire() as conn:
        async with conn.transaction():
            request_owner = await conn.fetchrow(
                "SELECT guild_id,user_id FROM voice_gacha_pulls WHERE request_id=$1",
                req.request_id,
            )
            if request_owner and (
                str(request_owner["guild_id"]) != guild_id or str(request_owner["user_id"]) != user_id
            ):
                raise HTTPException(status_code=409, detail="Request ID is already in use")
            prior = await conn.fetchrow(
                """
                SELECT pull.prize_id,pull.rarity,pull.gacha_type,pull.was_new,
                       prize.id,prize.title
                FROM voice_gacha_pulls pull
                JOIN voice_gacha_prizes prize ON prize.id=pull.prize_id
                WHERE pull.request_id=$1 AND pull.guild_id=$2 AND pull.user_id=$3
                """,
                req.request_id,
                guild_id,
                user_id,
            )
            if prior:
                credits = await account_credits(conn, guild_id, user_id)
                return {"ok": True, "credits": credits, "prize": prize_payload(prior, req.session)}

            account = await conn.fetchrow(
                """
                INSERT INTO voice_gacha_accounts(guild_id,user_id,spin_credits)
                VALUES($1,$2,0)
                ON CONFLICT(guild_id,user_id) DO UPDATE SET updated_at=NOW()
                RETURNING spin_credits
                """,
                guild_id,
                user_id,
            )
            await conn.execute(
                "SELECT 1 FROM voice_gacha_accounts WHERE guild_id=$1 AND user_id=$2 FOR UPDATE",
                guild_id,
                user_id,
            )
            if int(account["spin_credits"] or 0) <= 0:
                raise HTTPException(status_code=400, detail="No spin credits")

            rows = await conn.fetch(
                """
                SELECT id,title,rarity,gacha_type
                FROM voice_gacha_prizes
                WHERE gacha_type=$1 AND active=TRUE
                ORDER BY id
                FOR SHARE
                """,
                req.gacha_type,
            )
            ssr_rate_bps = await conn.fetchval(
                "SELECT ssr_rate_bps FROM voice_gacha_settings WHERE gacha_type=$1",
                req.gacha_type,
            )
            try:
                rarity = choose_rarity(
                    any(row["rarity"] == "R" for row in rows),
                    any(row["rarity"] == "SSR" for row in rows),
                    ssr_rate_bps=int(ssr_rate_bps if ssr_rate_bps is not None else SSR_RATE_BPS),
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            candidates = [row for row in rows if row["rarity"] == rarity]
            prize = candidates[secrets.randbelow(len(candidates))]

            credits = await conn.fetchval(
                """
                UPDATE voice_gacha_accounts
                SET spin_credits=spin_credits-1,updated_at=NOW()
                WHERE guild_id=$1 AND user_id=$2 AND spin_credits>0
                RETURNING spin_credits
                """,
                guild_id,
                user_id,
            )
            if credits is None:
                raise HTTPException(status_code=409, detail="Spin credit changed; retry")
            collection = await conn.fetchrow(
                """
                INSERT INTO voice_gacha_collections
                    (guild_id,user_id,prize_id,owned_count,first_obtained_at,last_obtained_at)
                VALUES($1,$2,$3,1,NOW(),NOW())
                ON CONFLICT(guild_id,user_id,prize_id) DO UPDATE SET
                    owned_count=voice_gacha_collections.owned_count+1,
                    last_obtained_at=NOW()
                RETURNING owned_count
                """,
                guild_id,
                user_id,
                int(prize["id"]),
            )
            was_new = int(collection["owned_count"]) == 1
            await conn.execute(
                """
                INSERT INTO voice_gacha_pulls
                    (request_id,guild_id,user_id,gacha_type,prize_id,rarity,was_new)
                VALUES($1,$2,$3,$4,$5,$6,$7)
                """,
                req.request_id,
                guild_id,
                user_id,
                req.gacha_type,
                int(prize["id"]),
                rarity,
                was_new,
            )

    enriched = dict(prize)
    enriched["was_new"] = was_new
    enriched["owned_count"] = int(collection["owned_count"])
    return {"ok": True, "credits": int(credits), "prize": prize_payload(enriched, req.session)}


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/collection")
async def collection(session: str, gacha_type: Literal["boys", "girls"] | None = None):
    data = await require_session(app.state.pool, session)
    args = [str(data["guild_id"]), str(data["user_id"])]
    condition = ""
    if gacha_type:
        args.append(gacha_type)
        condition = "AND prize.gacha_type=$3"
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT prize.id,prize.title,prize.rarity,prize.gacha_type,
                   owned.owned_count,owned.first_obtained_at,owned.last_obtained_at
            FROM voice_gacha_collections owned
            JOIN voice_gacha_prizes prize ON prize.id=owned.prize_id
            WHERE owned.guild_id=$1 AND owned.user_id=$2 {condition}
            ORDER BY CASE prize.rarity WHEN 'SSR' THEN 0 ELSE 1 END,
                     owned.last_obtained_at DESC
            """,
            *args,
        )
    return {"items": [prize_payload(row, session) for row in rows]}


@app.get("/api/media/{prize_id}")
async def prize_media(prize_id: int, session: str, download: bool = False):
    data = await require_session(app.state.pool, session)
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT prize.video_filename,prize.original_filename,prize.video_mime
            FROM voice_gacha_collections owned
            JOIN voice_gacha_prizes prize ON prize.id=owned.prize_id
            WHERE owned.guild_id=$1 AND owned.user_id=$2 AND prize.id=$3
            """,
            str(data["guild_id"]),
            str(data["user_id"]),
            prize_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Prize not owned")
    path = VIDEO_ROOT / row["video_filename"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Video file missing")
    return FileResponse(
        path,
        media_type=row["video_mime"],
        filename=row["original_filename"] if download else None,
        content_disposition_type="attachment" if download else "inline",
    )


@app.post("/api/admin/auth")
async def admin_auth(req: AdminAuthRequest):
    check_admin(req.password)
    return {"ok": True}


@app.post("/api/admin/codes")
async def issue_codes(req: IssueCodesRequest):
    check_admin(req.password)
    codes = [make_serial_code() for _ in range(req.count)]
    async with app.state.pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO voice_gacha_serial_codes(code_hash,code_hint,spins,memo)
                VALUES($1,$2,$3,$4)
                """,
                [
                    (hash_code(code), f"…{normalize_code(code)[-6:]}", req.spins, req.memo.strip())
                    for code in codes
                ],
            )
    return {"ok": True, "codes": codes, "spins": req.spins}


@app.post("/api/admin/overview")
async def admin_overview(req: AdminAuthRequest):
    check_admin(req.password)
    async with app.state.pool.acquire() as conn:
        prizes = await conn.fetch(
            """
            SELECT id,gacha_type,title,rarity,original_filename,active,created_at
            FROM voice_gacha_prizes ORDER BY gacha_type,rarity,id DESC
            """
        )
        codes = await conn.fetch(
            """
            SELECT code_hint,spins,memo,created_at,redeemed_user_id,redeemed_at
            FROM voice_gacha_serial_codes ORDER BY created_at DESC LIMIT 100
            """
        )
        stats = await conn.fetchrow(
            """
            SELECT
              (SELECT COUNT(*) FROM voice_gacha_pulls)::INTEGER AS pulls,
              (SELECT COUNT(*) FROM voice_gacha_collections)::INTEGER AS unique_unlocks,
              (SELECT COUNT(*) FROM voice_gacha_serial_codes WHERE redeemed_at IS NOT NULL)::INTEGER AS redeemed_codes
            """
        )
        settings = await conn.fetch(
            "SELECT gacha_type,ssr_rate_bps,updated_at FROM voice_gacha_settings ORDER BY gacha_type"
        )
    return {
        "prizes": [dict(row) for row in prizes],
        "codes": [dict(row) for row in codes],
        "stats": dict(stats),
        "rates": {
            str(row["gacha_type"]): {
                "R": (10_000 - int(row["ssr_rate_bps"])) / 100,
                "SSR": int(row["ssr_rate_bps"]) / 100,
                "updated_at": row["updated_at"],
            }
            for row in settings
        },
    }


@app.post("/api/admin/rates")
async def update_rates(req: RateUpdateRequest):
    check_admin(req.password)
    if abs((req.r_percent + req.ssr_percent) - 100) > 0.001:
        raise HTTPException(status_code=400, detail="RとSSRの合計を100%にしてください")
    ssr_rate_bps = int(round(req.ssr_percent * 100))
    async with app.state.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO voice_gacha_settings(gacha_type,ssr_rate_bps,updated_at)
            VALUES($1,$2,NOW())
            ON CONFLICT(gacha_type) DO UPDATE SET
                ssr_rate_bps=EXCLUDED.ssr_rate_bps,
                updated_at=NOW()
            """,
            req.gacha_type,
            ssr_rate_bps,
        )
    return {
        "ok": True,
        "gacha_type": req.gacha_type,
        "rates": {"R": (10_000 - ssr_rate_bps) / 100, "SSR": ssr_rate_bps / 100},
    }


@app.post("/api/admin/prizes")
async def upload_prize(
    password: str = Form(...),
    gacha_type: Literal["boys", "girls"] = Form(...),
    title: str = Form(..., min_length=1, max_length=120),
    rarity: Literal["R", "SSR"] = Form(...),
    video: UploadFile = File(...),
):
    check_admin(password)
    content_type = (video.content_type or "application/octet-stream").lower()
    if not content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="A video file is required")
    suffix = Path(video.filename or "").suffix.lower()
    if suffix not in {".mp4", ".webm", ".mov", ".m4v"}:
        suffix = ".mp4"
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    destination = VIDEO_ROOT / stored_name
    size = 0
    try:
        with destination.open("wb") as handle:
            while chunk := await video.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_VIDEO_BYTES:
                    raise HTTPException(status_code=413, detail="Video is too large")
                handle.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="Video is empty")
        async with app.state.pool.acquire() as conn:
            prize_id = await conn.fetchval(
                """
                INSERT INTO voice_gacha_prizes
                    (gacha_type,title,rarity,video_filename,original_filename,video_mime)
                VALUES($1,$2,$3,$4,$5,$6) RETURNING id
                """,
                gacha_type,
                title.strip(),
                rarity,
                stored_name,
                Path(video.filename or f"voice{suffix}").name,
                content_type,
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await video.close()
    return {"ok": True, "prize_id": int(prize_id)}


@app.post("/api/admin/prizes/toggle")
async def toggle_prize(req: PrizeToggleRequest):
    check_admin(req.password)
    async with app.state.pool.acquire() as conn:
        changed = await conn.fetchval(
            "UPDATE voice_gacha_prizes SET active=$2 WHERE id=$1 RETURNING id",
            req.prize_id,
            req.active,
        )
    if not changed:
        raise HTTPException(status_code=404, detail="Prize not found")
    return {"ok": True}


@app.post("/api/admin/prizes/delete")
async def delete_prize(req: PrizeDeleteRequest):
    check_admin(req.password)
    video_filename = None
    async with app.state.pool.acquire() as conn:
        async with conn.transaction():
            prize = await conn.fetchrow(
                "SELECT id,video_filename FROM voice_gacha_prizes WHERE id=$1 FOR UPDATE",
                req.prize_id,
            )
            if not prize:
                raise HTTPException(status_code=404, detail="景品が見つかりません")
            pull_count = await conn.fetchval(
                "SELECT COUNT(*) FROM voice_gacha_pulls WHERE prize_id=$1",
                req.prize_id,
            )
            if int(pull_count or 0) > 0:
                raise HTTPException(
                    status_code=409,
                    detail="獲得履歴がある景品は削除できません。排出停止を使用してください。",
                )
            video_filename = str(prize["video_filename"])
            await conn.execute("DELETE FROM voice_gacha_prizes WHERE id=$1", req.prize_id)
    if video_filename:
        (VIDEO_ROOT / video_filename).unlink(missing_ok=True)
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_ROOT, html=True), name="static")

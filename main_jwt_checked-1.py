from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional

import os
import secrets
import time

import psycopg
from psycopg.rows import dict_row
import stripe
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from security import (
    get_current_admin_user,
    get_current_user,
    issue_login_token,
    register_user,
)

app = FastAPI()

DATABASE_URL = os.environ["DATABASE_URL"]
STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
SITE_URL = os.environ["SITE_URL"].rstrip("/")

stripe.api_key = STRIPE_SECRET_KEY

withdraw_rate_limit = defaultdict(list)
RATE_LIMIT_SECONDS = 60


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        raise exc
    return JSONResponse(status_code=500, content={"detail": "サーバー内部エラー"})


def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


@contextmanager
def db_cursor():
    conn = get_db()
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()
        conn.close()


@contextmanager
def db_transaction():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def ensure_user_row_exists(cur, user_id: str):
    cur.execute(
        """
        INSERT INTO users (
            user_id,
            points,
            free_gacha,
            locked_points,
            post_count,
            role,
            token_version,
            is_active
        )
        VALUES (%s, 0, 0, 0, 0, 'user', 0, TRUE)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id,),
    )


def now_iso() -> str:
    return datetime.utcnow().isoformat()


def get_current_user_id(conn, request: Request) -> str:
    user = get_current_user(conn, request)
    return user["user_id"]


def get_current_admin_user_id(conn, request: Request) -> str:
    user = get_current_admin_user(conn, request)
    return user["user_id"]


def init_db():
    with db_transaction() as (_, cur):
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                password_hash TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                token_version INTEGER NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                points INTEGER NOT NULL DEFAULT 0,
                free_gacha INTEGER NOT NULL DEFAULT 0,
                locked_points INTEGER NOT NULL DEFAULT 0,
                post_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user'")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS points INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS free_gacha INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_points INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS post_count INTEGER NOT NULL DEFAULT 0")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS prompts (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                original_creator_user_id TEXT,
                title TEXT,
                content TEXT,
                category TEXT,
                url TEXT,
                created_at TEXT,
                review_status TEXT NOT NULL DEFAULT 'accepted',
                is_visible BOOLEAN NOT NULL DEFAULT TRUE,
                bundle_entry_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                resale_offer_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                bundle_consented_at TIMESTAMP NULL,
                reviewed_at TIMESTAMP NULL,
                review_note TEXT
            )
            """
        )

        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS original_creator_user_id TEXT")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS is_visible BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS bundle_entry_enabled BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS resale_offer_enabled BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS bundle_consented_at TIMESTAMP NULL")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP NULL")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS review_note TEXT")
        cur.execute(
            """
            UPDATE prompts
            SET original_creator_user_id = user_id
            WHERE original_creator_user_id IS NULL
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS gacha_logs (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                created_at TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS creator_wallets (
                user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                yen INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                amount_yen INTEGER,
                method TEXT,
                destination TEXT,
                withdraw_code TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                admin_note TEXT,
                created_at TEXT,
                processed_at TIMESTAMP NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                stripe_session_id TEXT UNIQUE,
                stripe_payment_intent_id TEXT,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                product_code TEXT NOT NULL,
                points_to_add INTEGER NOT NULL,
                amount_jpy INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS withdraw_codes (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                code TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bundles (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                target_article_count INTEGER NOT NULL DEFAULT 1,
                genre TEXT NOT NULL DEFAULT 'その他',
                price_points INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'recruiting',
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                published_at TIMESTAMP NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bundle_items (
                id SERIAL PRIMARY KEY,
                bundle_id INTEGER NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
                prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                entry_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                original_creator_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                entry_type TEXT NOT NULL DEFAULT 'own',
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (bundle_id, prompt_id, entry_user_id)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bundle_purchases (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                bundle_id INTEGER NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
                price_points INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, bundle_id)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bundle_reward_distributions (
                id SERIAL PRIMARY KEY,
                bundle_id INTEGER NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
                prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                creator_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                amount_yen INTEGER NOT NULL,
                reward_type TEXT NOT NULL DEFAULT 'creator',
                distribution_round INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (bundle_id, prompt_id, reward_type, distribution_round, creator_user_id)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_stop_requests (
                id SERIAL PRIMARY KEY,
                prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                processed_at TIMESTAMP NULL
            )
            """
        )

        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_prompts_review_status'
                ) THEN
                    ALTER TABLE prompts
                    ADD CONSTRAINT chk_prompts_review_status
                    CHECK (review_status IN ('pending_review', 'accepted', 'rejected'));
                END IF;
            END $$;
            """
        )

        cur.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_bundles_status'
                ) THEN
                    ALTER TABLE bundles DROP CONSTRAINT chk_bundles_status;
                END IF;
                ALTER TABLE bundles
                ADD CONSTRAINT chk_bundles_status
                CHECK (status IN ('recruiting', 'active', 'closed'));
            END $$;
            """
        )

        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_withdrawal_requests_status'
                ) THEN
                    ALTER TABLE withdrawal_requests
                    ADD CONSTRAINT chk_withdrawal_requests_status
                    CHECK (status IN ('pending', 'approved', 'paid', 'rejected'));
                END IF;
            END $$;
            """
        )

        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_withdrawal_requests_method'
                ) THEN
                    ALTER TABLE withdrawal_requests
                    ADD CONSTRAINT chk_withdrawal_requests_method
                    CHECK (method IN ('paypay', 'amazon_gift'));
                END IF;
            END $$;
            """
        )

        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_bundle_reward_distributions_reward_type'
                ) THEN
                    ALTER TABLE bundle_reward_distributions
                    ADD CONSTRAINT chk_bundle_reward_distributions_reward_type
                    CHECK (reward_type IN ('creator', 'original_creator'));
                END IF;
            END $$;
            """
        )

        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_prompt_stop_requests_status'
                ) THEN
                    ALTER TABLE prompt_stop_requests
                    ADD CONSTRAINT chk_prompt_stop_requests_status
                    CHECK (status IN ('pending', 'approved', 'rejected'));
                END IF;
            END $$;
            """
        )

        index_statements = [
            "CREATE INDEX IF NOT EXISTS ix_users_role ON users(role)",
            "CREATE INDEX IF NOT EXISTS ix_users_is_active ON users(is_active)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_user_id ON prompts(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_category ON prompts(category)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_created_at ON prompts(created_at)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_review_status ON prompts(review_status)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_is_visible ON prompts(is_visible)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_bundle_entry_enabled ON prompts(bundle_entry_enabled)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_resale_offer_enabled ON prompts(resale_offer_enabled)",
            "CREATE INDEX IF NOT EXISTS ix_gacha_logs_user_id ON gacha_logs(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_gacha_logs_prompt_id ON gacha_logs(prompt_id)",
            "CREATE INDEX IF NOT EXISTS ix_payments_user_id ON payments(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_payments_status ON payments(status)",
            "CREATE INDEX IF NOT EXISTS ix_withdraw_codes_user_id ON withdraw_codes(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_withdraw_codes_code ON withdraw_codes(code)",
            "CREATE INDEX IF NOT EXISTS ix_withdrawal_requests_user_id ON withdrawal_requests(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_withdrawal_requests_status ON withdrawal_requests(status)",
            "CREATE INDEX IF NOT EXISTS ix_bundles_status ON bundles(status)",
            "CREATE INDEX IF NOT EXISTS ix_bundle_items_bundle_id ON bundle_items(bundle_id)",
            "CREATE INDEX IF NOT EXISTS ix_bundle_items_prompt_id ON bundle_items(prompt_id)",
            "CREATE INDEX IF NOT EXISTS ix_bundle_items_entry_user_id ON bundle_items(entry_user_id)",
            "CREATE INDEX IF NOT EXISTS ix_bundle_reward_distributions_bundle_id ON bundle_reward_distributions(bundle_id)",
            "CREATE INDEX IF NOT EXISTS ix_bundle_reward_distributions_prompt_id ON bundle_reward_distributions(prompt_id)",
            "CREATE INDEX IF NOT EXISTS ix_prompt_stop_requests_prompt_id ON prompt_stop_requests(prompt_id)",
            "CREATE INDEX IF NOT EXISTS ix_prompt_stop_requests_user_id ON prompt_stop_requests(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_prompt_stop_requests_status ON prompt_stop_requests(status)",
        ]
        for stmt in index_statements:
            cur.execute(stmt)


init_db()


class RegisterRequest(BaseModel):
    user_id: str
    password: str


class LoginRequest(BaseModel):
    user_id: str
    password: str


class CreatePromptRequest(BaseModel):
    title: str
    content: str
    category: str
    url: Optional[str] = None
    bundle_consent: bool


class GachaRequest(BaseModel):
    category: Optional[str] = None


class CreateCheckoutSessionRequest(BaseModel):
    product_code: str


class WithdrawCodeRequest(BaseModel):
    pass


class CreateWithdrawalRequest(BaseModel):
    amount_yen: int
    method: str
    destination: str
    withdraw_code: str


class CreateBundleRequest(BaseModel):
    title: str
    description: Optional[str] = None
    target_article_count: int
    genre: str
    price_points: int


class AddBundleItemRequest(BaseModel):
    bundle_id: int
    prompt_id: int


class BundleEntryRequest(BaseModel):
    bundle_id: int
    prompt_id: int


class PublishBundleRequest(BaseModel):
    bundle_id: int


class BuyBundleRequest(BaseModel):
    bundle_id: int


class DistributeBundleRequest(BaseModel):
    bundle_id: int
    distribution_round: int = 1


class TogglePromptFlagRequest(BaseModel):
    enabled: bool


class PromptStopRequest(BaseModel):
    reason: Optional[str] = None


class ProcessPromptStopRequest(BaseModel):
    status: str
    admin_note: Optional[str] = None


def get_product_config(product_code: str) -> dict:
    if product_code == "300":
        return {"points": 300, "amount_jpy": 300, "name": "300ポイント"}
    if product_code == "1000":
        return {"points": 1000, "amount_jpy": 900, "name": "1000ポイント"}
    raise HTTPException(status_code=400, detail="商品エラー")


@app.post("/auth/register")
def auth_register(req: RegisterRequest):
    with db_transaction() as (conn, _):
        created = register_user(conn, user_id=req.user_id, password=req.password)
        return {
            "status": "ok",
            "user_id": created["user_id"],
            "role": created["role"],
        }


@app.post("/auth/login")
def auth_login(req: LoginRequest):
    with db_transaction() as (conn, _):
        return issue_login_token(conn, user_id=req.user_id, password=req.password)


@app.post("/prompts")
def create_prompt(req: CreatePromptRequest, request: Request):
    with db_transaction() as (conn, cur):
        current_user = get_current_user(conn, request)
        user_id = current_user["user_id"]
        ensure_user_row_exists(cur, user_id)

        if not req.bundle_consent:
            raise HTTPException(status_code=400, detail="福袋利用規約に同意してください")

        cur.execute(
            """
            SELECT post_count, points
            FROM users
            WHERE user_id = %s
            FOR UPDATE
            """,
            (user_id,),
        )
        user = cur.fetchone()

        current_post_count = user["post_count"]
        if current_post_count >= 10:
            if user["points"] < 100:
                raise HTTPException(status_code=400, detail="11記事目以降の投稿には100pt必要です")
            cur.execute(
                """
                UPDATE users
                SET points = points - 100
                WHERE user_id = %s
                """,
                (user_id,),
            )

        cur.execute(
            """
            INSERT INTO prompts (
                user_id,
                original_creator_user_id,
                title,
                content,
                category,
                url,
                created_at,
                review_status,
                is_visible,
                bundle_entry_enabled,
                resale_offer_enabled,
                bundle_consented_at,
                reviewed_at,
                review_note
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'accepted', TRUE, TRUE, TRUE, NOW(), NOW(), '自動承認')
            """,
            (
                user_id,
                user_id,
                req.title,
                req.content,
                req.category,
                req.url,
                now_iso(),
            ),
        )

        cur.execute(
            """
            UPDATE users
            SET free_gacha = free_gacha + 1,
                post_count = post_count + 1
            WHERE user_id = %s
            """,
            (user_id,),
        )

        return {"status": "ok"}


@app.get("/articles/latest")
def get_latest_articles(limit: int = Query(default=10, le=100)):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, title, url, category, created_at
            FROM prompts
            WHERE review_status = 'accepted'
              AND is_visible = TRUE
            ORDER BY CAST(created_at AS TIMESTAMP) DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "url": row["url"] if row.get("url") else None,
                "category": row["category"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]


@app.post("/gacha/draw")
def draw_gacha(req: GachaRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)
        ensure_user_row_exists(cur, user_id)

        cur.execute(
            """
            SELECT *
            FROM users
            WHERE user_id = %s
            FOR UPDATE
            """,
            (user_id,),
        )
        user = cur.fetchone()

        use_free = user["free_gacha"] > 0
        if not use_free and user["points"] < 30:
            raise HTTPException(status_code=400, detail="ポイント不足")

        cur.execute(
            """
            SELECT *
            FROM prompts
            WHERE review_status = 'accepted'
              AND is_visible = TRUE
            ORDER BY RANDOM()
            LIMIT 1
            """
        )
        prompt = cur.fetchone()
        if not prompt:
            raise HTTPException(status_code=404, detail="対象なし")

        creator_id = prompt["user_id"]

        if use_free:
            cur.execute(
                """
                UPDATE users
                SET free_gacha = free_gacha - 1
                WHERE user_id = %s
                """,
                (user_id,),
            )
        else:
            cur.execute(
                """
                UPDATE users
                SET points = points - 30
                WHERE user_id = %s
                """,
                (user_id,),
            )

        cur.execute(
            """
            INSERT INTO gacha_logs (user_id, prompt_id, created_at)
            VALUES (%s, %s, %s)
            """,
            (user_id, prompt["id"], now_iso()),
        )

        if not use_free:
            cur.execute(
                """
                INSERT INTO creator_wallets (user_id, yen)
                VALUES (%s, 15)
                ON CONFLICT (user_id)
                DO UPDATE SET yen = creator_wallets.yen + 15
                """,
                (creator_id,),
            )

        return {
            "result": {
                "id": prompt["id"],
                "title": prompt["title"],
                "content": prompt["content"],
                "category": prompt["category"],
                "url": prompt["url"],
            }
        }


@app.get("/gacha/ad")
def get_ad():
    ads = [
        {"text": "今だけ特別キャンペーン中！"},
        {"text": "副業・収益化に役立つ情報をチェック"},
        {"text": "記事を投稿して放置収益化"},
    ]
    return ads[int(time.time()) % len(ads)]


@app.post("/stripe/create-checkout-session")
def create_checkout_session(req: CreateCheckoutSessionRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)
        ensure_user_row_exists(cur, user_id)
        product = get_product_config(req.product_code)

        session = stripe.checkout.Session.create(
            mode="payment",
            success_url=f"{SITE_URL}/mypage.html?checkout=success",
            cancel_url=f"{SITE_URL}/mypage.html?checkout=cancel",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "jpy",
                        "unit_amount": product["amount_jpy"],
                        "product_data": {
                            "name": product["name"],
                            "description": f"{product['points']}pt を自動付与",
                        },
                    },
                    "quantity": 1,
                }
            ],
            metadata={
                "user_id": user_id,
                "product_code": req.product_code,
                "points_to_add": str(product["points"]),
                "amount_jpy": str(product["amount_jpy"]),
            },
        )

        cur.execute(
            """
            INSERT INTO payments (
                stripe_session_id,
                user_id,
                product_code,
                points_to_add,
                amount_jpy,
                status,
                created_at
            )
            VALUES (%s, %s, %s, %s, %s, 'pending', %s)
            ON CONFLICT (stripe_session_id) DO NOTHING
            """,
            (
                session.id,
                user_id,
                req.product_code,
                product["points"],
                product["amount_jpy"],
                now_iso(),
            ),
        )

        return {"checkout_url": session.url}


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        stripe_session_id = session["id"]
        stripe_payment_intent_id = session.get("payment_intent")
        metadata = session.get("metadata", {})

        user_id = metadata.get("user_id")
        product_code = metadata.get("product_code")
        points_to_add = int(metadata.get("points_to_add", "0"))
        amount_jpy = int(metadata.get("amount_jpy", "0"))

        if not user_id or not product_code or points_to_add <= 0:
            return JSONResponse({"received": True})

        with db_transaction() as (_, cur):
            ensure_user_row_exists(cur, user_id)

            cur.execute(
                """
                SELECT *
                FROM payments
                WHERE stripe_session_id = %s
                FOR UPDATE
                """,
                (stripe_session_id,),
            )
            payment = cur.fetchone()

            if payment and payment["status"] == "paid":
                return JSONResponse({"received": True})

            if not payment:
                cur.execute(
                    """
                    INSERT INTO payments (
                        stripe_session_id,
                        stripe_payment_intent_id,
                        user_id,
                        product_code,
                        points_to_add,
                        amount_jpy,
                        status,
                        created_at,
                        completed_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'paid', %s, %s)
                    """,
                    (
                        stripe_session_id,
                        stripe_payment_intent_id,
                        user_id,
                        product_code,
                        points_to_add,
                        amount_jpy,
                        now_iso(),
                        now_iso(),
                    ),
                )
                cur.execute(
                    """
                    UPDATE users
                    SET points = points + %s
                    WHERE user_id = %s
                    """,
                    (points_to_add, user_id),
                )
                return JSONResponse({"received": True})

            cur.execute(
                """
                UPDATE users
                SET points = points + %s
                WHERE user_id = %s
                """,
                (points_to_add, user_id),
            )
            cur.execute(
                """
                UPDATE payments
                SET status = 'paid',
                    stripe_payment_intent_id = %s,
                    completed_at = %s
                WHERE stripe_session_id = %s
                """,
                (stripe_payment_intent_id, now_iso(), stripe_session_id),
            )

    return JSONResponse({"received": True})


@app.get("/mypage/history")
def mypage_history(request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)
        cur.execute(
            """
            SELECT
                p.title,
                p.content,
                p.category,
                MAX(g.created_at) AS viewed_at
            FROM gacha_logs g
            INNER JOIN prompts p
                ON g.prompt_id = p.id
            WHERE g.user_id = %s
            GROUP BY p.id, p.title, p.content, p.category
            ORDER BY viewed_at DESC
            LIMIT 50
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        return [
            {
                "title": row["title"],
                "content": row["content"],
                "category": row["category"],
            }
            for row in rows
        ]


@app.get("/mypage/earnings")
def mypage_earnings(request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        cur.execute(
            """
            SELECT COALESCE(COUNT(g.id) * 15, 0) AS gacha_yen
            FROM gacha_logs g
            INNER JOIN prompts p ON g.prompt_id = p.id
            WHERE p.user_id = %s
            """,
            (user_id,),
        )
        gacha_yen = cur.fetchone()["gacha_yen"]

        cur.execute(
            """
            SELECT COALESCE(SUM(amount_yen), 0) AS bundle_creator_yen
            FROM bundle_reward_distributions
            WHERE creator_user_id = %s
              AND reward_type = 'creator'
            """,
            (user_id,),
        )
        bundle_creator_yen = cur.fetchone()["bundle_creator_yen"]

        cur.execute(
            """
            SELECT COALESCE(SUM(amount_yen), 0) AS bundle_original_yen
            FROM bundle_reward_distributions
            WHERE creator_user_id = %s
              AND reward_type = 'original_creator'
            """,
            (user_id,),
        )
        bundle_original_yen = cur.fetchone()["bundle_original_yen"]

        total_yen = gacha_yen + bundle_creator_yen + bundle_original_yen

        return {
            "total_yen": total_yen,
            "gacha_yen": gacha_yen,
            "bundle_creator_yen": bundle_creator_yen,
            "bundle_original_yen": bundle_original_yen,
        }


@app.get("/mypage/status")
def mypage_status(request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)
        cur.execute(
            """
            SELECT post_count
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        user = cur.fetchone()
        post_count = user["post_count"] if user else 0
        next_cost = 0 if post_count < 10 else 100
        return {
            "post_count": post_count,
            "free_limit": 10,
            "next_cost": next_cost,
        }


@app.get("/mypage/prompts")
def mypage_prompts(request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)
        cur.execute(
            """
            SELECT
                p.id,
                p.title,
                p.category,
                p.url,
                p.created_at,
                p.resale_offer_enabled,
                p.bundle_entry_enabled,
                p.is_visible,
                EXISTS (
                    SELECT 1
                    FROM prompt_stop_requests psr
                    WHERE psr.prompt_id = p.id
                      AND psr.user_id = %s
                      AND psr.status = 'pending'
                ) AS has_pending_stop_request
            FROM prompts p
            WHERE p.user_id = %s
            ORDER BY CAST(p.created_at AS TIMESTAMP) DESC, p.id DESC
            """,
            (user_id, user_id),
        )
        rows = cur.fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "category": row["category"],
                "url": row["url"],
                "created_at": row["created_at"],
                "resale_offer_enabled": row["resale_offer_enabled"],
                "bundle_entry_enabled": row["bundle_entry_enabled"],
                "is_visible": row["is_visible"],
                "has_pending_stop_request": row["has_pending_stop_request"],
            }
            for row in rows
        ]


@app.get("/mypage")
def mypage(request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        cur.execute(
            """
            SELECT yen
            FROM creator_wallets
            WHERE user_id = %s
            """,
            (user_id,),
        )
        wallet = cur.fetchone()

        cur.execute(
            """
            SELECT points, locked_points, post_count
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        user = cur.fetchone()

        return {
            "user_id": user_id,
            "yen": wallet["yen"] if wallet else 0,
            "points": user["points"] if user else 0,
            "locked_points": user["locked_points"] if user else 0,
            "post_count": user["post_count"] if user else 0,
        }


@app.post("/mypage/prompts/{prompt_id}/resale-toggle")
def toggle_prompt_resale(prompt_id: int, req: TogglePromptFlagRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)
        cur.execute(
            """
            SELECT id, user_id
            FROM prompts
            WHERE id = %s
            FOR UPDATE
            """,
            (prompt_id,),
        )
        prompt = cur.fetchone()
        if not prompt:
            raise HTTPException(status_code=404, detail="記事が見つかりません")
        if prompt["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="自分の記事のみ変更できます")

        cur.execute(
            """
            UPDATE prompts
            SET resale_offer_enabled = %s
            WHERE id = %s
            """,
            (req.enabled, prompt_id),
        )
        return {"status": "ok", "prompt_id": prompt_id, "resale_offer_enabled": req.enabled}


@app.post("/mypage/prompts/{prompt_id}/bundle-toggle")
def toggle_prompt_bundle(prompt_id: int, req: TogglePromptFlagRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)
        cur.execute(
            """
            SELECT id, user_id
            FROM prompts
            WHERE id = %s
            FOR UPDATE
            """,
            (prompt_id,),
        )
        prompt = cur.fetchone()
        if not prompt:
            raise HTTPException(status_code=404, detail="記事が見つかりません")
        if prompt["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="自分の記事のみ変更できます")

        cur.execute(
            """
            UPDATE prompts
            SET bundle_entry_enabled = %s
            WHERE id = %s
            """,
            (req.enabled, prompt_id),
        )
        return {"status": "ok", "prompt_id": prompt_id, "bundle_entry_enabled": req.enabled}


@app.post("/mypage/prompts/{prompt_id}/stop-request")
def create_prompt_stop_request(prompt_id: int, req: PromptStopRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)
        cur.execute(
            """
            SELECT id, user_id
            FROM prompts
            WHERE id = %s
            FOR UPDATE
            """,
            (prompt_id,),
        )
        prompt = cur.fetchone()
        if not prompt:
            raise HTTPException(status_code=404, detail="記事が見つかりません")
        if prompt["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="自分の記事のみ申請できます")

        cur.execute(
            """
            SELECT id
            FROM prompt_stop_requests
            WHERE prompt_id = %s
              AND user_id = %s
              AND status = 'pending'
            LIMIT 1
            """,
            (prompt_id, user_id),
        )
        existing = cur.fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="掲載停止申請は受付中です")

        cur.execute(
            """
            INSERT INTO prompt_stop_requests (
                prompt_id,
                user_id,
                reason,
                status
            )
            VALUES (%s, %s, %s, 'pending')
            RETURNING id
            """,
            (prompt_id, user_id, req.reason),
        )
        row = cur.fetchone()
        return {"status": "pending", "request_id": row["id"], "prompt_id": prompt_id}


@app.post("/withdraw/code")
def create_withdraw_code(request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        now = time.time()
        withdraw_rate_limit[user_id] = [
            t for t in withdraw_rate_limit[user_id]
            if now - t < RATE_LIMIT_SECONDS
        ]
        if len(withdraw_rate_limit[user_id]) >= 1:
            raise HTTPException(status_code=429, detail="出金コードは1分間に1回までです")
        withdraw_rate_limit[user_id].append(now)

        code = f"{secrets.randbelow(900000) + 100000}"
        expires = datetime.utcnow() + timedelta(minutes=10)

        cur.execute(
            """
            UPDATE withdraw_codes
            SET used = TRUE
            WHERE user_id = %s AND used = FALSE
            """,
            (user_id,),
        )

        cur.execute(
            """
            INSERT INTO withdraw_codes (user_id, code, expires_at, used)
            VALUES (%s, %s, %s, FALSE)
            """,
            (user_id, code, expires),
        )

        return {"code": code, "expires_in": "10分"}


@app.post("/withdraw/request")
def create_withdraw_request(req: CreateWithdrawalRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        if req.amount_yen < 1000:
            raise HTTPException(status_code=400, detail="出金申請は1000円以上です")
        if req.method not in ("paypay", "amazon_gift"):
            raise HTTPException(status_code=400, detail="送金方法エラー")

        cur.execute(
            """
            SELECT yen
            FROM creator_wallets
            WHERE user_id = %s
            FOR UPDATE
            """,
            (user_id,),
        )
        wallet = cur.fetchone()
        current_yen = wallet["yen"] if wallet else 0
        if current_yen < req.amount_yen:
            raise HTTPException(status_code=400, detail="残高不足")

        cur.execute(
            """
            SELECT id, used, expires_at
            FROM withdraw_codes
            WHERE user_id = %s AND code = %s
            ORDER BY created_at DESC
            LIMIT 1
            FOR UPDATE
            """,
            (user_id, req.withdraw_code),
        )
        code_row = cur.fetchone()
        if not code_row:
            raise HTTPException(status_code=400, detail="出金コードが正しくありません")
        if code_row["used"]:
            raise HTTPException(status_code=400, detail="この出金コードは使用済みです")
        if code_row["expires_at"] < datetime.utcnow():
            raise HTTPException(status_code=400, detail="出金コードの有効期限が切れています")

        cur.execute(
            """
            UPDATE creator_wallets
            SET yen = yen - %s
            WHERE user_id = %s
            """,
            (req.amount_yen, user_id),
        )
        cur.execute(
            """
            UPDATE withdraw_codes
            SET used = TRUE
            WHERE id = %s
            """,
            (code_row["id"],),
        )
        cur.execute(
            """
            INSERT INTO withdrawal_requests (
                user_id, amount_yen, method, destination,
                withdraw_code, status, created_at
            )
            VALUES (%s, %s, %s, %s, %s, 'pending', %s)
            RETURNING id
            """,
            (user_id, req.amount_yen, req.method, req.destination, req.withdraw_code, now_iso()),
        )
        row = cur.fetchone()
        return {"status": "pending", "request_id": row["id"]}


@app.post("/admin/bundles")
def create_bundle(req: CreateBundleRequest, request: Request):
    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request)

        if req.target_article_count <= 0:
            raise HTTPException(status_code=400, detail="募集記事数は1以上にしてください")
        if req.price_points <= 0:
            raise HTTPException(status_code=400, detail="価格は1以上にしてください")

        cur.execute(
            """
            INSERT INTO bundles (
                title,
                description,
                target_article_count,
                genre,
                price_points,
                status
            )
            VALUES (%s, %s, %s, %s, %s, 'recruiting')
            RETURNING id
            """,
            (req.title, req.description, req.target_article_count, req.genre, req.price_points),
        )
        bundle = cur.fetchone()
        return {"bundle_id": bundle["id"]}


@app.post("/admin/bundles/items")
def add_bundle_item(req: AddBundleItemRequest, request: Request):
    with db_transaction() as (conn, cur):
        entry_user_id = get_current_admin_user_id(conn, request)

        cur.execute(
            """
            SELECT id, user_id, original_creator_user_id, review_status, bundle_entry_enabled, is_visible
            FROM prompts
            WHERE id = %s
            FOR UPDATE
            """,
            (req.prompt_id,),
        )
        prompt = cur.fetchone()
        if not prompt:
            raise HTTPException(status_code=404, detail="記事なし")
        if prompt["review_status"] != "accepted":
            raise HTTPException(status_code=400, detail="accepted記事のみ採用可能です")
        if not prompt["bundle_entry_enabled"] or not prompt["is_visible"]:
            raise HTTPException(status_code=400, detail="福袋利用不可の記事です")

        original_creator_user_id = prompt["original_creator_user_id"] or prompt["user_id"]
        entry_type = "own" if prompt["user_id"] == entry_user_id else "gacha"

        cur.execute(
            """
            INSERT INTO bundle_items (
                bundle_id,
                prompt_id,
                entry_user_id,
                original_creator_user_id,
                entry_type
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (bundle_id, prompt_id, entry_user_id) DO NOTHING
            """,
            (req.bundle_id, req.prompt_id, entry_user_id, original_creator_user_id, entry_type),
        )
        return {"status": "added"}


@app.post("/bundles/entry")
def entry_bundle(req: BundleEntryRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        cur.execute(
            """
            SELECT id, status, genre, target_article_count
            FROM bundles
            WHERE id = %s
            FOR UPDATE
            """,
            (req.bundle_id,),
        )
        bundle = cur.fetchone()
        if not bundle:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")
        if bundle["status"] != "recruiting":
            raise HTTPException(status_code=400, detail="この福袋は募集中ではありません")

        cur.execute(
            """
            SELECT
                id,
                user_id,
                original_creator_user_id,
                category,
                review_status,
                bundle_entry_enabled,
                is_visible
            FROM prompts
            WHERE id = %s
            FOR UPDATE
            """,
            (req.prompt_id,),
        )
        prompt = cur.fetchone()
        if not prompt:
            raise HTTPException(status_code=404, detail="記事が見つかりません")
        if prompt["review_status"] != "accepted" or not prompt["is_visible"]:
            raise HTTPException(status_code=400, detail="acceptedかつ公開中の記事のみ応募できます")
        if not prompt["bundle_entry_enabled"]:
            raise HTTPException(status_code=400, detail="この記事は福袋利用不可です")

        is_own_prompt = prompt["user_id"] == user_id

        cur.execute(
            """
            SELECT 1
            FROM gacha_logs
            WHERE user_id = %s
              AND prompt_id = %s
            LIMIT 1
            """,
            (user_id, req.prompt_id),
        )
        gacha_hit = cur.fetchone()
        is_gacha_prompt = bool(gacha_hit)

        if not is_own_prompt and not is_gacha_prompt:
            raise HTTPException(status_code=403, detail="自分の投稿記事、または自分がガチャで取得した記事のみ応募できます")

        if bundle["genre"] and bundle["genre"] != "その他":
            prompt_category = prompt["category"] or "その他"
            if prompt_category != bundle["genre"]:
                raise HTTPException(status_code=400, detail="福袋ジャンルと記事カテゴリが一致しません")

        entry_type = "own" if is_own_prompt else "gacha"
        original_creator_user_id = prompt["original_creator_user_id"] or prompt["user_id"]

        cur.execute(
            """
            SELECT 1
            FROM bundle_items
            WHERE bundle_id = %s
              AND prompt_id = %s
              AND entry_user_id = %s
            LIMIT 1
            """,
            (req.bundle_id, req.prompt_id, user_id),
        )
        already = cur.fetchone()
        if already:
            raise HTTPException(status_code=400, detail="この記事は既に応募済みです")

        cur.execute(
            """
            INSERT INTO bundle_items (
                bundle_id,
                prompt_id,
                entry_user_id,
                original_creator_user_id,
                entry_type
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (req.bundle_id, req.prompt_id, user_id, original_creator_user_id, entry_type),
        )
        row = cur.fetchone()

        cur.execute(
            """
            SELECT COUNT(*) AS current_article_count
            FROM bundle_items
            WHERE bundle_id = %s
            """,
            (req.bundle_id,),
        )
        current_count = cur.fetchone()["current_article_count"]
        target_count = bundle["target_article_count"]

        return {
            "status": "ok",
            "bundle_item_id": row["id"],
            "entry_type": entry_type,
            "current_article_count": current_count,
            "target_article_count": target_count,
            "is_ready_to_publish": current_count >= target_count,
        }


@app.post("/admin/bundles/publish")
def publish_bundle(req: PublishBundleRequest, request: Request):
    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request)

        cur.execute(
            """
            SELECT id, status, target_article_count
            FROM bundles
            WHERE id = %s
            FOR UPDATE
            """,
            (req.bundle_id,),
        )
        bundle = cur.fetchone()
        if not bundle:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")
        if bundle["status"] != "recruiting":
            raise HTTPException(status_code=400, detail="募集中の福袋のみ販売開始できます")

        cur.execute(
            """
            SELECT COUNT(*) AS current_article_count
            FROM bundle_items
            WHERE bundle_id = %s
            """,
            (req.bundle_id,),
        )
        current_count = cur.fetchone()["current_article_count"]
        target_count = bundle["target_article_count"]

        if current_count < target_count:
            raise HTTPException(status_code=400, detail=f"記事数不足です（{current_count}/{target_count}）")

        cur.execute(
            """
            UPDATE bundles
            SET status = 'active',
                published_at = NOW()
            WHERE id = %s
            """,
            (req.bundle_id,),
        )
        return {"status": "published"}


@app.get("/bundles/recruiting")
def list_recruiting_bundles():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                b.id,
                b.title,
                b.description,
                b.genre,
                b.price_points,
                b.status,
                b.target_article_count,
                b.created_at,
                b.published_at,
                COUNT(bi.id) AS current_article_count
            FROM bundles b
            LEFT JOIN bundle_items bi
              ON b.id = bi.bundle_id
            WHERE b.status = 'recruiting'
            GROUP BY
                b.id,
                b.title,
                b.description,
                b.genre,
                b.price_points,
                b.status,
                b.target_article_count,
                b.created_at,
                b.published_at
            ORDER BY b.id DESC
            """
        )
        rows = cur.fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "description": row["description"],
                "genre": row["genre"],
                "price_points": row["price_points"],
                "status": row["status"],
                "target_article_count": row["target_article_count"],
                "current_article_count": row["current_article_count"],
                "remaining_article_count": max(row["target_article_count"] - row["current_article_count"], 0),
                "is_ready_to_publish": row["current_article_count"] >= row["target_article_count"],
                "created_at": row["created_at"],
                "published_at": row["published_at"],
            }
            for row in rows
        ]


@app.get("/bundles")
def list_bundles():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                b.id,
                b.title,
                b.description,
                b.genre,
                b.price_points,
                b.status,
                b.target_article_count,
                b.created_at,
                b.published_at,
                COUNT(bi.id) AS current_article_count
            FROM bundles b
            LEFT JOIN bundle_items bi
              ON b.id = bi.bundle_id
            WHERE b.status IN ('recruiting', 'active')
            GROUP BY
                b.id,
                b.title,
                b.description,
                b.genre,
                b.price_points,
                b.status,
                b.target_article_count,
                b.created_at,
                b.published_at
            ORDER BY b.id DESC
            """
        )
        rows = cur.fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "description": row["description"],
                "genre": row["genre"],
                "price_points": row["price_points"],
                "status": row["status"],
                "target_article_count": row["target_article_count"],
                "current_article_count": row["current_article_count"],
                "remaining_article_count": max(row["target_article_count"] - row["current_article_count"], 0),
                "is_ready_to_publish": row["current_article_count"] >= row["target_article_count"],
                "created_at": row["created_at"],
                "published_at": row["published_at"],
            }
            for row in rows
        ]


@app.post("/bundles/buy")
def buy_bundle(req: BuyBundleRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        cur.execute(
            """
            SELECT id, price_points, status
            FROM bundles
            WHERE id = %s
            FOR UPDATE
            """,
            (req.bundle_id,),
        )
        bundle = cur.fetchone()
        if not bundle or bundle["status"] != "active":
            raise HTTPException(status_code=404, detail="福袋なし")

        cur.execute(
            """
            SELECT points
            FROM users
            WHERE user_id = %s
            FOR UPDATE
            """,
            (user_id,),
        )
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
        if user["points"] < bundle["price_points"]:
            raise HTTPException(status_code=400, detail="ポイント不足")

        cur.execute(
            """
            INSERT INTO bundle_purchases (user_id, bundle_id, price_points)
            VALUES (%s, %s, %s)
            """,
            (user_id, req.bundle_id, bundle["price_points"]),
        )
        cur.execute(
            """
            UPDATE users
            SET points = points - %s
            WHERE user_id = %s
            """,
            (bundle["price_points"], user_id),
        )

        return {"status": "ok"}


@app.get("/bundles/{bundle_id}/progress")
def get_bundle_progress(bundle_id: int):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                b.id,
                b.title,
                b.status,
                b.genre,
                b.target_article_count,
                COUNT(bi.id) AS current_article_count
            FROM bundles b
            LEFT JOIN bundle_items bi
              ON b.id = bi.bundle_id
            WHERE b.id = %s
            GROUP BY
                b.id,
                b.title,
                b.status,
                b.genre,
                b.target_article_count
            """,
            (bundle_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")

        current_count = row["current_article_count"]
        target_count = row["target_article_count"]
        return {
            "bundle_id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "genre": row["genre"],
            "current_article_count": current_count,
            "target_article_count": target_count,
            "remaining_article_count": max(target_count - current_count, 0),
            "progress_percent": int((current_count / target_count) * 100) if target_count > 0 else 0,
            "is_ready_to_publish": current_count >= target_count,
        }


@app.get("/bundles/{bundle_id}")
def get_bundle(bundle_id: int):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                b.id,
                b.title,
                b.description,
                b.genre,
                b.price_points,
                b.status,
                b.target_article_count,
                b.created_at,
                b.published_at,
                COUNT(bi.id) AS current_article_count
            FROM bundles b
            LEFT JOIN bundle_items bi
              ON b.id = bi.bundle_id
            WHERE b.id = %s
            GROUP BY
                b.id,
                b.title,
                b.description,
                b.genre,
                b.price_points,
                b.status,
                b.target_article_count,
                b.created_at,
                b.published_at
            """,
            (bundle_id,),
        )
        bundle = cur.fetchone()
        if not bundle:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")

        return {
            "id": bundle["id"],
            "title": bundle["title"],
            "description": bundle["description"],
            "genre": bundle["genre"],
            "price_points": bundle["price_points"],
            "status": bundle["status"],
            "target_article_count": bundle["target_article_count"],
            "current_article_count": bundle["current_article_count"],
            "remaining_article_count": max(bundle["target_article_count"] - bundle["current_article_count"], 0),
            "is_ready_to_publish": bundle["current_article_count"] >= bundle["target_article_count"],
            "created_at": bundle["created_at"],
            "published_at": bundle["published_at"],
        }


@app.get("/bundles/{bundle_id}/items")
def get_bundle_items(bundle_id: int, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        cur.execute(
            """
            SELECT 1
            FROM bundle_purchases
            WHERE bundle_id = %s AND user_id = %s
            """,
            (bundle_id, user_id),
        )
        purchased = cur.fetchone()
        if not purchased:
            raise HTTPException(status_code=403, detail="未購入です")

        cur.execute(
            """
            SELECT p.id, p.title, p.content, p.category, p.url
            FROM bundle_items bi
            INNER JOIN prompts p ON bi.prompt_id = p.id
            WHERE bi.bundle_id = %s
              AND p.is_visible = TRUE
            ORDER BY bi.id ASC
            """,
            (bundle_id,),
        )
        return cur.fetchall()


@app.post("/admin/bundles/distribute")
def distribute_bundle(req: DistributeBundleRequest, request: Request):
    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request)

        cur.execute(
            """
            SELECT id
            FROM bundles
            WHERE id = %s
            FOR UPDATE
            """,
            (req.bundle_id,),
        )
        bundle = cur.fetchone()
        if not bundle:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")

        cur.execute(
            """
            SELECT COALESCE(SUM(price_points), 0) AS total_points
            FROM bundle_purchases
            WHERE bundle_id = %s
            """,
            (req.bundle_id,),
        )
        total = cur.fetchone()["total_points"]
        if total <= 0:
            return {"status": "no_sales"}

        cur.execute(
            """
            SELECT
                bi.prompt_id,
                bi.entry_user_id,
                bi.original_creator_user_id
            FROM bundle_items bi
            WHERE bi.bundle_id = %s
            ORDER BY bi.id ASC
            """,
            (req.bundle_id,),
        )
        items = cur.fetchall()
        if not items:
            raise HTTPException(status_code=400, detail="福袋に採用記事がありません")

        total_items = len(items)
        creator_pool_yen = int(total * 0.5)
        original_creator_pool_yen = int(total * 0.1)
        creator_unit_yen = int(creator_pool_yen / total_items)
        original_creator_unit_yen = int(original_creator_pool_yen / total_items)

        for row in items:
            prompt_id = row["prompt_id"]
            entry_user_id = row["entry_user_id"]
            original_creator_user_id = row["original_creator_user_id"] or row["entry_user_id"]

            cur.execute(
                """
                INSERT INTO bundle_reward_distributions (
                    bundle_id, prompt_id, creator_user_id, amount_yen, reward_type, distribution_round
                )
                VALUES (%s, %s, %s, %s, 'creator', %s)
                ON CONFLICT DO NOTHING
                """,
                (req.bundle_id, prompt_id, entry_user_id, creator_unit_yen, req.distribution_round),
            )
            if cur.rowcount > 0:
                cur.execute(
                    """
                    INSERT INTO creator_wallets (user_id, yen)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id)
                    DO UPDATE SET yen = creator_wallets.yen + %s
                    """,
                    (entry_user_id, creator_unit_yen, creator_unit_yen),
                )

            cur.execute(
                """
                INSERT INTO bundle_reward_distributions (
                    bundle_id, prompt_id, creator_user_id, amount_yen, reward_type, distribution_round
                )
                VALUES (%s, %s, %s, %s, 'original_creator', %s)
                ON CONFLICT DO NOTHING
                """,
                (req.bundle_id, prompt_id, original_creator_user_id, original_creator_unit_yen, req.distribution_round),
            )
            if cur.rowcount > 0:
                cur.execute(
                    """
                    INSERT INTO creator_wallets (user_id, yen)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id)
                    DO UPDATE SET yen = creator_wallets.yen + %s
                    """,
                    (original_creator_user_id, original_creator_unit_yen, original_creator_unit_yen),
                )

        return {"status": "distributed"}


@app.get("/admin/prompt-stop-requests")
def admin_list_prompt_stop_requests(request: Request):
    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request)
        cur.execute(
            """
            SELECT
                psr.id,
                psr.prompt_id,
                psr.user_id,
                psr.reason,
                psr.status,
                psr.created_at,
                psr.processed_at,
                p.title
            FROM prompt_stop_requests psr
            INNER JOIN prompts p ON p.id = psr.prompt_id
            ORDER BY psr.created_at DESC, psr.id DESC
            """
        )
        return cur.fetchall()


@app.patch("/admin/prompt-stop-requests/{request_id}")
def admin_process_prompt_stop_request(request_id: int, req: ProcessPromptStopRequest, request: Request):
    if req.status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="status は approved または rejected のみです")

    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request)

        cur.execute(
            """
            SELECT id, prompt_id, status
            FROM prompt_stop_requests
            WHERE id = %s
            FOR UPDATE
            """,
            (request_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="停止申請が見つかりません")
        if row["status"] != "pending":
            raise HTTPException(status_code=400, detail="この停止申請は既に処理済みです")

        cur.execute(
            """
            UPDATE prompt_stop_requests
            SET status = %s,
                processed_at = NOW()
            WHERE id = %s
            """,
            (req.status, request_id),
        )

        if req.status == "approved":
            cur.execute(
                """
                UPDATE prompts
                SET is_visible = FALSE
                WHERE id = %s
                """,
                (row["prompt_id"],),
            )

        return {"status": req.status, "request_id": request_id}


@app.get("/ranking")
def get_ranking():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                p.title,
                COUNT(g.id) AS draw_count
            FROM gacha_logs g
            INNER JOIN prompts p
                ON g.prompt_id = p.id
            WHERE p.is_visible = TRUE
            GROUP BY p.id, p.title
            ORDER BY draw_count DESC, p.id ASC
            LIMIT 20
            """
        )
        rows = cur.fetchall()
        return [{"title": row["title"], "draw_count": row["draw_count"]} for row in rows]


app.mount("/", StaticFiles(directory=".", html=True), name="static")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os
import secrets
from datetime import datetime, timedelta
from collections import defaultdict
import time

import psycopg
from psycopg.rows import dict_row
import stripe

app = FastAPI()

DATABASE_URL = os.environ["DATABASE_URL"]
STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
SITE_URL = os.environ["SITE_URL"].rstrip("/")

stripe.api_key = STRIPE_SECRET_KEY


def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # promptsテーブル（urlカラム追加）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS prompts (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        title TEXT,
        content TEXT,
        category TEXT,
        url TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        points INTEGER DEFAULT 0,
        free_gacha INTEGER DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS gacha_logs (
        id SERIAL PRIMARY KEY,
        user_id TEXT,
        prompt_id INTEGER,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS creator_wallets (
        user_id TEXT PRIMARY KEY,
        yen INTEGER DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS withdrawal_requests (
        id SERIAL PRIMARY KEY,
        user_id TEXT,
        amount INTEGER,
        status TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id SERIAL PRIMARY KEY,
        stripe_session_id TEXT UNIQUE,
        stripe_payment_intent_id TEXT,
        user_id TEXT NOT NULL,
        product_code TEXT NOT NULL,
        points_to_add INTEGER NOT NULL,
        amount_jpy INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        completed_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS withdraw_codes (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        code TEXT NOT NULL,
        expires_at TIMESTAMP NOT NULL,
        used BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """)

    # インデックス追加（パフォーマンス向上）
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prompts_created_at ON prompts (created_at DESC);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_withdraw_codes_user ON withdraw_codes (user_id);")

    conn.commit()
    cur.close()
    conn.close()


init_db()


# ====================== Pydantic Models ======================
class CreatePromptRequest(BaseModel):
    user_id: str
    title: str
    content: str
    category: str
    url: str | None = None


class GachaRequest(BaseModel):
    user_id: str
    category: str


class CreateCheckoutSessionRequest(BaseModel):
    user_id: str
    product_code: str


class WithdrawCodeRequest(BaseModel):
    user_id: str


# ====================== Rate Limit（出金コード用） ======================
withdraw_rate_limit = defaultdict(list)
RATE_LIMIT_SECONDS = 60


def get_product_config(product_code: str) -> dict:
    if product_code == "300":
        return {"points": 300, "amount_jpy": 300, "name": "300ポイント"}
    if product_code == "1000":
        return {"points": 1000, "amount_jpy": 900, "name": "1000ポイント"}
    raise HTTPException(status_code=400, detail="商品エラー")


# ====================== API Endpoints ======================

@app.post("/prompts")
def create_prompt(req: CreatePromptRequest):
    conn = get_db()
    cur = conn.cursor()

    try:
        conn.execute("BEGIN")

        cur.execute("""
            INSERT INTO prompts (user_id, title, content, category, url, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            req.user_id,
            req.title,
            req.content,
            req.category,
            req.url,
            datetime.utcnow().isoformat()
        ))

        cur.execute("""
            INSERT INTO users (user_id, free_gacha)
            VALUES (%s, 1)
            ON CONFLICT (user_id)
            DO UPDATE SET free_gacha = users.free_gacha + 1
        """, (req.user_id,))

        conn.commit()
        return {"status": "ok"}

    except Exception:
        conn.rollback()
        raise HTTPException(status_code=500, detail="投稿エラー")
    finally:
        cur.close()
        conn.close()


@app.get("/articles/latest")
def get_latest_articles(limit: int = 10):
    """最新の投稿記事を10件取得（タイトルリスト用）"""
    if limit < 1 or limit > 50:
        limit = 10

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT id, title, url, category, created_at
            FROM prompts
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))

        rows = cur.fetchall()

        return [
            {
                "id": row["id"],
                "title": row["title"],
                "url": row["url"] if row.get("url") else None,
                "category": row["category"],
                "created_at": row["created_at"]
            }
            for row in rows
        ]
    finally:
        cur.close()
        conn.close()


@app.post("/gacha/draw")
def draw_gacha(req: GachaRequest):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("SELECT * FROM users WHERE user_id = %s", (req.user_id,))
        user = cur.fetchone()

        if not user:
            cur.execute("""
                INSERT INTO users (user_id, points, free_gacha)
                VALUES (%s, 0, 0)
                ON CONFLICT (user_id) DO NOTHING
            """, (req.user_id,))
            conn.commit()
            cur.execute("SELECT * FROM users WHERE user_id = %s", (req.user_id,))
            user = cur.fetchone()

        free_gacha = user["free_gacha"]

        if free_gacha > 0:
            use_free = True
        else:
            use_free = False
            if user["points"] < 30:
                raise HTTPException(status_code=400, detail="ポイント不足")

        cur.execute("""
            SELECT * FROM prompts
            WHERE user_id != %s
            AND category = %s
        """, (req.user_id, req.category))
        prompts = cur.fetchall()

        if not prompts:
            raise HTTPException(status_code=404, detail="対象なし")

        prompt = secrets.choice(prompts) if prompts else None
        creator_id = prompt["user_id"]

        conn.execute("BEGIN")

        if use_free:
            cur.execute("UPDATE users SET free_gacha = free_gacha - 1 WHERE user_id = %s", (req.user_id,))
        else:
            cur.execute("UPDATE users SET points = points - 30 WHERE user_id = %s", (req.user_id,))

        cur.execute("""
            INSERT INTO gacha_logs (user_id, prompt_id, created_at)
            VALUES (%s, %s, %s)
        """, (req.user_id, prompt["id"], datetime.utcnow().isoformat()))

        if not use_free:
            cur.execute("""
                INSERT INTO creator_wallets (user_id, yen)
                VALUES (%s, 15)
                ON CONFLICT (user_id)
                DO UPDATE SET yen = creator_wallets.yen + 15
            """, (creator_id,))

        conn.commit()

        return {
            "result": {
                "id": prompt["id"],
                "title": prompt["title"],
                "content": prompt["content"],
                "category": prompt["category"]
            }
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise HTTPException(status_code=500, detail="処理失敗")
    finally:
        cur.close()
        conn.close()


@app.get("/gacha/ad")
def get_ad():
    ads = [
        {"text": "今だけ特別キャンペーン中！"},
        {"text": "副業・収益化に役立つ情報をチェック"},
        {"text": "記事を投稿して放置収益化"}
    ]
    return secrets.choice(ads)


@app.post("/stripe/create-checkout-session")
def create_checkout_session(req: CreateCheckoutSessionRequest):
    product = get_product_config(req.product_code)

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO users (user_id, points, free_gacha)
            VALUES (%s, 0, 0)
            ON CONFLICT (user_id) DO NOTHING
        """, (req.user_id,))
        conn.commit()

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
                            "description": f"{product['points']}pt を自動付与"
                        }
                    },
                    "quantity": 1
                }
            ],
            metadata={
                "user_id": req.user_id,
                "product_code": req.product_code,
                "points_to_add": str(product["points"]),
                "amount_jpy": str(product["amount_jpy"])
            }
        )

        cur.execute("""
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
        """, (
            session.id,
            req.user_id,
            req.product_code,
            product["points"],
            product["amount_jpy"],
            datetime.utcnow().isoformat()
        ))
        conn.commit()

        return {"checkout_url": session.url}

    except stripe.error.StripeError as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Stripeエラー: {str(e)}")
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise HTTPException(status_code=500, detail="Checkout Session作成失敗")
    finally:
        cur.close()
        conn.close()


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET
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

        conn = get_db()
        cur = conn.cursor()

        try:
            conn.execute("BEGIN")

            cur.execute("""
                SELECT * FROM payments
                WHERE stripe_session_id = %s
                FOR UPDATE
            """, (stripe_session_id,))
            payment = cur.fetchone()

            if payment and payment["status"] == "paid":
                conn.commit()
                return JSONResponse({"received": True})

            if not payment:
                cur.execute("""
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
                """, (
                    stripe_session_id,
                    stripe_payment_intent_id,
                    user_id,
                    product_code,
                    points_to_add,
                    amount_jpy,
                    datetime.utcnow().isoformat(),
                    datetime.utcnow().isoformat()
                ))

                cur.execute("""
                    INSERT INTO users (user_id, points, free_gacha)
                    VALUES (%s, %s, 0)
                    ON CONFLICT (user_id)
                    DO UPDATE SET points = users.points + %s
                """, (user_id, points_to_add, points_to_add))

                conn.commit()
                return JSONResponse({"received": True})

            cur.execute("""
                UPDATE users
                SET points = points + %s
                WHERE user_id = %s
            """, (points_to_add, user_id))

            cur.execute("""
                UPDATE payments
                SET status = 'paid',
                    stripe_payment_intent_id = %s,
                    completed_at = %s
                WHERE stripe_session_id = %s
            """, (
                stripe_payment_intent_id,
                datetime.utcnow().isoformat(),
                stripe_session_id
            ))

            conn.commit()

        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    return JSONResponse({"received": True})


@app.get("/mypage/{user_id}")
def mypage(user_id: str):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT yen
            FROM creator_wallets
            WHERE user_id = %s
        """, (user_id,))
        wallet = cur.fetchone()

        cur.execute("""
            SELECT points
            FROM users
            WHERE user_id = %s
        """, (user_id,))
        user = cur.fetchone()

        return {
            "yen": wallet["yen"] if wallet else 0,
            "points": user["points"] if user else 0
        }

    finally:
        cur.close()
        conn.close()


@app.get("/mypage/history/{user_id}")
def mypage_history(user_id: str):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("""
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
        """, (user_id,))

        rows = cur.fetchall()

        return [
            {
                "title": row["title"],
                "content": row["content"],
                "category": row["category"]
            }
            for row in rows
        ]

    finally:
        cur.close()
        conn.close()


@app.get("/ranking")
def get_ranking():
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT p.title, COUNT(g.id) AS draw_count
            FROM gacha_logs g
            INNER JOIN prompts p ON g.prompt_id = p.id
            GROUP BY p.id, p.title
            ORDER BY draw_count DESC, p.id ASC
            LIMIT 20
        """)
        rows = cur.fetchall()
        return [{"title": row["title"], "draw_count": row["draw_count"]} for row in rows]
    finally:
        cur.close()
        conn.close()


@app.post("/withdraw/code")
def create_withdraw_code(req: WithdrawCodeRequest):
    user_id = req.user_id

    # Rate Limit
    now = time.time()
    withdraw_rate_limit[user_id] = [t for t in withdraw_rate_limit[user_id] if now - t < RATE_LIMIT_SECONDS]
    if len(withdraw_rate_limit[user_id]) >= 1:
        raise HTTPException(status_code=429, detail="出金コードは1分間に1回までです。")

    withdraw_rate_limit[user_id].append(now)

    code = f"{secrets.randbelow(900000) + 100000}"
    expires = datetime.utcnow() + timedelta(minutes=10)

    conn = get_db()
    cur = conn.cursor()

    try:
        conn.execute("BEGIN")
        cur.execute("UPDATE withdraw_codes SET used = TRUE WHERE user_id = %s AND used = FALSE", (user_id,))
        cur.execute("""
            INSERT INTO withdraw_codes (user_id, code, expires_at, used)
            VALUES (%s, %s, %s, FALSE)
        """, (user_id, code, expires))
        conn.commit()
        return {"code": code, "expires_in": "10分"}
    except Exception:
        conn.rollback()
        raise HTTPException(status_code=500, detail="出金コード発行失敗")
    finally:
        cur.close()
        conn.close()


# Static Files
app.mount("/", StaticFiles(directory=".", html=True), name="static")

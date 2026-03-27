from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import secrets
import time

from fastapi import APIRouter, HTTPException, Request

from db import db_transaction
from models import (
    PromptStopRequest,
    TogglePromptFlagRequest,
    UpdatePromptRequest,
)
from utils import get_current_user_id


router = APIRouter()

withdraw_rate_limit = defaultdict(list)
RATE_LIMIT_SECONDS = 60


@router.get("/mypage/history")
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
            INNER JOIN prompts p ON g.prompt_id = p.id
            WHERE g.user_id = %s
            GROUP BY p.id, p.title, p.content, p.category
            ORDER BY viewed_at DESC
            LIMIT 50
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        return [{"title": row["title"], "content": row["content"], "category": row["category"]} for row in rows]


@router.get("/mypage/earnings")
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
            "SELECT COALESCE(SUM(entry_yen), 0) AS bundle_entry_yen FROM bundle_reward_distributions WHERE entry_user_id = %s",
            (user_id,),
        )
        bundle_entry_yen = cur.fetchone()["bundle_entry_yen"]

        cur.execute(
            "SELECT COALESCE(SUM(creator_yen), 0) AS bundle_creator_yen FROM bundle_reward_distributions WHERE original_creator_user_id = %s",
            (user_id,),
        )
        bundle_creator_yen = cur.fetchone()["bundle_creator_yen"]

        total_yen = gacha_yen + bundle_entry_yen + bundle_creator_yen
        return {
            "total_yen": total_yen,
            "gacha_yen": gacha_yen,
            "bundle_entry_yen": bundle_entry_yen,
            "bundle_creator_yen": bundle_creator_yen,
        }


@router.get("/mypage/status")
def mypage_status(request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)
        cur.execute("SELECT post_count FROM users WHERE user_id = %s", (user_id,))
        user = cur.fetchone()
        post_count = user["post_count"] if user else 0
        return {
            "post_count": post_count,
            "free_limit": 10,
            "next_cost": 0 if post_count < 10 else 100,
        }


@router.get("/mypage/bundles")
def mypage_bundles(request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        cur.execute(
            """
            SELECT
                bp.bundle_id,
                bp.price_points,
                bp.created_at AS purchased_at,
                b.title,
                b.description,
                b.genre,
                b.status,
                b.target_article_count,
                b.published_at
            FROM bundle_purchases bp
            INNER JOIN bundles b ON b.id = bp.bundle_id
            WHERE bp.user_id = %s
            ORDER BY bp.created_at DESC, bp.id DESC
            """,
            (user_id,),
        )
        rows = cur.fetchall()

        return [
            {
                "bundle_id": row["bundle_id"],
                "title": row["title"],
                "description": row["description"],
                "genre": row["genre"],
                "status": row["status"],
                "price_points": row["price_points"],
                "target_article_count": row["target_article_count"],
                "published_at": row["published_at"],
                "purchased_at": row["purchased_at"],
            }
            for row in rows
        ]

@router.post("/withdraw/request")
def create_withdraw_request(...)

@router.get("/mypage/prompts")
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


@router.get("/mypage")
def mypage(request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)
        cur.execute("SELECT yen FROM creator_wallets WHERE user_id = %s", (user_id,))
        wallet = cur.fetchone()
        cur.execute("SELECT points, locked_points, post_count FROM users WHERE user_id = %s", (user_id,))
        user = cur.fetchone()

        return {
            "user_id": user_id,
            "yen": wallet["yen"] if wallet else 0,
            "points": user["points"] if user else 0,
            "locked_points": user["locked_points"] if user else 0,
            "post_count": user["post_count"] if user else 0,
        }


@router.post("/mypage/prompts/{prompt_id}/resale-toggle")
def toggle_prompt_resale(prompt_id: int, req: TogglePromptFlagRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request, require_csrf=True)
        cur.execute("SELECT id, user_id FROM prompts WHERE id = %s FOR UPDATE", (prompt_id,))
        prompt = cur.fetchone()
        if not prompt:
            raise HTTPException(status_code=404, detail="記事が見つかりません")
        if prompt["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="自分の記事のみ変更できます")

        cur.execute("UPDATE prompts SET resale_offer_enabled = %s WHERE id = %s", (req.enabled, prompt_id))
        return {"status": "ok", "prompt_id": prompt_id, "resale_offer_enabled": req.enabled}


@router.post("/mypage/prompts/{prompt_id}/bundle-toggle")
def toggle_prompt_bundle(prompt_id: int, req: TogglePromptFlagRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request, require_csrf=True)
        cur.execute("SELECT id, user_id FROM prompts WHERE id = %s FOR UPDATE", (prompt_id,))
        prompt = cur.fetchone()
        if not prompt:
            raise HTTPException(status_code=404, detail="記事が見つかりません")
        if prompt["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="自分の記事のみ変更できます")

        cur.execute("UPDATE prompts SET bundle_entry_enabled = %s WHERE id = %s", (req.enabled, prompt_id))
        return {"status": "ok", "prompt_id": prompt_id, "bundle_entry_enabled": req.enabled}


@router.patch("/mypage/prompts/{prompt_id}")
def update_my_prompt(prompt_id: int, req: UpdatePromptRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request, require_csrf=True)

        cur.execute(
            """
            SELECT id, user_id, title, content, category, url
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
            raise HTTPException(status_code=403, detail="自分の記事のみ更新できます")

        new_title = req.title if req.title is not None else prompt["title"]
        new_content = req.content if req.content is not None else prompt["content"]
        new_category = req.category if req.category is not None else prompt["category"]
        new_url = req.url if req.url is not None else prompt["url"]

        cur.execute(
            """
            UPDATE prompts
            SET title = %s,
                content = %s,
                category = %s,
                url = %s
            WHERE id = %s
            """,
            (new_title, new_content, new_category, new_url, prompt_id),
        )

        return {
            "status": "ok",
            "prompt_id": prompt_id,
        }


@router.post("/mypage/prompts/{prompt_id}/stop-request")
def create_prompt_stop_request(prompt_id: int, req: PromptStopRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request, require_csrf=True)
        cur.execute("SELECT id, user_id FROM prompts WHERE id = %s FOR UPDATE", (prompt_id,))
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
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="掲載停止申請は受付中です")

        cur.execute(
            """
            INSERT INTO prompt_stop_requests (prompt_id, user_id, reason, status)
            VALUES (%s, %s, %s, 'pending')
            RETURNING id
            """,
            (prompt_id, user_id, req.reason),
        )
        row = cur.fetchone()
        return {"status": "pending", "request_id": row["id"], "prompt_id": prompt_id}

@router.post("/withdraw/request")
def create_withdraw_request(req: CreateWithdrawalRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request, require_csrf=True)

        if req.amount_yen < 1000:
            raise HTTPException(status_code=400, detail="出金申請は1000円以上です")
        if req.method not in ("paypay", "amazon_gift"):
            raise HTTPException(status_code=400, detail="送金方法エラー")

        cur.execute("SELECT yen FROM creator_wallets WHERE user_id = %s FOR UPDATE", (user_id,))
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

        cur.execute("UPDATE creator_wallets SET yen = yen - %s WHERE user_id = %s", (req.amount_yen, user_id))
        cur.execute("UPDATE withdraw_codes SET used = TRUE WHERE id = %s", (code_row["id"],))
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

@router.post("/withdraw/code")
def create_withdraw_code(request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request, require_csrf=True)

        now = time.time()
        withdraw_rate_limit[user_id] = [t for t in withdraw_rate_limit[user_id] if now - t < RATE_LIMIT_SECONDS]
        if len(withdraw_rate_limit[user_id]) >= 1:
            raise HTTPException(status_code=429, detail="出金コードは1分間に1回までです")
        withdraw_rate_limit[user_id].append(now)

        code = f"{secrets.randbelow(900000) + 100000}"
        expires = datetime.utcnow() + timedelta(minutes=10)

        cur.execute("UPDATE withdraw_codes SET used = TRUE WHERE user_id = %s AND used = FALSE", (user_id,))
        cur.execute(
            """
            INSERT INTO withdraw_codes (user_id, code, expires_at, used)
            VALUES (%s, %s, %s, FALSE)
            """,
            (user_id, code, expires),
        )
        return {"code": code, "expires_in": "10分"}

"""
mypage.py — マイページAPI（収益・投稿管理・出金フロー）

仕様書準拠：
- 自分のデータのみアクセス可能（所有者チェック）
- 書き込み系はCSRF必須
- 出金コードは1分間に1回制限・10分有効
- 投稿停止申請の二重防止
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import secrets
import time

from fastapi import APIRouter, HTTPException, Request

from db import db_transaction
from dependencies import get_current_user_id_dep
from models import (
    CreateWithdrawalRequest,
    PromptStopRequest,
    TogglePromptFlagRequest,
    UpdatePromptRequest,
)

router = APIRouter(prefix="/mypage", tags=["mypage"])

# 出金コード生成レート制限（インメモリ・簡易版）
withdraw_rate_limit = defaultdict(list)
RATE_LIMIT_SECONDS = 60


@router.get("")
def mypage(request: Request):
    """マイページ基本情報"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id_dep(request, conn)

        cur.execute(
            "SELECT COALESCE(yen, 0) AS yen FROM creator_wallets WHERE user_id = %s",
            (user_id,),
        )
        wallet = cur.fetchone()

        cur.execute(
            "SELECT points, locked_points, post_count FROM users WHERE user_id = %s",
            (user_id,),
        )
        user = cur.fetchone() or {}

        return {
            "status": "ok",
            "user_id": user_id,
            "yen": wallet["yen"] if wallet else 0,
            "points": user.get("points", 0),
            "locked_points": user.get("locked_points", 0),
            "post_count": user.get("post_count", 0),
        }


@router.get("/status")
def mypage_status(request: Request):
    """投稿可能状況"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id_dep(request, conn)
        cur.execute("SELECT post_count FROM users WHERE user_id = %s", (user_id,))
        user = cur.fetchone() or {}

        post_count = user.get("post_count", 0)

        return {
            "status": "ok",
            "post_count": post_count,
            "free_limit": 10,
            "next_cost": 0 if post_count < 10 else 100,
        }


@router.get("/history")
def mypage_history(request: Request):
    """ガチャ取得履歴（最大50件）"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id_dep(request, conn)

        cur.execute(
            """
            SELECT
                p.title,
                p.category,
                MAX(g.created_at) AS viewed_at
            FROM gacha_logs g
            INNER JOIN prompts p ON g.prompt_id = p.id
            WHERE g.user_id = %s
            GROUP BY p.id, p.title, p.category
            ORDER BY viewed_at DESC
            LIMIT 50
            """,
            (user_id,),
        )
        rows = cur.fetchall()

        return {
            "status": "ok",
            "history": [
                {
                    "title": row["title"],
                    "category": row["category"],
                    "viewed_at": row["viewed_at"],
                }
                for row in rows
            ],
        }


@router.get("/earnings")
def mypage_earnings(request: Request):
    """収益集計"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id_dep(request, conn)

        # ガチャ報酬（有料ガチャのみ15円）
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

        # 福袋報酬
        cur.execute(
            "SELECT COALESCE(SUM(entry_yen), 0) AS bundle_entry_yen "
            "FROM bundle_reward_distributions WHERE entry_user_id = %s",
            (user_id,),
        )
        bundle_entry_yen = cur.fetchone()["bundle_entry_yen"]

        cur.execute(
            "SELECT COALESCE(SUM(creator_yen), 0) AS bundle_creator_yen "
            "FROM bundle_reward_distributions WHERE original_creator_user_id = %s",
            (user_id,),
        )
        bundle_creator_yen = cur.fetchone()["bundle_creator_yen"]

        total_yen = gacha_yen + bundle_entry_yen + bundle_creator_yen

        return {
            "status": "ok",
            "total_yen": total_yen,
            "gacha_yen": gacha_yen,
            "bundle_entry_yen": bundle_entry_yen,
            "bundle_creator_yen": bundle_creator_yen,
        }


@router.get("/bundles")
def mypage_bundles(request: Request):
    """購入済み福袋一覧"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id_dep(request, conn)

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
            ORDER BY bp.created_at DESC
            """,
            (user_id,),
        )
        rows = cur.fetchall()

        return {
            "status": "ok",
            "bundles": [
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
            ],
        }


@router.get("/prompts")
def mypage_prompts(request: Request):
    """自分の投稿記事一覧（管理情報付き）"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id_dep(request, conn)

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
                    SELECT 1 FROM prompt_stop_requests psr
                    WHERE psr.prompt_id = p.id
                      AND psr.user_id = %s
                      AND psr.status = 'pending'
                ) AS has_pending_stop_request
            FROM prompts p
            WHERE p.user_id = %s
            ORDER BY p.created_at DESC, p.id DESC
            """,
            (user_id, user_id),
        )
        rows = cur.fetchall()

        return {
            "status": "ok",
            "prompts": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "category": row["category"],
                    "url": row["url"],
                    "created_at": row["created_at"],
                    "resale_offer_enabled": row["resale_offer_enabled"],
                    "bundle_entry_enabled": row["bundle_entry_enabled"],
                    "is_visible": row["is_visible"],
                    "has_pending_stop_request": bool(row["has_pending_stop_request"]),
                }
                for row in rows
            ],
        }


# ─────────────────────────────────────────────
# 投稿管理（書き込み系：CSRF必須）
# ─────────────────────────────────────────────

@router.post("/prompts/{prompt_id}/resale-toggle")
def toggle_prompt_resale(prompt_id: int, req: TogglePromptFlagRequest, request: Request):
    """再販売オファー許可のON/OFF"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id_dep(request, conn, require_csrf=True)

        cur.execute(
            "SELECT id, user_id FROM prompts WHERE id = %s FOR UPDATE",
            (prompt_id,),
        )
        prompt = cur.fetchone()
        if not prompt or prompt["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="自分の記事のみ変更できます")

        cur.execute(
            "UPDATE prompts SET resale_offer_enabled = %s WHERE id = %s",
            (req.enabled, prompt_id),
        )

        return {
            "status": "ok",
            "prompt_id": prompt_id,
            "resale_offer_enabled": req.enabled,
            "message": f"再販売オファーを {'許可' if req.enabled else '停止'} にしました",
        }


@router.post("/prompts/{prompt_id}/bundle-toggle")
def toggle_prompt_bundle(prompt_id: int, req: TogglePromptFlagRequest, request: Request):
    """福袋利用許可のON/OFF"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id_dep(request, conn, require_csrf=True)

        cur.execute(
            "SELECT id, user_id FROM prompts WHERE id = %s FOR UPDATE",
            (prompt_id,),
        )
        prompt = cur.fetchone()
        if not prompt or prompt["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="自分の記事のみ変更できます")

        cur.execute(
            "UPDATE prompts SET bundle_entry_enabled = %s WHERE id = %s",
            (req.enabled, prompt_id),
        )

        return {
            "status": "ok",
            "prompt_id": prompt_id,
            "bundle_entry_enabled": req.enabled,
            "message": f"福袋利用を {'許可' if req.enabled else '停止'} にしました",
        }


@router.patch("/prompts/{prompt_id}")
def update_my_prompt(prompt_id: int, req: UpdatePromptRequest, request: Request):
    """自分の記事を更新"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id_dep(request, conn, require_csrf=True)

        cur.execute(
            "SELECT id, user_id FROM prompts WHERE id = %s FOR UPDATE",
            (prompt_id,),
        )
        prompt = cur.fetchone()
        if not prompt or prompt["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="自分の記事のみ更新できます")

        cur.execute(
            """
            UPDATE prompts
            SET title = COALESCE(%s, title),
                content = COALESCE(%s, content),
                category = COALESCE(%s, category),
                url = COALESCE(%s, url)
            WHERE id = %s
            """,
            (req.title, req.content, req.category, req.url, prompt_id),
        )

        return {"status": "ok", "prompt_id": prompt_id, "message": "記事を更新しました"}


@router.post("/prompts/{prompt_id}/stop-request")
def create_prompt_stop_request(prompt_id: int, req: PromptStopRequest, request: Request):
    """掲載停止申請"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id_dep(request, conn, require_csrf=True)

        cur.execute(
            "SELECT id, user_id FROM prompts WHERE id = %s FOR UPDATE",
            (prompt_id,),
        )
        prompt = cur.fetchone()
        if not prompt or prompt["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="自分の記事のみ申請できます")

        # 二重申請防止
        cur.execute(
            """
            SELECT 1 FROM prompt_stop_requests
            WHERE prompt_id = %s AND user_id = %s AND status = 'pending'
            """,
            (prompt_id, user_id),
        )
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="既に掲載停止申請が受付中です")

        cur.execute(
            """
            INSERT INTO prompt_stop_requests (prompt_id, user_id, reason, status)
            VALUES (%s, %s, %s, 'pending')
            RETURNING id
            """,
            (prompt_id, user_id, req.reason),
        )
        row = cur.fetchone()

        return {
            "status": "ok",
            "message": "掲載停止申請を受け付けました",
            "request_id": row["id"],
            "prompt_id": prompt_id,
        }


# ─────────────────────────────────────────────
# 出金機能
# ─────────────────────────────────────────────

@router.post("/withdraw/code")
def create_withdraw_code(request: Request):
    """出金コード発行（10分有効・1分間に1回制限）"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id_dep(request, conn, require_csrf=True)

        # レート制限チェック
        now = time.time()
        withdraw_rate_limit[user_id] = [
            t for t in withdraw_rate_limit[user_id] if now - t < RATE_LIMIT_SECONDS
        ]
        if len(withdraw_rate_limit[user_id]) >= 1:
            raise HTTPException(
                status_code=429,
                detail="出金コードは1分間に1回のみ発行可能です"
            )
        withdraw_rate_limit[user_id].append(now)

        # 古い未使用コードを無効化
        cur.execute(
            "UPDATE withdraw_codes SET used = TRUE WHERE user_id = %s AND used = FALSE",
            (user_id,),
        )

        code = f"{secrets.randbelow(900000) + 100000}"  # 6桁数字
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

        cur.execute(
            """
            INSERT INTO withdraw_codes (user_id, code, expires_at, used)
            VALUES (%s, %s, %s, FALSE)
            """,
            (user_id, code, expires_at),
        )

        return {
            "status": "ok",
            "code": code,
            "expires_in": "10分",
            "message": "出金コードを発行しました",
        }


@router.post("/withdraw/request")
def create_withdraw_request(req: CreateWithdrawalRequest, request: Request):
    """出金申請（コード検証 + 残高即時減算）"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id_dep(request, conn, require_csrf=True)

        # 残高確認
        cur.execute(
            "SELECT yen FROM creator_wallets WHERE user_id = %s FOR UPDATE",
            (user_id,),
        )
        wallet = cur.fetchone()
        if not wallet or wallet["yen"] < req.amount_yen:
            raise HTTPException(
                status_code=400,
                detail=f"残高が不足しています（残高: {wallet['yen'] if wallet else 0}円）"
            )

        # 出金コード検証
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
            raise HTTPException(status_code=400, detail="この出金コードは既に使用されています")
        if code_row["expires_at"] < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="出金コードの有効期限が切れています")

        # 残高はadminによる承認時に減算（admin.py側で処理）
        # コード使用済み + 申請登録
        cur.execute(
            "UPDATE withdraw_codes SET used = TRUE WHERE id = %s",
            (code_row["id"],),
        )

        cur.execute(
            """
            INSERT INTO withdrawal_requests (
                user_id, amount_yen, method, destination, withdraw_code, status
            )
            VALUES (%s, %s, %s, %s, %s, 'pending')
            RETURNING id
            """,
            (
                user_id,
                req.amount_yen,
                req.method,
                req.destination,
                req.withdraw_code,
            ),
        )
        row = cur.fetchone()

        return {
            "status": "ok",
            "message": "出金申請を受け付けました",
            "request_id": row["id"],
            "amount_yen": req.amount_yen,
}

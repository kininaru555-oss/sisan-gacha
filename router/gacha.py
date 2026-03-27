from __future__ import annotations

from collections import defaultdict
import time

from fastapi import APIRouter, HTTPException, Request

from db import db_transaction
from models import GachaRequest
from utils import ensure_user_row_exists, get_current_user_id, now_iso


router = APIRouter()

# ─────────────────────────────────────────────
# ガチャ実行レート制限（簡易）
# - 1ユーザーあたり短時間連打を抑制
# - 将来は Redis 等へ移行推奨
# ─────────────────────────────────────────────
gacha_rate_limit = defaultdict(list)
GACHA_RATE_LIMIT_SECONDS = 10
GACHA_RATE_LIMIT_MAX_CALLS = 5


def _enforce_gacha_rate_limit(user_id: str) -> None:
    now = time.time()
    gacha_rate_limit[user_id] = [
        t for t in gacha_rate_limit[user_id]
        if now - t < GACHA_RATE_LIMIT_SECONDS
    ]
    if len(gacha_rate_limit[user_id]) >= GACHA_RATE_LIMIT_MAX_CALLS:
        raise HTTPException(
            status_code=429,
            detail="ガチャの実行回数が多すぎます。少し待ってから再実行してください",
        )
    gacha_rate_limit[user_id].append(now)


@router.post("/gacha/draw")
def draw_gacha(req: GachaRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request, require_csrf=True)
        ensure_user_row_exists(cur, user_id)

        _enforce_gacha_rate_limit(user_id)

        cur.execute(
            """
            SELECT
                user_id,
                points,
                free_gacha,
                locked_points,
                post_count,
                role,
                token_version,
                is_active
            FROM users
            WHERE user_id = %s
            FOR UPDATE
            """,
            (user_id,),
        )
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
        if not user.get("is_active", True):
            raise HTTPException(status_code=403, detail="このアカウントは利用停止中です")

        use_free = user["free_gacha"] > 0
        if not use_free and user["points"] < 30:
            raise HTTPException(status_code=400, detail="ポイント不足")

        cur.execute(
            """
            SELECT
                id,
                user_id,
                title,
                content,
                category,
                url,
                review_status,
                is_visible
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

        if prompt["review_status"] != "accepted" or not prompt["is_visible"]:
            raise HTTPException(status_code=400, detail="抽選対象が不正です")

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
            cost_type = "free"
            cost_points = 0
        else:
            cur.execute(
                """
                UPDATE users
                SET points = points - 30
                WHERE user_id = %s
                """
                ,
                (user_id,),
            )
            cost_type = "paid"
            cost_points = 30

        draw_time = now_iso()

        cur.execute(
            """
            INSERT INTO gacha_logs (user_id, prompt_id, created_at)
            VALUES (%s, %s, %s)
            """,
            (user_id, prompt["id"], draw_time),
        )

        # 自分の投稿を自分で引いた場合は報酬を付与しない
        if not use_free and creator_id != user_id:
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
            },
            "meta": {
                "drawn_at": draw_time,
                "cost_type": cost_type,
                "cost_points": cost_points,
                "creator_reward_yen": 0 if use_free or creator_id == user_id else 15,
            },
        }


@router.get("/gacha/ad")
def get_ad():
    ads = [
        {"text": "今だけ特別キャンペーン中！"},
        {"text": "副業・収益化に役立つ情報をチェック"},
        {"text": "記事を投稿して放置収益化"},
    ]
    return ads[int(time.time()) % len(ads)]

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from db import db_transaction
from models import GachaRequest
from utils import ensure_user_row_exists, get_current_user_id, now_iso


router = APIRouter()


@router.post("/gacha/draw")
def draw_gacha(req: GachaRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request, require_csrf=True)
        ensure_user_row_exists(cur, user_id)

        cur.execute("SELECT * FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
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
                "UPDATE users SET free_gacha = free_gacha - 1 WHERE user_id = %s",
                (user_id,),
            )
        else:
            cur.execute(
                "UPDATE users SET points = points - 30 WHERE user_id = %s",
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


@router.get("/gacha/ad")
def get_ad():
    ads = [
        {"text": "今だけ特別キャンペーン中！"},
        {"text": "副業・収益化に役立つ情報をチェック"},
        {"text": "記事を投稿して放置収益化"},
    ]
    return ads[int(time.time()) % len(ads)]

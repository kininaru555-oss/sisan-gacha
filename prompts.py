from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from db import db_cursor, db_transaction
from models import CreatePromptRequest
from utils import ensure_user_row_exists, get_current_user_id, now_iso


router = APIRouter()


@router.post("/prompts")
def create_prompt(req: CreatePromptRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request, require_csrf=True)
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

        if user["post_count"] >= 10:
            if user["points"] < 100:
                raise HTTPException(status_code=400, detail="11記事目以降の投稿には100pt必要です")
            cur.execute(
                "UPDATE users SET points = points - 100 WHERE user_id = %s",
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
            (user_id, user_id, req.title, req.content, req.category, req.url, now_iso()),
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


@router.get("/articles/latest")
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


@router.get("/ranking")
def get_ranking():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                p.title,
                COUNT(g.id) AS draw_count
            FROM gacha_logs g
            INNER JOIN prompts p ON g.prompt_id = p.id
            WHERE p.is_visible = TRUE
            GROUP BY p.id, p.title
            ORDER BY draw_count DESC, p.id ASC
            LIMIT 20
            """
        )
        rows = cur.fetchall()
        return [{"title": row["title"], "draw_count": row["draw_count"]} for row in rows]

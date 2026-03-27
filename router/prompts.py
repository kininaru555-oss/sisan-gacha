"""
prompts.py — 記事投稿・新着記事・ランキングAPI

仕様書準拠：
- 投稿は即時承認（review_status = 'accepted'）
- 1〜10件目：無料投稿
- 11件目以降：100pt消費
- 投稿成功時に free_gacha +1、post_count +1
- CSRF保護必須
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from db import db_cursor, db_transaction
from dependencies import get_current_user_id_dep   # ← dependencies.pyを使用
from models import CreatePromptRequest
from utils import ensure_user_row_exists, now_iso

router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.post("")
def create_prompt(req: CreatePromptRequest, request: Request):
    """
    記事（プロンプト）投稿
    - CSRF必須
    - 投稿制限（10件まで無料、以降100pt）
    - 投稿成功で無料ガチャチケット +1
    """
    with db_transaction() as (conn, cur):
        # 認証 + CSRF検証
        user_id = get_current_user_id_dep(
            request=request,
            conn=conn,
            require_csrf=True
        )

        ensure_user_row_exists(cur, user_id)

        if not req.bundle_consent:
            raise HTTPException(
                status_code=400,
                detail="福袋利用規約に同意してください"
            )

        # ユーザー情報取得（FOR UPDATE でロック）
        cur.execute(
            """
            SELECT post_count, points, is_active
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

        # 投稿制限チェック & ポイント消費
        cost_points = 0
        if user["post_count"] >= 10:
            if user["points"] < 100:
                raise HTTPException(
                    status_code=400,
                    detail="11記事目以降の投稿には100ポイント必要です"
                )
            cost_points = 100
            cur.execute(
                "UPDATE users SET points = points - 100 WHERE user_id = %s",
                (user_id,),
            )

        # XSS対策：本番では bleach や html.escape を使用推奨
        # title = html.escape(req.title)
        # content = html.escape(req.content)  # または markdown処理

        # プロンプト登録（即時承認）
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
            RETURNING id
            """,
            (
                user_id,
                user_id,                    # original_creator_user_id
                req.title,
                req.content,
                req.category,
                req.url,
                now_iso(),
            ),
        )
        prompt_id = cur.fetchone()["id"]

        # ユーザー更新（無料ガチャ付与 + 投稿数カウント）
        cur.execute(
            """
            UPDATE users
            SET free_gacha = free_gacha + 1,
                post_count = post_count + 1
            WHERE user_id = %s
            """,
            (user_id,),
        )

        return {
            "status": "ok",
            "message": "記事を投稿しました",
            "prompt_id": prompt_id,
            "free_gacha_added": 1,
            "points_consumed": cost_points,
        }


@router.get("/articles/latest")
def get_latest_articles(limit: int = Query(default=10, ge=1, le=100)):
    """新着記事一覧（公開済みのみ）"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                id,
                title,
                url,
                category,
                created_at
            FROM prompts
            WHERE review_status = 'accepted'
              AND is_visible = TRUE
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

        return {
            "status": "ok",
            "articles": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "url": row["url"] if row.get("url") else None,
                    "category": row["category"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }


@router.get("/ranking")
def get_ranking(limit: int = Query(default=20, ge=1, le=50)):
    """ガチャ人気ランキング"""
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
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

        return {
            "status": "ok",
            "ranking": [
                {
                    "title": row["title"],
                    "draw_count": row["draw_count"],
                }
                for row in rows
            ]
}

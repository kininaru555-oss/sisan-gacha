"""
prompts.py — 記事投稿・新着記事・ランキングAPI

仕様書準拠（v8.0）：
- 投稿は即時承認（review_status = 'accepted'）
- 1〜10件目：無料投稿 / 11件目以降：100pt消費
- 投稿成功時に free_gacha +1、post_count +1
- 福袋利用規約同意は必須（bundle_consent = True）
- CSRF保護必須
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from db import db_cursor, db_transaction
from dependencies import get_current_user_id_dep
from models import CreatePromptRequest
from datetime import datetime, timezone

from utils import ensure_user_row_exists

router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.post("")
def create_prompt(req: CreatePromptRequest, request: Request):
    """
    記事投稿エンドポイント
    - CSRF必須
    - 投稿制限（1〜10件無料、以後100pt）
    - 福袋利用規約同意必須
    - 成功時：free_gacha +1、post_count +1
    """
    with db_transaction() as (conn, cur):
        # 認証 + CSRF検証
        user_id = get_current_user_id_dep(
            request=request,
            conn=conn,
            require_csrf=True
        )

        ensure_user_row_exists(cur, user_id)

        # 福袋利用規約同意チェック（設計書必須）
        if not req.bundle_consent:
            raise HTTPException(
                status_code=400,
                detail="福袋利用規約に同意する必要があります"
            )

        # ユーザー情報取得（ロック）
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

        # 投稿コスト計算
        cost_points = 0
        if user["post_count"] >= 10:
            if user["points"] < 100:
                raise HTTPException(
                    status_code=400,
                    detail="11記事目以降の投稿には100ポイント必要です。現在のポイント: " + str(user["points"])
                )
            cost_points = 100
            cur.execute(
                "UPDATE users SET points = points - 100 WHERE user_id = %s",
                (user_id,),
            )

        # 記事登録（即時承認）
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
                user_id,                    # original_creator_user_id = 投稿者本人
                req.title,
                req.content,
                req.category,
                req.url,
                datetime.now(timezone.utc),
            ),
        )
        prompt_id = cur.fetchone()["id"]

        # ユーザー更新（無料ガチャ付与 + 投稿数増加）
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
            "message": "記事を投稿しました。無料ガチャチケットを1回付与しました。",
            "prompt_id": prompt_id,
            "free_gacha_added": 1,
            "points_consumed": cost_points,
            "next_post_cost": 100 if (user["post_count"] + 1) >= 10 else 0,
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
                category,
                url,
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
                    "category": row["category"],
                    "url": row["url"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }


@router.get("/ranking")
def get_ranking(limit: int = Query(default=20, ge=1, le=50)):
    """人気記事ランキング（ガチャ抽選回数ベース・上位20件）"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                p.id,
                p.title,
                p.category,
                COUNT(g.id) AS draw_count
            FROM gacha_logs g
            INNER JOIN prompts p ON g.prompt_id = p.id
            WHERE p.is_visible = TRUE
            GROUP BY p.id, p.title, p.category
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
                    "id": row["id"],
                    "title": row["title"],
                    "category": row["category"],
                    "draw_count": row["draw_count"],
                }
                for row in rows
            ]
                }

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from db import db_transaction
from utils import get_current_user_id


router = APIRouter()


@router.get("/bundles/{bundle_id}/entry-candidates")
def get_bundle_entry_candidates(bundle_id: int, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        cur.execute(
            """
            SELECT id, genre, status
            FROM bundles
            WHERE id = %s
            """,
            (bundle_id,),
        )
        bundle = cur.fetchone()
        if not bundle:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")
        if bundle["status"] != "recruiting":
            raise HTTPException(status_code=400, detail="この福袋は募集中ではありません")

        cur.execute(
            """
            SELECT
                p.id AS prompt_id,
                p.title,
                p.category,
                CASE
                    WHEN p.user_id = %s THEN 'own'
                    ELSE 'gacha'
                END AS entry_type
            FROM prompts p
            WHERE p.review_status = 'accepted'
              AND p.is_visible = TRUE
              AND p.bundle_entry_enabled = TRUE
              AND (
                    p.user_id = %s
                    OR EXISTS (
                        SELECT 1
                        FROM gacha_logs g
                        WHERE g.user_id = %s
                          AND g.prompt_id = p.id
                    )
                  )
              AND NOT EXISTS (
                    SELECT 1
                    FROM bundle_items bi
                    WHERE bi.bundle_id = %s
                      AND bi.prompt_id = p.id
                      AND bi.entry_user_id = %s
                  )
              AND (
                    %s = 'その他'
                    OR COALESCE(p.category, 'その他') = %s
                  )
            ORDER BY p.id DESC
            """,
            (user_id, user_id, user_id, bundle_id, user_id, bundle["genre"], bundle["genre"]),
        )
        rows = cur.fetchall()

        return [
            {
                "prompt_id": row["prompt_id"],
                "title": row["title"],
                "category": row["category"],
                "entry_type": row["entry_type"],
            }
            for row in rows
        ]


def build_content_preview(text: Optional[str], limit: int = 120) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    normalized = " ".join(raw.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "..."


def resolve_author_name(prompt_row) -> str:
    """
    author_name 表示用。
    現状のDBでは prompts.user_id / prompts.original_creator_user_id は見えているので、
    安全に使える範囲で user_id ベース名を返す。
    将来 users.display_name 等があるならここで差し替える。
    """
    if prompt_row.get("original_creator_user_id"):
        return prompt_row["original_creator_user_id"]
    if prompt_row.get("user_id"):
        return prompt_row["user_id"]
    return "不明"


@router.get("/bundles/{bundle_id}/preview")
def get_bundle_preview(bundle_id: int, request: Request):
    """
    未購入ユーザー向けの安全な中身プレビュー。
    - 本文全文は返さない
    - URL本体は返さない
    - active / recruiting のみ公開
    """
    with db_transaction() as (conn, cur):
        cur.execute(
            """
            SELECT
                id,
                title,
                description,
                genre,
                price_points,
                status,
                target_article_count
            FROM bundles
            WHERE id = %s
            """,
            (bundle_id,),
        )
        bundle = cur.fetchone()
        if not bundle:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")

        if bundle["status"] not in ("recruiting", "active"):
            raise HTTPException(status_code=400, detail="この福袋はプレビュー対象外です")

        cur.execute(
            """
            SELECT
                p.id AS prompt_id,
                p.title,
                p.category,
                p.content,
                p.url,
                p.user_id,
                p.original_creator_user_id,
                bi.entry_user_id,
                bi.entry_type
            FROM bundle_items bi
            INNER JOIN prompts p
                ON p.id = bi.prompt_id
            WHERE bi.bundle_id = %s
            ORDER BY bi.id DESC
            LIMIT 12
            """,
            (bundle_id,),
        )
        rows = cur.fetchall()

        author_ids = set()
        items = []

        for row in rows:
            author_name = resolve_author_name(row)
            author_ids.add(author_name)

            items.append(
                {
                    "prompt_id": row["prompt_id"],
                    "title": row["title"],
                    "category": row["category"] or "その他",
                    "author_name": author_name,
                    "entry_type": row["entry_type"],
                    "content_preview": build_content_preview(row["content"], 120),
                    "has_url": bool(row["url"]),
                }
            )

        return {
            "bundle_id": bundle["id"],
            "bundle_title": bundle["title"],
            "status": bundle["status"],
            "title_count": len(items),
            "author_count": len(author_ids),
            "items": items,
        }


@router.get("/bundles/{bundle_id}/entry-candidates/detail")
def get_bundle_entry_candidates_detail(bundle_id: int, request: Request):
    """
    既存 /bundles/{bundle_id}/entry-candidates の詳細版。
    応募判断に必要な補足情報を返す。
    """
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        cur.execute(
            """
            SELECT id, genre, status
            FROM bundles
            WHERE id = %s
            """,
            (bundle_id,),
        )
        bundle = cur.fetchone()
        if not bundle:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")
        if bundle["status"] != "recruiting":
            raise HTTPException(status_code=400, detail="この福袋は募集中ではありません")

        cur.execute(
            """
            SELECT
                p.id AS prompt_id,
                p.title,
                p.category,
                p.content,
                p.url,
                p.user_id,
                p.original_creator_user_id,
                CASE
                    WHEN p.user_id = %s THEN 'own'
                    ELSE 'gacha'
                END AS entry_type,
                EXISTS (
                    SELECT 1
                    FROM bundle_items bi
                    WHERE bi.bundle_id = %s
                      AND bi.prompt_id = p.id
                      AND bi.entry_user_id = %s
                ) AS is_already_entered
            FROM prompts p
            WHERE p.review_status = 'accepted'
              AND p.is_visible = TRUE
              AND p.bundle_entry_enabled = TRUE
              AND (
                    p.user_id = %s
                    OR EXISTS (
                        SELECT 1
                        FROM gacha_logs g
                        WHERE g.user_id = %s
                          AND g.prompt_id = p.id
                    )
                  )
              AND (
                    %s = 'その他'
                    OR COALESCE(p.category, 'その他') = %s
                  )
            ORDER BY p.id DESC
            """,
            (
                user_id,
                bundle_id,
                user_id,
                user_id,
                user_id,
                bundle["genre"],
                bundle["genre"],
            ),
        )
        rows = cur.fetchall()

        return {
            "bundle_id": bundle_id,
            "items": [
                {
                    "prompt_id": row["prompt_id"],
                    "title": row["title"],
                    "category": row["category"] or "その他",
                    "entry_type": row["entry_type"],
                    "content_preview": build_content_preview(row["content"], 120),
                    "has_url": bool(row["url"]),
                    "author_name": resolve_author_name(row),
                    "is_already_entered": bool(row["is_already_entered"]),
                }
                for row in rows
            ],
  }

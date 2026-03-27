from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from db import db_cursor, db_transaction, get_db
from models import BuyBundleRequest, BundleEntryRequest
from security import get_current_user
from utils import get_current_user_id


router = APIRouter()


# ======================
# ヘルパー関数
# ======================

def build_content_preview(text: Optional[str], limit: int = 120) -> str:
    """コンテンツのプレビューを作成（長すぎる場合は省略）"""
    raw = (text or "").strip()
    if not raw:
        return ""
    normalized = " ".join(raw.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "..."


def resolve_author_name(prompt_row) -> str:
    """著作者名の解決（original_creator_user_id優先）"""
    if prompt_row.get("original_creator_user_id"):
        return prompt_row["original_creator_user_id"]
    if prompt_row.get("user_id"):
        return prompt_row["user_id"]
    return "不明"


# ======================
# エンドポイント
# ======================

@router.get("/bundles/{bundle_id}/entry-candidates")
def get_bundle_entry_candidates(bundle_id: int, request: Request):
    """応募可能な記事一覧（簡易版）"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        # 福袋の存在と状態チェック
        cur.execute(
            "SELECT id, genre, status FROM bundles WHERE id = %s",
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
                CASE WHEN p.user_id = %s THEN 'own' ELSE 'gacha' END AS entry_type
            FROM prompts p
            WHERE p.review_status = 'accepted'
              AND p.is_visible = TRUE
              AND p.bundle_entry_enabled = TRUE
              AND (
                    p.user_id = %s
                    OR EXISTS (
                        SELECT 1 FROM gacha_logs g 
                        WHERE g.user_id = %s AND g.prompt_id = p.id
                    )
                  )
              AND NOT EXISTS (
                    SELECT 1 FROM bundle_items bi 
                    WHERE bi.bundle_id = %s 
                      AND bi.prompt_id = p.id 
                      AND bi.entry_user_id = %s
                  )
              AND (%s = 'その他' OR COALESCE(p.category, 'その他') = %s)
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


@router.get("/bundles/{bundle_id}/entry-candidates/detail")
def get_bundle_entry_candidates_detail(bundle_id: int, request: Request):
    """応募可能な記事一覧（詳細プレビュー付き）"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        cur.execute(
            "SELECT id, genre, status FROM bundles WHERE id = %s",
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
                CASE WHEN p.user_id = %s THEN 'own' ELSE 'gacha' END AS entry_type,
                EXISTS (
                    SELECT 1 FROM bundle_items bi 
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
                        SELECT 1 FROM gacha_logs g 
                        WHERE g.user_id = %s AND g.prompt_id = p.id
                    )
                  )
              AND (%s = 'その他' OR COALESCE(p.category, 'その他') = %s)
            ORDER BY p.id DESC
            """,
            (user_id, bundle_id, user_id, user_id, user_id, bundle["genre"], bundle["genre"]),
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


@router.get("/bundles/{bundle_id}/preview")
def get_bundle_preview(bundle_id: int):
    """福袋のプレビュー（販売前でも一部内容が見られる）"""
    with db_transaction() as (conn, cur):
        cur.execute(
            """
            SELECT id, title, description, genre, price_points, status, target_article_count
            FROM bundles WHERE id = %s
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
                bi.entry_type
            FROM bundle_items bi
            INNER JOIN prompts p ON p.id = bi.prompt_id
            WHERE bi.bundle_id = %s
            ORDER BY bi.id DESC
            LIMIT 12
            """,
            (bundle_id,),
        )
        rows = cur.fetchall()

        author_ids = {resolve_author_name(row) for row in rows}

        items = [
            {
                "prompt_id": row["prompt_id"],
                "title": row["title"],
                "category": row["category"] or "その他",
                "author_name": resolve_author_name(row),
                "entry_type": row["entry_type"],
                "content_preview": build_content_preview(row["content"], 120),
                "has_url": bool(row["url"]),
            }
            for row in rows
        ]

        return {
            "bundle_id": bundle["id"],
            "bundle_title": bundle["title"],
            "status": bundle["status"],
            "title_count": len(items),
            "author_count": len(author_ids),
            "items": items,
        }


@router.post("/bundles/entry")
def entry_bundle(req: BundleEntryRequest, request: Request):
    """福袋に応募する"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request, require_csrf=True)

        # 福袋存在・状態チェック
        cur.execute(
            "SELECT id, status, genre, target_article_count FROM bundles WHERE id = %s FOR UPDATE",
            (req.bundle_id,),
        )
        bundle = cur.fetchone()
        if not bundle:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")
        if bundle["status"] != "recruiting":
            raise HTTPException(status_code=400, detail="この福袋は募集中ではありません")

        # 記事存在・権限チェック
        cur.execute(
            """
            SELECT id, user_id, original_creator_user_id, category, 
                   review_status, bundle_entry_enabled, is_visible
            FROM prompts WHERE id = %s FOR UPDATE
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
        # ガチャ取得記事かチェック
        cur.execute(
            "SELECT 1 FROM gacha_logs WHERE user_id = %s AND prompt_id = %s LIMIT 1",
            (user_id, req.prompt_id),
        )
        is_gacha_prompt = bool(cur.fetchone())

        if not is_own_prompt and not is_gacha_prompt:
            raise HTTPException(status_code=403, detail="自分の投稿記事、または自分がガチャで取得した記事のみ応募できます")

        # ジャンルチェック
        if bundle["genre"] and bundle["genre"] != "その他":
            prompt_category = prompt["category"] or "その他"
            if prompt_category != bundle["genre"]:
                raise HTTPException(status_code=400, detail="福袋ジャンルと記事カテゴリが一致しません")

        entry_type = "own" if is_own_prompt else "gacha"
        original_creator_user_id = prompt["original_creator_user_id"] or prompt["user_id"]

        # 二重応募チェック
        cur.execute(
            """
            SELECT 1 FROM bundle_items 
            WHERE bundle_id = %s AND prompt_id = %s AND entry_user_id = %s
            LIMIT 1
            """,
            (req.bundle_id, req.prompt_id, user_id),
        )
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="この記事は既に応募済みです")

        # 応募登録
        cur.execute(
            """
            INSERT INTO bundle_items 
                (bundle_id, prompt_id, entry_user_id, original_creator_user_id, entry_type)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (req.bundle_id, req.prompt_id, user_id, original_creator_user_id, entry_type),
        )
        new_item = cur.fetchone()

        # 現在の応募数を取得
        cur.execute(
            "SELECT COUNT(*) AS current_article_count FROM bundle_items WHERE bundle_id = %s",
            (req.bundle_id,),
        )
        current_count = cur.fetchone()["current_article_count"]

        return {
            "status": "ok",
            "bundle_item_id": new_item["id"],
            "entry_type": entry_type,
            "current_article_count": current_count,
            "target_article_count": bundle["target_article_count"],
            "is_ready_to_publish": current_count >= bundle["target_article_count"],
        }


@router.post("/bundles/buy")
def buy_bundle(req: BuyBundleRequest, request: Request):
    """福袋を購入する（ポイント消費）"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request, require_csrf=True)

        cur.execute(
            "SELECT id, price_points, status FROM bundles WHERE id = %s FOR UPDATE",
            (req.bundle_id,),
        )
        bundle = cur.fetchone()
        if not bundle or bundle["status"] != "active":
            raise HTTPException(status_code=404, detail="この福袋は購入できません")

        cur.execute(
            "SELECT points FROM users WHERE user_id = %s FOR UPDATE",
            (user_id,),
        )
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
        if user["points"] < bundle["price_points"]:
            raise HTTPException(status_code=400, detail="ポイントが不足しています")

        # 購入履歴登録 + ポイント減算
        cur.execute(
            "INSERT INTO bundle_purchases (user_id, bundle_id, price_points) VALUES (%s, %s, %s)",
            (user_id, req.bundle_id, bundle["price_points"]),
        )
        cur.execute(
            "UPDATE users SET points = points - %s WHERE user_id = %s",
            (bundle["price_points"], user_id),
        )

        return {"status": "ok", "message": "福袋を購入しました"}


@router.get("/bundles/recruiting")
def list_recruiting_bundles(request: Request):
    """募集中の福袋一覧"""
    return _list_bundles(request, status_filter="recruiting")


@router.get("/bundles")
def list_bundles(request: Request):
    """募集中 + 販売中の福袋一覧"""
    return _list_bundles(request, status_filter=None)


def _list_bundles(request: Request, status_filter: Optional[str] = None):
    """福袋一覧の共通ロジック"""
    user_id = None
    try:
        with get_db() as conn:
            user = get_current_user(conn, request)
            user_id = user["user_id"]
    except Exception:
        user_id = None

    with db_cursor() as cur:
        status_condition = "b.status = 'recruiting'" if status_filter == "recruiting" else "b.status IN ('recruiting', 'active')"

        cur.execute(
            f"""
            SELECT
                b.id, b.title, b.description, b.genre, b.price_points, b.status,
                b.target_article_count, b.created_at, b.published_at,
                COUNT(bi.id) AS current_article_count,
                CASE
                    WHEN %s IS NULL THEN FALSE
                    ELSE EXISTS (
                        SELECT 1 FROM bundle_purchases bp 
                        WHERE bp.bundle_id = b.id AND bp.user_id = %s
                    )
                END AS is_purchased
            FROM bundles b
            LEFT JOIN bundle_items bi ON b.id = bi.bundle_id
            WHERE {status_condition}
            GROUP BY b.id, b.title, b.description, b.genre, b.price_points, 
                     b.status, b.target_article_count, b.created_at, b.published_at
            ORDER BY b.id DESC
            """,
            (user_id, user_id),
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
                "is_purchased": row["is_purchased"],
                "created_at": row["created_at"],
                "published_at": row["published_at"],
            }
            for row in rows
        ]


@router.get("/bundles/{bundle_id}/progress")
def get_bundle_progress(bundle_id: int):
    """福袋の募集進捗"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                b.id, b.title, b.status, b.genre, b.target_article_count,
                COUNT(bi.id) AS current_article_count
            FROM bundles b
            LEFT JOIN bundle_items bi ON b.id = bi.bundle_id
            WHERE b.id = %s
            GROUP BY b.id, b.title, b.status, b.genre, b.target_article_count
            """,
            (bundle_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")

        current = row["current_article_count"]
        target = row["target_article_count"]

        return {
            "bundle_id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "genre": row["genre"],
            "current_article_count": current,
            "target_article_count": target,
            "remaining_article_count": max(target - current, 0),
            "progress_percent": int((current / target) * 100) if target > 0 else 0,
            "is_ready_to_publish": current >= target,
        }


@router.get("/bundles/{bundle_id}/purchase-status")
def get_bundle_purchase_status(bundle_id: int, request: Request):
    """ユーザーがこの福袋を購入済みかどうか"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        cur.execute("SELECT id FROM bundles WHERE id = %s", (bundle_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="福袋が見つかりません")

        cur.execute(
            """
            SELECT 1 FROM bundle_purchases 
            WHERE bundle_id = %s AND user_id = %s LIMIT 1
            """,
            (bundle_id, user_id),
        )
        purchased = bool(cur.fetchone())

        return {"bundle_id": bundle_id, "purchased": purchased}


@router.get("/bundles/{bundle_id}")
def get_bundle(bundle_id: int):
    """福袋の基本情報"""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                b.id, b.title, b.description, b.genre, b.price_points, b.status,
                b.target_article_count, b.created_at, b.published_at,
                COUNT(bi.id) AS current_article_count
            FROM bundles b
            LEFT JOIN bundle_items bi ON b.id = bi.bundle_id
            WHERE b.id = %s
            GROUP BY b.id, b.title, b.description, b.genre, b.price_points, 
                     b.status, b.target_article_count, b.created_at, b.published_at
            """,
            (bundle_id,),
        )
        bundle = cur.fetchone()
        if not bundle:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")

        current = bundle["current_article_count"]
        target = bundle["target_article_count"]

        return {
            "id": bundle["id"],
            "title": bundle["title"],
            "description": bundle["description"],
            "genre": bundle["genre"],
            "price_points": bundle["price_points"],
            "status": bundle["status"],
            "target_article_count": bundle["target_article_count"],
            "current_article_count": current,
            "remaining_article_count": max(target - current, 0),
            "is_ready_to_publish": current >= target,
            "created_at": bundle["created_at"],
            "published_at": bundle["published_at"],
        }


@router.get("/bundles/{bundle_id}/items")
def get_bundle_items(bundle_id: int, request: Request):
    """購入済みの福袋の中身を取得"""
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request)

        cur.execute(
            "SELECT 1 FROM bundle_purchases WHERE bundle_id = %s AND user_id = %s",
            (bundle_id, user_id),
        )
        if not cur.fetchone():
            raise HTTPException(status_code=403, detail="この福袋は未購入です")

        cur.execute(
            """
            SELECT p.id, p.title, p.content, p.category, p.url
            FROM bundle_items bi
            INNER JOIN prompts p ON bi.prompt_id = p.id
            WHERE bi.bundle_id = %s AND p.is_visible = TRUE
            ORDER BY bi.id ASC
            """,
            (bundle_id,),
        )
        return cur.fetchall()

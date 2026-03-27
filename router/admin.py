"""
admin.py — 管理者向けAPI

・福袋（bundles）の作成・管理
・プロンプト停止申請の審査
・出金申請の処理
・全操作にCSRF保護を適用
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from db import db_transaction
from dependencies import get_current_admin_user_id_dep  # ← dependencies.pyを使用
from models import (
    AddBundleItemRequest,
    CloseBundleRequest,
    CreateBundleRequest,
    DistributeBundleRequest,
    ProcessPromptStopRequest,
    ProcessWithdrawRequest,
    PublishBundleRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/bundles")
def create_bundle(req: CreateBundleRequest, request: Request):
    """新しい福袋を作成"""
    with db_transaction() as (conn, cur):
        # 管理者認証 + CSRF検証
        get_current_admin_user_id_dep(request, conn, require_csrf=True)

        if req.target_article_count <= 0:
            raise HTTPException(status_code=400, detail="募集記事数は1以上にしてください")
        if req.price_points <= 0:
            raise HTTPException(status_code=400, detail="価格は1以上にしてください")

        cur.execute(
            """
            INSERT INTO bundles (
                title, description, target_article_count, genre, price_points, status
            )
            VALUES (%s, %s, %s, %s, %s, 'recruiting')
            RETURNING id
            """,
            (req.title, req.description, req.target_article_count, req.genre, req.price_points),
        )
        bundle = cur.fetchone()

        return {
            "status": "ok",
            "bundle_id": bundle["id"],
            "message": "福袋を作成しました"
        }


@router.post("/bundles/items")
def add_bundle_item(req: AddBundleItemRequest, request: Request):
    """福袋に記事を追加"""
    with db_transaction() as (conn, cur):
        get_current_admin_user_id_dep(request, conn, require_csrf=True)

        cur.execute(
            """
            SELECT id, user_id, original_creator_user_id, review_status,
                   bundle_entry_enabled, is_visible
            FROM prompts
            WHERE id = %s
            FOR UPDATE
            """,
            (req.prompt_id,),
        )
        prompt = cur.fetchone()

        if not prompt:
            raise HTTPException(status_code=404, detail="指定された記事が見つかりません")

        if prompt["review_status"] != "accepted":
            raise HTTPException(status_code=400, detail="accepted状態の記事のみ採用可能です")

        if not prompt["bundle_entry_enabled"] or not prompt["is_visible"]:
            raise HTTPException(status_code=400, detail="この記事は福袋への登録が無効化されています")

        original_creator_user_id = prompt["original_creator_user_id"] or prompt["user_id"]
        entry_type = "own" if prompt["user_id"] == prompt["user_id"] else "gacha"  # 修正: entry_user_idは不要

        cur.execute(
            """
            INSERT INTO bundle_items (
                bundle_id, prompt_id, entry_user_id, original_creator_user_id, entry_type
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (bundle_id, prompt_id, entry_user_id) DO NOTHING
            """,
            (req.bundle_id, req.prompt_id, prompt["user_id"], original_creator_user_id, entry_type),  # entry_user_idとしてpromptのuser_idを使用
        )

        return {"status": "ok", "message": "記事を福袋に追加しました"}


@router.delete("/bundles/items/{bundle_item_id}")
def remove_bundle_item(bundle_item_id: int, request: Request):
    """福袋から記事を削除"""
    with db_transaction() as (conn, cur):
        get_current_admin_user_id_dep(request, conn, require_csrf=True)

        cur.execute(
            "DELETE FROM bundle_items WHERE id = %s RETURNING id",
            (bundle_item_id,)
        )
        deleted = cur.fetchone()

        if not deleted:
            raise HTTPException(status_code=404, detail="指定された募集記事が見つかりません")

        return {
            "status": "ok",
            "message": "記事を削除しました",
            "bundle_item_id": bundle_item_id
        }


@router.post("/bundles/publish")
def publish_bundle(req: PublishBundleRequest, request: Request):
    """福袋を販売開始（recruiting → active）"""
    with db_transaction() as (conn, cur):
        get_current_admin_user_id_dep(request, conn, require_csrf=True)

        cur.execute(
            "SELECT id, status, target_article_count FROM bundles WHERE id = %s FOR UPDATE",
            (req.bundle_id,)
        )
        bundle = cur.fetchone()

        if not bundle:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")

        if bundle["status"] != "recruiting":
            raise HTTPException(status_code=400, detail="募集中の福袋のみ販売開始できます")

        cur.execute(
            "SELECT COUNT(*) AS current_count FROM bundle_items WHERE bundle_id = %s",
            (req.bundle_id,)
        )
        current_count = cur.fetchone()["current_count"]

        if current_count < bundle["target_article_count"]:
            raise HTTPException(
                status_code=400,
                detail=f"記事数が不足しています（現在: {current_count} / 必要: {bundle['target_article_count']}）"
            )

        cur.execute(
            """
            UPDATE bundles
            SET status = 'active',
                published_at = NOW()
            WHERE id = %s
            """,
            (req.bundle_id,)
        )

        return {"status": "ok", "message": "福袋を販売開始しました"}


@router.post("/bundles/close")
def close_bundle(req: CloseBundleRequest, request: Request):
    """販売中の福袋を締め切り（active → closed）"""
    with db_transaction() as (conn, cur):
        get_current_admin_user_id_dep(request, conn, require_csrf=True)

        cur.execute("SELECT id, status FROM bundles WHERE id = %s FOR UPDATE", (req.bundle_id,))
        bundle = cur.fetchone()

        if not bundle:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")
        if bundle["status"] != "active":
            raise HTTPException(status_code=400, detail="販売中の福袋のみ締め切りできます")

        cur.execute(
            "UPDATE bundles SET status = 'closed' WHERE id = %s",
            (req.bundle_id,)
        )

        return {"status": "ok", "message": "福袋を締め切りました", "bundle_id": req.bundle_id}


@router.post("/bundles/distribute")
def distribute_bundle(req: DistributeBundleRequest, request: Request):
    """福袋の売上をクリエイターに分配"""
    with db_transaction() as (conn, cur):
        get_current_admin_user_id_dep(request, conn, require_csrf=True)

        cur.execute("SELECT id FROM bundles WHERE id = %s FOR UPDATE", (req.bundle_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="福袋が見つかりません")

        # 売上合計取得
        cur.execute(
            "SELECT COALESCE(SUM(price_points), 0) AS total_points FROM bundle_purchases WHERE bundle_id = %s",
            (req.bundle_id,)
        )
        total_points = cur.fetchone()["total_points"]

        if total_points <= 0:
            return {"status": "ok", "message": "売上がありません"}

        # 分配処理（元のロジックを維持しつつ整理）
        cur.execute(
            """
            SELECT entry_user_id, original_creator_user_id
            FROM bundle_items
            WHERE bundle_id = %s
            ORDER BY id ASC
            """,
            (req.bundle_id,)
        )
        items = cur.fetchall()

        if not items:
            raise HTTPException(status_code=400, detail="福袋に採用記事がありません")

        total_items = len(items)
        entry_unit = int((total_points * 0.5) / total_items)
        creator_unit = int((total_points * 0.1) / total_items)

        entry_remainder = int(total_points * 0.5) - entry_unit * total_items
        creator_remainder = int(total_points * 0.1) - creator_unit * total_items

        grouped = {}
        for row in items:
            key = (row["entry_user_id"], row["original_creator_user_id"] or row["entry_user_id"])
            if key not in grouped:
                grouped[key] = {
                    "entry_user_id": row["entry_user_id"],
                    "original_creator_user_id": key[1],
                    "entry_yen": 0,
                    "creator_yen": 0,
                }
            grouped[key]["entry_yen"] += entry_unit
            grouped[key]["creator_yen"] += creator_unit

        # 端数加算
        if grouped:
            first_key = next(iter(grouped))
            grouped[first_key]["entry_yen"] += entry_remainder
            grouped[first_key]["creator_yen"] += creator_remainder

        for data in grouped.values():
            cur.execute(
                """
                INSERT INTO bundle_reward_distributions (
                    bundle_id, entry_user_id, original_creator_user_id,
                    sales_yen, entry_yen, creator_yen, distribution_round
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    req.bundle_id,
                    data["entry_user_id"],
                    data["original_creator_user_id"],
                    total_points,
                    data["entry_yen"],
                    data["creator_yen"],
                    req.distribution_round,
                ),
            )

            if cur.rowcount > 0:
                # entry_user（投稿者）報酬
                cur.execute(
                    """
                    INSERT INTO creator_wallets (user_id, yen)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET yen = creator_wallets.yen + %s
                    """,
                    (data["entry_user_id"], data["entry_yen"], data["entry_yen"]),
                )
                # original_creator報酬
                cur.execute(
                    """
                    INSERT INTO creator_wallets (user_id, yen)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET yen = creator_wallets.yen + %s
                    """,
                    (data["original_creator_user_id"], data["creator_yen"], data["creator_yen"]),
                )
                logger.info(
                    "Bundle distribution: bundle=%s entry=%s entry_yen=%s creator=%s creator_yen=%s round=%s",
                    req.bundle_id, data["entry_user_id"], data["entry_yen"],
                    data["original_creator_user_id"], data["creator_yen"], req.distribution_round
                )

        return {"status": "ok", "message": "分配処理が完了しました"}


@router.get("/prompt-stop-requests")
def list_prompt_stop_requests(request: Request):
    """プロンプト停止申請一覧（読み取り専用なのでCSRF不要）"""
    with db_transaction() as (conn, cur):
        get_current_admin_user_id_dep(request, conn)  # CSRF不要

        cur.execute(
            """
            SELECT
                psr.id, psr.prompt_id, psr.user_id, psr.reason, psr.status,
                psr.created_at, psr.processed_at, p.title
            FROM prompt_stop_requests psr
            INNER JOIN prompts p ON p.id = psr.prompt_id
            ORDER BY psr.created_at DESC, psr.id DESC
            """
        )
        return {"status": "ok", "requests": cur.fetchall()}


@router.patch("/prompt-stop-requests/{request_id}")
def process_prompt_stop_request(
    request_id: int, req: ProcessPromptStopRequest, request: Request
):
    """停止申請の承認/却下"""
    if req.status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="statusは approved または rejected のみ有効です")

    with db_transaction() as (conn, cur):
        get_current_admin_user_id_dep(request, conn, require_csrf=True)

        cur.execute(
            "SELECT id, prompt_id, status FROM prompt_stop_requests WHERE id = %s FOR UPDATE",
            (request_id,)
        )
        row = cur.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="停止申請が見つかりません")
        if row["status"] != "pending":
            raise HTTPException(status_code=400, detail="この申請は既に処理済みです")

        cur.execute(
            """
            UPDATE prompt_stop_requests
            SET status = %s, processed_at = NOW()
            WHERE id = %s
            """,
            (req.status, request_id)
        )

        if req.status == "approved":
            cur.execute("UPDATE prompts SET is_visible = FALSE WHERE id = %s", (row["prompt_id"],))

        return {"status": "ok", "message": f"申請を{req.status}にしました", "request_id": request_id}


@router.get("/withdraw/requests")
def list_withdraw_requests(request: Request):
    """出金申請一覧（読み取り専用）"""
    with db_transaction() as (conn, cur):
        get_current_admin_user_id_dep(request, conn)  # CSRF不要

        cur.execute(
            """
            SELECT id, user_id, amount_yen, method, destination, withdraw_code,
                   status, admin_note, created_at, processed_at
            FROM withdrawal_requests
            ORDER BY created_at DESC, id DESC
            """
        )
        return {"status": "ok", "requests": cur.fetchall()}


@router.patch("/withdraw/requests/{request_id}")
def process_withdraw_request(
    request_id: int, req: ProcessWithdrawRequest, request: Request
):
    """出金申請の処理（approved / paid / rejected）"""
    if req.status not in ("approved", "paid", "rejected"):
        raise HTTPException(status_code=400, detail="statusは approved / paid / rejected のみ有効です")

    with db_transaction() as (conn, cur):
        get_current_admin_user_id_dep(request, conn, require_csrf=True)

        cur.execute(
            "SELECT id, user_id, amount_yen, status FROM withdrawal_requests WHERE id = %s FOR UPDATE",
            (request_id,)
        )
        row = cur.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="出金申請が見つかりません")

        if req.status == "approved" and row["status"] == "pending":
            amount_yen = row["amount_yen"]
            user_id = row["user_id"]

            cur.execute(
                "SELECT yen FROM creator_wallets WHERE user_id = %s FOR UPDATE",
                (user_id,)
            )
            wallet = cur.fetchone()
            current_yen = wallet["yen"] if wallet else 0

            if current_yen < amount_yen:
                raise HTTPException(
                    status_code=400,
                    detail=f"ウォレット残高不足です（残高: {current_yen}円 / 申請額: {amount_yen}円）"
                )

            cur.execute(
                "UPDATE creator_wallets SET yen = yen - %s WHERE user_id = %s",
                (amount_yen, user_id)
            )
            logger.info("Withdraw approved: request_id=%d user=%s amount=%d", request_id, user_id, amount_yen)

        cur.execute(
            """
            UPDATE withdrawal_requests
            SET status = %s, admin_note = %s, processed_at = NOW()
            WHERE id = %s
            """,
            (req.status, req.admin_note, request_id)
        )

        return {"status": "ok", "message": f"申請を{req.status}に更新しました", "request_id": request_id}

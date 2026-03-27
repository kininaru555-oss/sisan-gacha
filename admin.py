from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from db import db_transaction
from models import (
    AddBundleItemRequest,
    CreateBundleRequest,
    DistributeBundleRequest,
    ProcessPromptStopRequest,
    ProcessWithdrawRequest,
    PublishBundleRequest,
)
from utils import get_current_admin_user_id


router = APIRouter()


@router.post("/admin/bundles")
def create_bundle(req: CreateBundleRequest, request: Request):
    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request, require_csrf=True)

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
        return {"bundle_id": bundle["id"]}


@router.post("/admin/bundles/items")
def add_bundle_item(req: AddBundleItemRequest, request: Request):
    with db_transaction() as (conn, cur):
        entry_user_id = get_current_admin_user_id(conn, request, require_csrf=True)

        cur.execute(
            """
            SELECT id, user_id, original_creator_user_id, review_status, bundle_entry_enabled, is_visible
            FROM prompts
            WHERE id = %s
            FOR UPDATE
            """,
            (req.prompt_id,),
        )
        prompt = cur.fetchone()
        if not prompt:
            raise HTTPException(status_code=404, detail="記事なし")
        if prompt["review_status"] != "accepted":
            raise HTTPException(status_code=400, detail="accepted記事のみ採用可能です")
        if not prompt["bundle_entry_enabled"] or not prompt["is_visible"]:
            raise HTTPException(status_code=400, detail="福袋利用不可の記事です")

        original_creator_user_id = prompt["original_creator_user_id"] or prompt["user_id"]
        entry_type = "own" if prompt["user_id"] == entry_user_id else "gacha"

        cur.execute(
            """
            INSERT INTO bundle_items (
                bundle_id, prompt_id, entry_user_id, original_creator_user_id, entry_type
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (bundle_id, prompt_id, entry_user_id) DO NOTHING
            """,
            (req.bundle_id, req.prompt_id, entry_user_id, original_creator_user_id, entry_type),
        )
        return {"status": "added"}


@router.delete("/admin/bundles/items/{bundle_item_id}")
def remove_bundle_item(bundle_item_id: int, request: Request):
    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request, require_csrf=True)
        cur.execute("DELETE FROM bundle_items WHERE id = %s RETURNING id", (bundle_item_id,))
        deleted = cur.fetchone()
        if not deleted:
            raise HTTPException(status_code=404, detail="募集記事が見つかりません")
        return {"status": "deleted", "bundle_item_id": bundle_item_id}


@router.post("/admin/bundles/publish")
def publish_bundle(req: PublishBundleRequest, request: Request):
    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request, require_csrf=True)

        cur.execute("SELECT id, status, target_article_count FROM bundles WHERE id = %s FOR UPDATE", (req.bundle_id,))
        bundle = cur.fetchone()
        if not bundle:
            raise HTTPException(status_code=404, detail="福袋が見つかりません")
        if bundle["status"] != "recruiting":
            raise HTTPException(status_code=400, detail="募集中の福袋のみ販売開始できます")

        cur.execute("SELECT COUNT(*) AS current_article_count FROM bundle_items WHERE bundle_id = %s", (req.bundle_id,))
        current_count = cur.fetchone()["current_article_count"]

        if current_count < bundle["target_article_count"]:
            raise HTTPException(status_code=400, detail=f"記事数不足です（{current_count}/{bundle['target_article_count']}）")

        cur.execute(
            """
            UPDATE bundles
            SET status = 'active',
                published_at = NOW()
            WHERE id = %s
            """,
            (req.bundle_id,),
        )
        return {"status": "published"}


@router.post("/admin/bundles/distribute")
def distribute_bundle(req: DistributeBundleRequest, request: Request):
    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request, require_csrf=True)

        cur.execute("SELECT id FROM bundles WHERE id = %s FOR UPDATE", (req.bundle_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="福袋が見つかりません")

        cur.execute("SELECT COALESCE(SUM(price_points), 0) AS total_points FROM bundle_purchases WHERE bundle_id = %s", (req.bundle_id,))
        total = cur.fetchone()["total_points"]
        if total <= 0:
            return {"status": "no_sales"}

        cur.execute(
            """
            SELECT entry_user_id, original_creator_user_id
            FROM bundle_items
            WHERE bundle_id = %s
            ORDER BY id ASC
            """,
            (req.bundle_id,),
        )
        items = cur.fetchall()
        if not items:
            raise HTTPException(status_code=400, detail="福袋に採用記事がありません")

        total_items = len(items)
        entry_unit_yen = int((total * 0.5) / total_items)
        creator_unit_yen = int((total * 0.1) / total_items)

        grouped: dict[tuple[str, str], dict[str, int | str]] = {}
        for row in items:
            entry_user_id = row["entry_user_id"]
            original_creator_user_id = row["original_creator_user_id"] or row["entry_user_id"]
            key = (entry_user_id, original_creator_user_id)
            if key not in grouped:
                grouped[key] = {
                    "entry_user_id": entry_user_id,
                    "original_creator_user_id": original_creator_user_id,
                    "sales_yen": total,
                    "entry_yen": 0,
                    "creator_yen": 0,
                }
            grouped[key]["entry_yen"] += entry_unit_yen
            grouped[key]["creator_yen"] += creator_unit_yen

        for row in grouped.values():
            cur.execute(
                """
                INSERT INTO bundle_reward_distributions (
                    bundle_id,
                    entry_user_id,
                    original_creator_user_id,
                    sales_yen,
                    entry_yen,
                    creator_yen,
                    distribution_round
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    req.bundle_id,
                    row["entry_user_id"],
                    row["original_creator_user_id"],
                    row["sales_yen"],
                    row["entry_yen"],
                    row["creator_yen"],
                    req.distribution_round,
                ),
            )

            if cur.rowcount > 0:
                cur.execute(
                    """
                    INSERT INTO creator_wallets (user_id, yen)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id)
                    DO UPDATE SET yen = creator_wallets.yen + %s
                    """,
                    (row["entry_user_id"], row["entry_yen"], row["entry_yen"]),
                )
                cur.execute(
                    """
                    INSERT INTO creator_wallets (user_id, yen)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id)
                    DO UPDATE SET yen = creator_wallets.yen + %s
                    """,
                    (row["original_creator_user_id"], row["creator_yen"], row["creator_yen"]),
                )

        return {"status": "distributed"}


@router.get("/admin/prompt-stop-requests")
def admin_list_prompt_stop_requests(request: Request):
    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request)
        cur.execute(
            """
            SELECT
                psr.id,
                psr.prompt_id,
                psr.user_id,
                psr.reason,
                psr.status,
                psr.created_at,
                psr.processed_at,
                p.title
            FROM prompt_stop_requests psr
            INNER JOIN prompts p ON p.id = psr.prompt_id
            ORDER BY psr.created_at DESC, psr.id DESC
            """
        )
        return cur.fetchall()


@router.patch("/admin/prompt-stop-requests/{request_id}")
def admin_process_prompt_stop_request(request_id: int, req: ProcessPromptStopRequest, request: Request):
    if req.status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="status は approved または rejected のみです")

    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request, require_csrf=True)

        cur.execute("SELECT id, prompt_id, status FROM prompt_stop_requests WHERE id = %s FOR UPDATE", (request_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="停止申請が見つかりません")
        if row["status"] != "pending":
            raise HTTPException(status_code=400, detail="この停止申請は既に処理済みです")

        cur.execute(
            """
            UPDATE prompt_stop_requests
            SET status = %s,
                processed_at = NOW()
            WHERE id = %s
            """,
            (req.status, request_id),
        )

        if req.status == "approved":
            cur.execute("UPDATE prompts SET is_visible = FALSE WHERE id = %s", (row["prompt_id"],))

        return {"status": req.status, "request_id": request_id}


@router.get("/admin/withdraw/requests")
def admin_list_withdraw_requests(request: Request):
    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request)
        cur.execute(
            """
            SELECT
                id,
                user_id,
                amount_yen,
                method,
                destination,
                withdraw_code,
                status,
                admin_note,
                created_at,
                processed_at
            FROM withdrawal_requests
            ORDER BY created_at DESC, id DESC
            """
        )
        return cur.fetchall()


@router.patch("/admin/withdraw/requests/{request_id}")
def admin_process_withdraw_request(request_id: int, req: ProcessWithdrawRequest, request: Request):
    if req.status not in ("approved", "paid", "rejected"):
        raise HTTPException(status_code=400, detail="status は approved / paid / rejected のみです")

    with db_transaction() as (conn, cur):
        get_current_admin_user_id(conn, request, require_csrf=True)

        cur.execute("SELECT id, status FROM withdrawal_requests WHERE id = %s FOR UPDATE", (request_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="出金申請が見つかりません")

        cur.execute(
            """
            UPDATE withdrawal_requests
            SET status = %s,
                admin_note = %s,
                processed_at = NOW()
            WHERE id = %s
            """,
            (req.status, req.admin_note, request_id),
        )
        return {"status": req.status, "request_id": request_id}

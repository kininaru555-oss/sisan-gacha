from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from db import db_transaction
from models import CreateCheckoutSessionRequest
from utils import ensure_user_row_exists, get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter()

STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
SITE_URL = os.environ["SITE_URL"].rstrip("/")

stripe.api_key = STRIPE_SECRET_KEY


def _now() -> datetime:
    """タイムゾーン付きの現在時刻（UTC）を返す"""
    return datetime.now(timezone.utc)


def get_product_config(product_code: str) -> dict:
    if product_code == "300":
        return {"points": 300, "amount_jpy": 300, "name": "300ポイント"}
    if product_code == "1000":
        return {"points": 1000, "amount_jpy": 900, "name": "1000ポイント"}
    raise HTTPException(status_code=400, detail="商品エラー")


@router.post("/stripe/create-checkout-session")
def create_checkout_session(req: CreateCheckoutSessionRequest, request: Request):
    with db_transaction() as (conn, cur):
        user_id = get_current_user_id(conn, request, require_csrf=True)
        ensure_user_row_exists(cur, user_id)
        product = get_product_config(req.product_code)

        session = stripe.checkout.Session.create(
            mode="payment",
            success_url=f"{SITE_URL}/mypage.html?checkout=success",
            cancel_url=f"{SITE_URL}/mypage.html?checkout=cancel",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "jpy",
                        "unit_amount": product["amount_jpy"],
                        "product_data": {
                            "name": product["name"],
                            "description": f"{product['points']}pt を自動付与",
                        },
                    },
                    "quantity": 1,
                }
            ],
            metadata={
                "user_id": user_id,
                "product_code": req.product_code,
                "points_to_add": str(product["points"]),
                "amount_jpy": str(product["amount_jpy"]),
            },
        )

        cur.execute(
            """
            INSERT INTO payments (
                stripe_session_id,
                user_id,
                product_code,
                points_to_add,
                amount_jpy,
                status,
                created_at
            )
            VALUES (%s, %s, %s, %s, %s, 'pending', %s)
            ON CONFLICT (stripe_session_id) DO NOTHING
            """,
            (
                session.id,
                user_id,
                req.product_code,
                product["points"],
                product["amount_jpy"],
                _now(),
            ),
        )

        return {"checkout_url": session.url}


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event["type"]
    logger.info("stripe webhook received: %s", event_type)

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(event["data"]["object"])

    elif event_type == "checkout.session.expired":
        _handle_checkout_expired(event["data"]["object"])

    elif event_type == "charge.refunded":
        _handle_charge_refunded(event["data"]["object"])

    return JSONResponse({"received": True})


# ---------------------------------------------------------------------------
# ハンドラ
# ---------------------------------------------------------------------------

def _handle_checkout_completed(session: dict) -> None:
    stripe_session_id = session["id"]
    stripe_payment_intent_id = session.get("payment_intent")
    metadata = session.get("metadata", {})

    user_id = metadata.get("user_id")
    product_code = metadata.get("product_code")
    points_to_add = int(metadata.get("points_to_add", "0"))
    amount_jpy = int(metadata.get("amount_jpy", "0"))

    if not user_id or not product_code or points_to_add <= 0:
        logger.warning(
            "checkout.session.completed: invalid metadata, session_id=%s", stripe_session_id
        )
        return

    with db_transaction() as (_, cur):
        ensure_user_row_exists(cur, user_id)

        cur.execute(
            "SELECT * FROM payments WHERE stripe_session_id = %s FOR UPDATE",
            (stripe_session_id,),
        )
        payment = cur.fetchone()

        # ケースA：既に paid（冪等）
        if payment and payment["status"] == "paid":
            logger.info(
                "checkout.session.completed: already paid, skip. session_id=%s", stripe_session_id
            )
            return

        now = _now()

        # ケースB：pending レコードなし（Webhook が先着した場合）
        if not payment:
            cur.execute(
                """
                INSERT INTO payments (
                    stripe_session_id,
                    stripe_payment_intent_id,
                    user_id,
                    product_code,
                    points_to_add,
                    amount_jpy,
                    status,
                    created_at,
                    completed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'paid', %s, %s)
                """,
                (
                    stripe_session_id,
                    stripe_payment_intent_id,
                    user_id,
                    product_code,
                    points_to_add,
                    amount_jpy,
                    now,
                    now,
                ),
            )
            cur.execute(
                "UPDATE users SET points = points + %s WHERE user_id = %s",
                (points_to_add, user_id),
            )
            logger.info(
                "checkout.session.completed: new payment paid. session_id=%s user_id=%s points=%d",
                stripe_session_id, user_id, points_to_add,
            )
            return

        # ケースC：pending → paid
        cur.execute(
            """
            UPDATE payments
            SET status = 'paid',
                stripe_payment_intent_id = %s,
                completed_at = %s
            WHERE stripe_session_id = %s
            """,
            (stripe_payment_intent_id, now, stripe_session_id),
        )
        cur.execute(
            "UPDATE users SET points = points + %s WHERE user_id = %s",
            (points_to_add, user_id),
        )
        logger.info(
            "checkout.session.completed: pending->paid. session_id=%s user_id=%s points=%d",
            stripe_session_id, user_id, points_to_add,
        )


def _handle_checkout_expired(session: dict) -> None:
    """セッション期限切れ：pending レコードを expired に更新する"""
    stripe_session_id = session["id"]

    with db_transaction() as (_, cur):
        cur.execute(
            """
            UPDATE payments
            SET status = 'expired'
            WHERE stripe_session_id = %s
              AND status = 'pending'
            """,
            (stripe_session_id,),
        )
        logger.info(
            "checkout.session.expired: session_id=%s updated to expired", stripe_session_id
        )


def _handle_charge_refunded(charge: dict) -> None:
    """返金：ポイントを差し引き、payments を refunded に更新する"""
    payment_intent_id = charge.get("payment_intent")
    if not payment_intent_id:
        logger.warning("charge.refunded: payment_intent not found in charge object")
        return

    with db_transaction() as (_, cur):
        cur.execute(
            "SELECT * FROM payments WHERE stripe_payment_intent_id = %s FOR UPDATE",
            (payment_intent_id,),
        )
        payment = cur.fetchone()

        if not payment:
            logger.warning(
                "charge.refunded: no payment record for payment_intent_id=%s", payment_intent_id
            )
            return

        if payment["status"] == "refunded":
            logger.info(
                "charge.refunded: already refunded, skip. payment_intent_id=%s", payment_intent_id
            )
            return

        if payment["status"] != "paid":
            logger.warning(
                "charge.refunded: unexpected status=%s for payment_intent_id=%s",
                payment["status"], payment_intent_id,
            )
            return

        points_to_deduct = payment["points_to_add"]
        user_id = payment["user_id"]

        cur.execute(
            """
            UPDATE payments
            SET status = 'refunded'
            WHERE stripe_payment_intent_id = %s
            """,
            (payment_intent_id,),
        )
        cur.execute(
            "UPDATE users SET points = GREATEST(0, points - %s) WHERE user_id = %s",
            (points_to_deduct, user_id),
        )
        logger.info(
            "charge.refunded: refunded. payment_intent_id=%s user_id=%s points_deducted=%d",
            payment_intent_id, user_id, points_to_deduct,
                )

from __future__ import annotations

import os

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from db import db_transaction
from models import CreateCheckoutSessionRequest
from utils import ensure_user_row_exists, get_current_user_id, now_iso


router = APIRouter()

STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
SITE_URL = os.environ["SITE_URL"].rstrip("/")

stripe.api_key = STRIPE_SECRET_KEY


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
                now_iso(),
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

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        stripe_session_id = session["id"]
        stripe_payment_intent_id = session.get("payment_intent")
        metadata = session.get("metadata", {})

        user_id = metadata.get("user_id")
        product_code = metadata.get("product_code")
        points_to_add = int(metadata.get("points_to_add", "0"))
        amount_jpy = int(metadata.get("amount_jpy", "0"))

        if not user_id or not product_code or points_to_add <= 0:
            return JSONResponse({"received": True})

        with db_transaction() as (_, cur):
            ensure_user_row_exists(cur, user_id)

            cur.execute(
                "SELECT * FROM payments WHERE stripe_session_id = %s FOR UPDATE",
                (stripe_session_id,),
            )
            payment = cur.fetchone()

            if payment and payment["status"] == "paid":
                return JSONResponse({"received": True})

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
                        now_iso(),
                        now_iso(),
                    ),
                )
                cur.execute(
                    "UPDATE users SET points = points + %s WHERE user_id = %s",
                    (points_to_add, user_id),
                )
                return JSONResponse({"received": True})

            cur.execute(
                "UPDATE users SET points = points + %s WHERE user_id = %s",
                (points_to_add, user_id),
            )
            cur.execute(
                """
                UPDATE payments
                SET status = 'paid',
                    stripe_payment_intent_id = %s,
                    completed_at = %s
                WHERE stripe_session_id = %s
                """,
                (stripe_payment_intent_id, now_iso(), stripe_session_id),
            )

    return JSONResponse({"received": True})

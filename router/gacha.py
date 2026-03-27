"""
gacha.py — ガチャ実行API

仕様:
- 30ポイント消費 or 無料ガチャチケット消費
- 承認済み・公開中のプロンプトからランダム抽選
- クリエイター報酬（15円）を自動付与（無料ガチャ・自引き除く）
- CSRF保護 + レート制限付き
"""

from __future__ import annotations

from collections import defaultdict
import time

from fastapi import APIRouter, HTTPException, Request

from db import db_transaction
from dependencies import get_current_user_id_dep   # ← dependencies.pyを使用
from models import GachaRequest
from datetime import datetime, timezone

from utils import ensure_user_row_exists

router = APIRouter(prefix="/gacha", tags=["gacha"])


# ─────────────────────────────────────────────
# 簡易レート制限（1ユーザーあたり）
# 将来的には Redis + SlowAPI への移行を強く推奨
# ─────────────────────────────────────────────
gacha_rate_limit = defaultdict(list)
GACHA_RATE_LIMIT_SECONDS = 10      # 10秒間に
GACHA_RATE_LIMIT_MAX_CALLS = 5     # 最大5回まで


def _enforce_gacha_rate_limit(user_id: str) -> None:
    """ガチャ連打防止"""
    now = time.time()
    # 古いタイムスタンプを削除
    gacha_rate_limit[user_id] = [
        t for t in gacha_rate_limit[user_id] if now - t < GACHA_RATE_LIMIT_SECONDS
    ]

    if len(gacha_rate_limit[user_id]) >= GACHA_RATE_LIMIT_MAX_CALLS:
        raise HTTPException(
            status_code=429,
            detail="ガチャの実行が集中しています。少し時間を置いてから再度お試しください。",
        )

    gacha_rate_limit[user_id].append(now)


@router.post("/draw")
def draw_gacha(req: GachaRequest, request: Request):
    """
    ガチャ実行エンドポイント
    - CSRFトークン必須
    - ポイント or 無料ガチャを消費
    - 抽選 + クリエイター報酬付与
    """
    with db_transaction() as (conn, cur):
        # ── 認証・CSRF検証 ──
        user_id = get_current_user_id_dep(
            request=request,
            conn=conn,
            require_csrf=True
        )

        ensure_user_row_exists(cur, user_id)

        # ── レート制限チェック ──
        _enforce_gacha_rate_limit(user_id)

        # ── ユーザー情報取得（FOR UPDATEでロック） ──
        cur.execute(
            """
            SELECT
                points,
                free_gacha,
                is_active
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

        # ── コスト計算 ──
        use_free = user["free_gacha"] > 0
        if not use_free and user["points"] < 30:
            raise HTTPException(
                status_code=400,
                detail="ポイントが不足しています。30ポイント必要です。"
            )

        # ── プロンプト抽選 ──
        cur.execute(
            """
            SELECT
                id,
                user_id AS creator_id,
                title,
                content,
                category,
                url
            FROM prompts
            WHERE review_status = 'accepted'
              AND is_visible = TRUE
            ORDER BY RANDOM()
            LIMIT 1
            """
        )
        prompt = cur.fetchone()

        if not prompt:
            raise HTTPException(
                status_code=404,
                detail="現在抽選可能な記事がありません。しばらくお待ちください。"
            )

        # ── 消費処理 ──
        if use_free:
            cur.execute(
                "UPDATE users SET free_gacha = free_gacha - 1 WHERE user_id = %s",
                (user_id,),
            )
            cost_type = "free"
            cost_points = 0
        else:
            cur.execute(
                "UPDATE users SET points = points - 30 WHERE user_id = %s",
                (user_id,),
            )
            cost_type = "paid"
            cost_points = 30

        draw_time = datetime.now(timezone.utc)

        # ── ガチャログ記録 ──
        cur.execute(
            """
            INSERT INTO gacha_logs (user_id, prompt_id, created_at)
            VALUES (%s, %s, %s)
            """,
            (user_id, prompt["id"], draw_time),
        )

        # ── クリエイター報酬付与（無料ガチャと自引きは除外） ──
        creator_reward_yen = 0
        creator_id = prompt["creator_id"]

        if not use_free and creator_id != user_id:
            creator_reward_yen = 15
            cur.execute(
                """
                INSERT INTO creator_wallets (user_id, yen)
                VALUES (%s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET yen = creator_wallets.yen + %s
                """,
                (creator_id, creator_reward_yen, creator_reward_yen),
            )

        # ── レスポンス ──
        return {
            "status": "ok",
            "result": {
                "id": prompt["id"],
                "title": prompt["title"],
                "content": prompt["content"],
                "category": prompt["category"],
                "url": prompt["url"],
            },
            "meta": {
                "drawn_at": draw_time,
                "cost_type": cost_type,
                "cost_points": cost_points,
                "creator_reward_yen": creator_reward_yen,
                "is_free_gacha": use_free,
            },
        }


@router.get("/ad")
def get_ad():
    """ガチャ画面用の広告（シンプル実装）"""
    ads = [
        {"text": "今だけ特別キャンペーン中！ポイント購入で無料ガチャGET"},
        {"text": "自分の記事を投稿して放置収益化をはじめよう"},
        {"text": "福袋に応募してレア記事をゲットするチャンス"},
    ]
    return {"ad": ads[int(time.time()) % len(ads)]}

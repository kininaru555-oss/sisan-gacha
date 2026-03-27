from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Request

from db import db_transaction
from security import (
    get_current_admin_user,
    get_current_user,
    verify_csrf_request,
)


def ensure_user_row_exists(cur, user_id: str):
    cur.execute(
        """
        INSERT INTO users (
            user_id, points, free_gacha, locked_points, post_count,
            role, token_version, is_active
        )
        VALUES (%s, 0, 0, 0, 0, 'user', 0, TRUE)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id,),
    )


def now_iso() -> str:
    return datetime.utcnow().isoformat()


def client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


# ─────────────────────────────────────────────
# FastAPI Depends 用（おすすめ）
# ─────────────────────────────────────────────
def get_db_conn():
    """トランザクションを使わない場合の簡易依存（必要に応じて使用）"""
    # 実際の運用では db_transaction と組み合わせるのが安全
    pass  # 必要なら拡張


def get_current_user_id(
    request: Request,
    conn=Depends(lambda: None),  # 実際はルーター側でトランザクション管理
    *,
    require_csrf: bool = False,
) -> str:
    # ここは auth.py の各エンドポイントでトランザクション内で呼ぶ想定
    user = get_current_user(conn, request)
    if require_csrf:
        verify_csrf_request(request)
    return user["user_id"]


def get_current_admin_user_id(
    request: Request,
    conn=Depends(lambda: None),
    *,
    require_csrf: bool = False,
) -> str:
    user = get_current_admin_user(conn, request)
    if require_csrf:
        verify_csrf_request(request)
    return user["user_id"]


# 将来的に Depends で直接使える形（例）
# from fastapi import Depends
# current_user = Depends(get_current_user)  # など

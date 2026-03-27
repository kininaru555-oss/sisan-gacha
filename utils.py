from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import HTTPException, Request

from security import (
    get_current_admin_user,
    get_current_user,
    verify_csrf_request,
)


def ensure_user_row_exists(cur, user_id: str):
    cur.execute(
        """
        INSERT INTO users (
            user_id,
            points,
            free_gacha,
            locked_points,
            post_count,
            role,
            token_version,
            is_active
        )
        VALUES (%s, 0, 0, 0, 0, 'user', 0, TRUE)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id,),
    )


def now_iso() -> str:
    return datetime.utcnow().isoformat()


def get_current_user_id(conn, request: Request, *, require_csrf: bool = False) -> str:
    user = get_current_user(conn, request)
    if require_csrf:
        verify_csrf_request(request)
    return user["user_id"]


def get_current_admin_user_id(conn, request: Request, *, require_csrf: bool = False) -> str:
    user = get_current_admin_user(conn, request)
    if require_csrf:
        verify_csrf_request(request)
    return user["user_id"]


def client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None

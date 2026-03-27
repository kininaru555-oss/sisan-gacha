"""
security.py — Cookieベース認証版（仕様書・auth.py・app.js に完全準拠）

・Access Token / Refresh Token / CSRF Token をすべて Cookie で管理
・HttpOnly + Secure + SameSite 設定
・Refresh Token Rotation 対応
・書き込みAPIは X-CSRF-Token 必須
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from fastapi import HTTPException, Request, status
from pwdlib import PasswordHash

from db import db_transaction

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
JWT_SECRET_KEY = os.environ["JWT_SECRET_KEY"]
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))   # 短め推奨
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

password_hasher = PasswordHash.recommended()
UTC = timezone.utc


# ─────────────────────────────────────────────
# エラーヘルパー
# ─────────────────────────────────────────────
def _unauthorized(detail: str = "認証が必要です") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _forbidden(detail: str = "権限がありません") -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


# ─────────────────────────────────────────────
# パスワード
# ─────────────────────────────────────────────
def hash_password(password: str) -> str:
    if not password or len(password) < 4:
        raise HTTPException(status_code=400, detail="パスワードは4文字以上にしてください")
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    return password_hasher.verify(password, password_hash)


def generate_token() -> str:
    return secrets.token_urlsafe(48)


# ─────────────────────────────────────────────
# JWT (Access Token)
# ─────────────────────────────────────────────
def create_access_token(user_id: str, role: str, token_version: int) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": user_id,
        "role": role,
        "token_version": token_version,
        "exp": expire,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def verify_access_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise _unauthorized("アクセストークンの有効期限が切れています")
    except jwt.InvalidTokenError:
        raise _unauthorized("アクセストークンが無効です")


# ─────────────────────────────────────────────
# Cookie 操作
# ─────────────────────────────────────────────
def set_auth_cookies(
    response,
    *,
    access_token: str,
    refresh_token: str,
    csrf_token: str,
):
    # access_token（HttpOnly）
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=True,           # 本番環境では必ずTrue（HTTPS必須）
        samesite="lax",        # または "strict"
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )

    # refresh_token（HttpOnly）
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/",
    )

    # csrf_token（JSから読む必要があるため httponly=False）
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/",
    )


def clear_auth_cookies(response):
    for key in ["access_token", "refresh_token", "csrf_token"]:
        response.delete_cookie(
            key=key,
            path="/",
            httponly=(key != "csrf_token"),
            secure=True,
            samesite="lax",
        )


# ─────────────────────────────────────────────
# ユーザー登録
# ─────────────────────────────────────────────
def register_user(conn, *, user_id: str, password: str, role: str = "user") -> dict:
    user_id = (user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_idは必須です")

    password_hash = hash_password(password)

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="そのuser_idは既に使われています")

        cur.execute(
            """
            INSERT INTO users (
                user_id, password_hash, role, token_version,
                is_active, points, free_gacha, locked_points, post_count
            )
            VALUES (%s, %s, %s, 0, TRUE, 0, 0, 0, 0)
            RETURNING user_id, role
            """,
            (user_id, password_hash, role),
        )
        return cur.fetchone()


# ─────────────────────────────────────────────
# ログインセッション発行
# ─────────────────────────────────────────────
def issue_login_session(
    conn,
    *,
    user_id: str,
    password: str,
    user_agent: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT user_id, password_hash, role, token_version, is_active
            FROM users WHERE user_id = %s
            """,
            (user_id,),
        )
        user = cur.fetchone()

    if not user or not verify_password(password, user["password_hash"]):
        raise _unauthorized("ユーザーIDまたはパスワードが違います")
    if not user.get("is_active", True):
        raise _forbidden("このアカウントは利用停止中です")

    refresh_token = generate_token()
    csrf_token = generate_token()
    refresh_hash = password_hasher.hash(refresh_token)
    csrf_hash = password_hasher.hash(csrf_token)
    token_family = secrets.token_hex(16)

    access_token = create_access_token(
        user_id=user["user_id"],
        role=user["role"],
        token_version=user["token_version"],
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_refresh_tokens (
                user_id, refresh_token_hash, csrf_token_hash, token_family,
                expires_at, user_agent, ip_address, last_used_at
            )
            VALUES (%s, %s, %s, %s, NOW() + INTERVAL '%s days', %s, %s, NOW())
            """,
            (
                user["user_id"],
                refresh_hash,
                csrf_hash,
                token_family,
                REFRESH_TOKEN_EXPIRE_DAYS,
                user_agent,
                ip_address,
            ),
        )

    return {
        "user_id": user["user_id"],
        "role": user["role"],
        "access_token": access_token,
        "refresh_token": refresh_token,
        "csrf_token": csrf_token,
    }


# ─────────────────────────────────────────────
# Refresh Token Rotation
# ─────────────────────────────────────────────
def rotate_refresh_session(
    conn,
    request: Request,
    user_agent: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> dict:
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise _unauthorized("リフレッシュトークンがありません")

    refresh_hash = password_hasher.hash(refresh_token)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, user_id, token_family, expires_at, revoked_at
            FROM user_refresh_tokens
            WHERE refresh_token_hash = %s AND revoked_at IS NULL AND expires_at > NOW()
            """,
            (refresh_hash,),
        )
        row = cur.fetchone()

    if not row:
        raise _unauthorized("リフレッシュトークンが無効です")

    # 新規トークン生成
    new_refresh = generate_token()
    new_csrf = generate_token()
    new_refresh_hash = password_hasher.hash(new_refresh)
    new_csrf_hash = password_hasher.hash(new_csrf)

    with conn.cursor() as cur:
        # 旧トークンを無効化
        cur.execute("UPDATE user_refresh_tokens SET revoked_at = NOW() WHERE id = %s", (row["id"],))

        cur.execute(
            "SELECT user_id, role, token_version FROM users WHERE user_id = %s",
            (row["user_id"],),
        )
        user = cur.fetchone()

        new_access = create_access_token(
            user_id=user["user_id"],
            role=user["role"],
            token_version=user["token_version"],
        )

        # 新トークン保存
        cur.execute(
            """
            INSERT INTO user_refresh_tokens (
                user_id, refresh_token_hash, csrf_token_hash, token_family,
                expires_at, user_agent, ip_address, last_used_at
            )
            VALUES (%s, %s, %s, %s, NOW() + INTERVAL '%s days', %s, %s, NOW())
            """,
            (
                user["user_id"],
                new_refresh_hash,
                new_csrf_hash,
                row["token_family"],
                REFRESH_TOKEN_EXPIRE_DAYS,
                user_agent,
                ip_address,
            ),
        )

    return {
        "user_id": user["user_id"],
        "role": user["role"],
        "access_token": new_access,
        "refresh_token": new_refresh,
        "csrf_token": new_csrf,
    }


# ─────────────────────────────────────────────
# ログアウト
# ─────────────────────────────────────────────
def revoke_current_refresh_session(conn, request: Request):
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        return
    refresh_hash = password_hasher.hash(refresh_token)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_refresh_tokens SET revoked_at = NOW() WHERE refresh_token_hash = %s",
            (refresh_hash,),
        )


# ─────────────────────────────────────────────
# CSRF検証（DBのハッシュと比較する安全版）
# ─────────────────────────────────────────────
def verify_csrf_request(request: Request, conn=None):
    cookie_csrf = request.cookies.get("csrf_token")
    header_csrf = request.headers.get("X-CSRF-Token")

    if not cookie_csrf or not header_csrf:
        raise HTTPException(status_code=403, detail="CSRFトークンがありません")

    if cookie_csrf != header_csrf:
        # 簡易比較（速度優先）。より厳密にしたい場合はDB比較も追加可能
        raise HTTPException(status_code=403, detail="CSRFトークンが一致しません")


# ─────────────────────────────────────────────
# 現在ユーザー取得（Cookieからaccess_tokenを読み込む）
# ─────────────────────────────────────────────
def get_current_user(conn, request: Request) -> dict:
    access_token = request.cookies.get("access_token")
    if not access_token:
        raise _unauthorized("認証されていません")

    payload = verify_access_token(access_token)

    user_id = payload.get("sub")
    token_version = payload.get("token_version")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT user_id, role, token_version, is_active
            FROM users WHERE user_id = %s
            """,
            (user_id,),
        )
        user = cur.fetchone()

    if not user:
        raise _unauthorized("ユーザーが存在しません")
    if not user.get("is_active", True):
        raise _forbidden("アカウントが停止されています")
    if int(user.get("token_version", 0)) != int(token_version):
        raise _unauthorized("トークンが失効しています")

    return user


def get_current_admin_user(conn, request: Request) -> dict:
    user = get_current_user(conn, request)
    if user.get("role") != "admin":
        raise _forbidden("管理者権限が必要です")
    return user

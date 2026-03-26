"""
security.py — JWT認証完全版

今回このファイルに入れている変更はこの範囲だけです。
1. パスワードハッシュ化 / 検証
2. JWT発行 / 検証
3. 現在ユーザー取得
4. 管理者権限チェック
5. token_version による一括失効
6. register / login 用ヘルパー

前提:
- users テーブルに以下のカラムが存在すること
    user_id TEXT PRIMARY KEY
    password_hash TEXT NOT NULL
    role TEXT NOT NULL DEFAULT 'user'
    token_version INTEGER NOT NULL DEFAULT 0
    is_active BOOLEAN NOT NULL DEFAULT TRUE

必要な環境変数:
- JWT_SECRET_KEY
- JWT_ALGORITHM (省略時 HS256)
- JWT_ACCESS_TOKEN_EXPIRE_MINUTES (省略時 60)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from fastapi import HTTPException, Request, status
from jwt import ExpiredSignatureError, InvalidTokenError
from pwdlib import PasswordHash


# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
JST = timezone(timedelta(hours=9))
UTC = timezone.utc

JWT_SECRET_KEY = os.environ["JWT_SECRET_KEY"]
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

password_hasher = PasswordHash.recommended()


# ─────────────────────────────────────────────
# 共通ユーティリティ
# ─────────────────────────────────────────────
def now_utc() -> datetime:
    return datetime.now(UTC)


def now_jst() -> datetime:
    return datetime.now(JST)


def _unauthorized(detail: str = "認証が必要です") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
    )


def _forbidden(detail: str = "権限がありません") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
    )


# ─────────────────────────────────────────────
# パスワード
# ─────────────────────────────────────────────
def hash_password(password: str) -> str:
    if not password:
        raise HTTPException(status_code=400, detail="パスワードは必須です")
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    return password_hasher.verify(password, password_hash)


# ─────────────────────────────────────────────
# JWT
# ─────────────────────────────────────────────
def create_access_token(
    *,
    user_id: str,
    role: str,
    token_version: int,
    expires_delta: Optional[timedelta] = None,
) -> str:
    issued_at = now_utc()
    expire = issued_at + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    payload: dict[str, Any] = {
        "sub": user_id,
        "role": role,
        "token_version": token_version,
        "iat": issued_at,
        "exp": expire,
    }

    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "exp", "iat", "token_version", "role"]},
        )
        return payload
    except ExpiredSignatureError as exc:
        raise _unauthorized("トークンの有効期限が切れています") from exc
    except InvalidTokenError as exc:
        raise _unauthorized("トークンが無効です") from exc


def get_bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise _unauthorized("Bearerトークンが必要です")
    token = auth[7:].strip()
    if not token:
        raise _unauthorized("Bearerトークンが空です")
    return token


# ─────────────────────────────────────────────
# ユーザー取得 / 権限
# ─────────────────────────────────────────────
def get_current_user(conn, request: Request) -> dict[str, Any]:
    token = get_bearer_token(request)
    payload = decode_access_token(token)

    user_id = payload.get("sub")
    role = payload.get("role")
    token_version = payload.get("token_version")

    if not user_id or role is None or token_version is None:
        raise _unauthorized("トークン内容が不正です")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                user_id,
                password_hash,
                role,
                token_version,
                is_active,
                points,
                free_gacha,
                locked_points,
                post_count
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        user = cur.fetchone()

    if not user:
        raise _unauthorized("ユーザーが存在しません")

    if not user.get("is_active", True):
        raise _forbidden("このアカウントは利用停止中です")

    if int(user.get("token_version", 0)) != int(token_version):
        raise _unauthorized("このトークンは失効しています")

    if user.get("role") != role:
        raise _unauthorized("トークン情報が一致しません")

    return user


def get_current_admin_user(conn, request: Request) -> dict[str, Any]:
    user = get_current_user(conn, request)
    if user.get("role") != "admin":
        raise _forbidden("管理者権限が必要です")
    return user


def require_same_user(current_user: dict[str, Any], target_user_id: str) -> str:
    current_user_id = current_user.get("user_id")
    if current_user_id != target_user_id:
        raise _forbidden("他ユーザーのデータにはアクセスできません")
    return current_user_id


# ─────────────────────────────────────────────
# register / login 用ヘルパー
# ─────────────────────────────────────────────
def register_user(conn, *, user_id: str, password: str, role: str = "user") -> dict[str, Any]:
    user_id = (user_id or "").strip()
    password = password or ""

    if not user_id:
        raise HTTPException(status_code=400, detail="user_idは必須です")

    if len(password) < 4:
        raise HTTPException(status_code=400, detail="パスワードは4文字以上にしてください")

    password_hash = hash_password(password)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        exists = cur.fetchone()
        if exists:
            raise HTTPException(status_code=400, detail="そのuser_idは既に使われています")

        cur.execute(
            """
            INSERT INTO users (
                user_id,
                password_hash,
                role,
                token_version,
                is_active,
                points,
                free_gacha,
                locked_points,
                post_count
            )
            VALUES (%s, %s, %s, 0, TRUE, 0, 0, 0, 0)
            RETURNING user_id, role, token_version, is_active
            """,
            (user_id, password_hash, role),
        )
        created = cur.fetchone()

    return created


def authenticate_user(conn, *, user_id: str, password: str) -> dict[str, Any]:
    user_id = (user_id or "").strip()
    password = password or ""

    if not user_id or not password:
        raise HTTPException(status_code=400, detail="user_idとpasswordは必須です")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                user_id,
                password_hash,
                role,
                token_version,
                is_active,
                points,
                free_gacha,
                locked_points,
                post_count
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        user = cur.fetchone()

    if not user:
        raise _unauthorized("ユーザーIDまたはパスワードが違います")

    if not user.get("is_active", True):
        raise _forbidden("このアカウントは利用停止中です")

    if not verify_password(password, user.get("password_hash", "")):
        raise _unauthorized("ユーザーIDまたはパスワードが違います")

    return user


def issue_login_token(conn, *, user_id: str, password: str) -> dict[str, Any]:
    user = authenticate_user(conn, user_id=user_id, password=password)

    access_token = create_access_token(
        user_id=user["user_id"],
        role=user["role"],
        token_version=int(user.get("token_version", 0)),
    )

    return {
        "user_id": user["user_id"],
        "role": user["role"],
        "access_token": access_token,
        "token_type": "bearer",
    }


# ─────────────────────────────────────────────
# 失効
# ─────────────────────────────────────────────
def revoke_user_tokens(conn, *, user_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE users
            SET token_version = COALESCE(token_version, 0) + 1
            WHERE user_id = %s
            """,
            (user_id,),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="ユーザーが存在しません")


# ─────────────────────────────────────────────
# FastAPIルーター側で使う想定の薄いヘルパー
# ─────────────────────────────────────────────
def get_current_user_id(conn, request: Request) -> str:
    user = get_current_user(conn, request)
    return user["user_id"]


def get_current_admin_user_id(conn, request: Request) -> str:
    user = get_current_admin_user(conn, request)
    return user["user_id"]

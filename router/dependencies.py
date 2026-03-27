from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, status

from db import db_transaction
from security import (
    get_current_admin_user,
    get_current_user,
    verify_csrf_request,
)


# ─────────────────────────────────────────────
# 基本的なユーザー取得依存（トランザクション内で使用）
# ─────────────────────────────────────────────
def get_current_user_dep(
    request: Request,
    conn=Depends(lambda: None),  # 実際はルーター側で db_transaction を使う
) -> dict:
    """Cookieのaccess_tokenから現在のユーザーを取得"""
    return get_current_user(conn, request)


def get_current_admin_user_dep(
    request: Request,
    conn=Depends(lambda: None),
) -> dict:
    """管理者ユーザーのみ許可"""
    return get_current_admin_user(conn, request)


# ─────────────────────────────────────────────
# ユーザーIDだけ欲しい場合（admin.pyで現在使っているパターンに近い）
# ─────────────────────────────────────────────
def get_current_user_id_dep(
    request: Request,
    conn=Depends(lambda: None),
    *,
    require_csrf: bool = False,
) -> str:
    """現在のユーザーIDを返す（CSRF検証オプション付き）"""
    user = get_current_user(conn, request)
    if require_csrf:
        verify_csrf_request(request)
    return user["user_id"]


def get_current_admin_user_id_dep(
    request: Request,
    conn=Depends(lambda: None),
    *,
    require_csrf: bool = False,
) -> str:
    """現在の管理者ユーザーIDを返す（CSRF検証オプション付き）"""
    user = get_current_admin_user(conn, request)
    if require_csrf:
        verify_csrf_request(request)
    return user["user_id"]


# ─────────────────────────────────────────────
# FastAPIのAnnotated型で使いやすく（おすすめの書き方）
# ─────────────────────────────────────────────
CurrentUser = Annotated[dict, Depends(get_current_user_dep)]
CurrentAdminUser = Annotated[dict, Depends(get_current_admin_user_dep)]

CurrentUserId = Annotated[str, Depends(get_current_user_id_dep)]
CurrentAdminUserId = Annotated[str, Depends(get_current_admin_user_id_dep)]


# ─────────────────────────────────────────────
# CSRF必須の書き込み用依存（より明示的にしたい場合）
# ─────────────────────────────────────────────
def require_csrf_and_current_user_id(
    request: Request,
    conn=Depends(lambda: None),
) -> str:
    """書き込みAPIでCSRF + ユーザーIDを同時に検証したい場合に便利"""
    user = get_current_user(conn, request)
    verify_csrf_request(request)
    return user["user_id"]


def require_csrf_and_current_admin_user_id(
    request: Request,
    conn=Depends(lambda: None),
) -> str:
    """管理者向け書き込みAPI用"""
    user = get_current_admin_user(conn, request)
    verify_csrf_request(request)
    return user["user_id"]


# ─────────────────────────────────────────────
# DBトランザクションと組み合わせるためのヘルパー（オプション）
# ─────────────────────────────────────────────
# 注意: FastAPIのDependsでdb_transactionを直接yieldするのは少しトリッキーなので、
# 現在はルーター側で「with db_transaction() as (conn, cur):」のまま使い、
# 内部で上記の *_dep を呼ぶ形を推奨しています。

# 将来的に以下のようなyield依存を作りたい場合は拡張可能です：
# async def get_db_transaction():
#     with db_transaction() as (conn, cur):
#         yield conn, cur

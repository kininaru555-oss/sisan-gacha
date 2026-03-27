from __future__ import annotations

from typing import Annotated, Generator, Tuple

import psycopg
from fastapi import Depends, Request

from db import db_transaction
from security import (
    get_current_admin_user,
    get_current_user,
    verify_csrf_request,
)


# ─────────────────────────────────────────────
# DB依存：yield で接続を提供し、終了時に自動commit/rollback
# ─────────────────────────────────────────────
def get_db() -> Generator:
    """
    FastAPI Depends 用のDB接続プロバイダ。
    with db_transaction() をyieldで包み、
    ルーター関数が終わった後に自動的にcommit/rollbackする。

    使用例：
        @router.get("/foo")
        def foo(db: DbConn):
            conn, cur = db
            cur.execute(...)
    """
    with db_transaction() as (conn, cur):
        yield conn, cur


# Annotated型エイリアス（ルーターで型ヒントとして使う）
DbConn = Annotated[Tuple[psycopg.Connection, psycopg.Cursor], Depends(get_db)]


# ─────────────────────────────────────────────
# ユーザー取得依存
# ─────────────────────────────────────────────
def get_current_user_dep(
    request: Request,
    db: DbConn,
) -> dict:
    """Cookieのaccess_tokenから現在のユーザーを取得"""
    conn, _ = db
    return get_current_user(conn, request)


def get_current_admin_user_dep(
    request: Request,
    db: DbConn,
) -> dict:
    """管理者ユーザーのみ許可"""
    conn, _ = db
    return get_current_admin_user(conn, request)


# ─────────────────────────────────────────────
# ユーザーIDだけ欲しい場合
# ─────────────────────────────────────────────
def get_current_user_id_dep(
    request: Request,
    db: DbConn,
) -> str:
    """現在のユーザーIDを返す（CSRF検証なし）"""
    conn, _ = db
    user = get_current_user(conn, request)
    return user["user_id"]


def get_current_admin_user_id_dep(
    request: Request,
    db: DbConn,
) -> str:
    """現在の管理者ユーザーIDを返す（CSRF検証なし）"""
    conn, _ = db
    user = get_current_admin_user(conn, request)
    return user["user_id"]


# ─────────────────────────────────────────────
# CSRF必須の書き込み用依存
# ─────────────────────────────────────────────
def require_user_with_csrf(
    request: Request,
    db: DbConn,
) -> str:
    """書き込みAPI用：CSRF検証 + ユーザーID返却"""
    conn, _ = db
    verify_csrf_request(request)
    user = get_current_user(conn, request)
    return user["user_id"]


def require_admin_with_csrf(
    request: Request,
    db: DbConn,
) -> str:
    """管理者向け書き込みAPI用：CSRF検証 + 管理者ID返却"""
    conn, _ = db
    verify_csrf_request(request)
    user = get_current_admin_user(conn, request)
    return user["user_id"]


# ─────────────────────────────────────────────
# Annotated型エイリアス（ルーターで型ヒントとして使う）
# ─────────────────────────────────────────────

# 読み取り系（CSRF不要）
CurrentUser      = Annotated[dict, Depends(get_current_user_dep)]
CurrentAdminUser = Annotated[dict, Depends(get_current_admin_user_dep)]
CurrentUserId    = Annotated[str,  Depends(get_current_user_id_dep)]
CurrentAdminUserId = Annotated[str, Depends(get_current_admin_user_id_dep)]

# 書き込み系（CSRF必須）
CurrentUserIdWithCsrf      = Annotated[str, Depends(require_user_with_csrf)]
CurrentAdminUserIdWithCsrf = Annotated[str, Depends(require_admin_with_csrf)]


# ─────────────────────────────────────────────
# 使用例（コメント）
# ─────────────────────────────────────────────
# 【読み取りAPI（CSRF不要）】
#   @router.get("/mypage")
#   def mypage(user_id: CurrentUserId):
#       ...
#
# 【書き込みAPI（CSRF必須）】
#   @router.post("/gacha")
#   def gacha(user_id: CurrentUserIdWithCsrf, db: DbConn):
#       conn, cur = db
#       cur.execute(...)
#
# 【現在の with db_transaction() パターンとの共存】
#   既存ルーターはそのまま動く。
#   新規ルーターから順次 Annotated 型に移行可能。

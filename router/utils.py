"""
utils.py — 共通ユーティリティ関数

現在残している関数:
- ensure_user_row_exists（ユーザー行の保証）
- now_iso（ISO形式の日時取得）
- client_ip（クライアントIP取得）

※ 認証関連の関数はすべて dependencies.py に移動済み
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import Request


def ensure_user_row_exists(cur, user_id: str) -> None:
    """
    ユーザーレコードが存在しない場合にデフォルト行を挿入する
    
    ポイント、無料ガチャなどの初期値を保証するために使用
    """
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
    """
    現在のUTC時刻をISO 8601形式の文字列で返す
    （ガチャログや作成日時などに使用）
    """
    return datetime.utcnow().isoformat()


def client_ip(request: Request) -> Optional[str]:
    """
    クライアントのIPアドレスを取得
    
    X-Forwarded-For ヘッダーを優先（リバースプロキシ対応）
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    
    return request.client.host if request.client else None


# ─────────────────────────────────────────────
# 将来的に追加する可能性のあるヘルパー（コメントアウト）
# ─────────────────────────────────────────────
# def sanitize_html(text: str) -> str:
#     """XSS対策用HTMLサニタイズ（bleach推奨）"""
#     pass
#
# def validate_url(url: Optional[str]) -> Optional[str]:
#     """投稿時のURL検証（許可ドメイン制限など）"""
#     pass

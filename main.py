"""
main.py — FastAPI アプリケーションエントリポイント（本番対応版）

・CORS対応（フロントエンドとの連携を考慮）
・セキュリティミドルウェア
・エラーハンドリング
・起動時初期化（DB）
・静的ファイル配信（SPA対応）
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from init_db import init_db
from routers.admin import router as admin_router
from routers.auth import router as auth_router
from routers.bundles import router as bundles_router
from routers.gacha import router as gacha_router
from routers.mypage import router as mypage_router
from routers.prompts import router as prompts_router
from routers.stripe_api import router as stripe_router

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# Lifespan（起動時・終了時の処理）
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動時処理
    logger.info("アプリケーション起動中...")
    try:
        init_db()  # DBテーブル初期化（本番では idempotent になっているので安全）
        logger.info("DB初期化完了")
    except Exception as e:
        logger.error(f"DB初期化エラー: {e}")
        # 必要に応じて raise で起動失敗にすることも可能
    yield
    # 終了時処理（クリーンアップが必要ならここに記述）
    logger.info("アプリケーション終了")


# FastAPIアプリ作成
app = FastAPI(
    title="Prompt Gacha API",
    description="プロンプトガチャ + 福袋 + クリエイター収益分配システム",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",           # 本番では /docs を無効化したい場合は None に
    redoc_url="/redoc",
)


# ─────────────────────────────────────────────
# ミドルウェア設定（重要：順序に注意）
# ─────────────────────────────────────────────

# 1. Trusted Host（Host Header Attack対策）- 本番では必ず設定
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[
        "localhost",
        "127.0.0.1",
        "*.yourdomain.com",          # 実際のドメインに変更
        "yourdomain.com",
    ],
)

# 2. CORS（フロントエンドとの連携用）- 仕様書に合わせ credentials=True
allowed_origins = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,https://yourdomain.com"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,          # 本番では ["*"] は絶対に使わない
    allow_credentials=True,                 # Cookie（access_token, refresh_token, csrf_token）を使うため必須
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-CSRF-Token",                     # 仕様書のCSRFヘッダー
        "X-Requested-With",
    ],
    expose_headers=["X-Process-Time"],      # 任意：処理時間をフロントに公開
    max_age=3600,                           # preflightキャッシュ
)

# 3. GZip圧縮（レスポンスサイズ削減）
app.add_middleware(GZipMiddleware, minimum_size=1000)

# （オプション）その他ミドルウェア例：
# - Process Time Header
# - Security Headers（StarletteのSecurityMiddleware）
# - Rate Limiting（SlowAPIなど）


# ─────────────────────────────────────────────
# グローバル例外ハンドリング
# ─────────────────────────────────────────────
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        raise exc

    logger.error(f"未処理例外が発生: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "サーバー内部エラーが発生しました。しばらくお待ちください。"}
    )


# ─────────────────────────────────────────────
# ルーター登録
# ─────────────────────────────────────────────
app.include_router(auth_router, prefix="/api")
app.include_router(prompts_router, prefix="/api")
app.include_router(gacha_router, prefix="/api")
app.include_router(stripe_router, prefix="/api")
app.include_router(mypage_router, prefix="/api")
app.include_router(bundles_router, prefix="/api")
app.include_router(admin_router, prefix="/api")


# ─────────────────────────────────────────────
# 静的ファイル + SPA対応（フロントエンドのindex.htmlを配信）
# ─────────────────────────────────────────────
# 注意: 静的ファイルは最後にマウント（ルーターより後に）
app.mount("/", StaticFiles(directory="static", html=True), name="static")


# ─────────────────────────────────────────────
# ヘルスチェック（ロードバランサー用）
# ─────────────────────────────────────────────
@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": app.version}


@app.get("/ready")
async def readiness_check():
    """DB接続確認などが必要ならここに拡張"""
    return {"status": "ready"}


# 起動時のメッセージ
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,           # 開発時のみ True、本番は False
        log_level="info",
    )

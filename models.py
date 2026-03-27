"""
models.py — Pydanticモデル定義（バリデーション強化版）

・フィールド長制限の追加（XSS/パフォーマンス対策）
・必須項目の明確化
・レスポンス用モデルも追加推奨（将来的に）
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────
# 認証関連
# ─────────────────────────────────────────────
class RegisterRequest(BaseModel):
    user_id: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$")
    password: str = Field(..., min_length=4, max_length=128)

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        return v.strip().lower()


class LoginRequest(BaseModel):
    user_id: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=1, max_length=128)


# ─────────────────────────────────────────────
# プロンプト（記事）関連
# ─────────────────────────────────────────────
class CreatePromptRequest(BaseModel):
    """記事投稿リクエスト（仕様書準拠）"""
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=5000)   # 必要に応じて調整
    category: str = Field(..., min_length=1, max_length=50)
    url: Optional[str] = Field(None, max_length=500)
    bundle_consent: bool = Field(True, description="福袋への提供に同意する")

    @field_validator("title", "content", "category")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: Optional[str]) -> Optional[str]:
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("URLは http:// または https:// で始まる必要があります")
        return v


class UpdatePromptRequest(BaseModel):
    """記事更新リクエスト（将来的に使用）"""
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    content: Optional[str] = Field(None, min_length=1, max_length=5000)
    category: Optional[str] = Field(None, min_length=1, max_length=50)
    url: Optional[str] = Field(None, max_length=500)


# ─────────────────────────────────────────────
# ガチャ関連
# ─────────────────────────────────────────────
class GachaRequest(BaseModel):
    """ガチャ実行リクエスト（現在は空でもOK）"""
    category: Optional[str] = Field(None, max_length=50)


# ─────────────────────────────────────────────
# 福袋（Bundle）関連
# ─────────────────────────────────────────────
class CreateBundleRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    target_article_count: int = Field(..., ge=1, le=50)
    genre: str = Field(..., min_length=1, max_length=50)
    price_points: int = Field(..., ge=10, le=10000)


class AddBundleItemRequest(BaseModel):
    bundle_id: int = Field(..., gt=0)
    prompt_id: int = Field(..., gt=0)


class PublishBundleRequest(BaseModel):
    bundle_id: int = Field(..., gt=0)


class CloseBundleRequest(BaseModel):
    bundle_id: int = Field(..., gt=0)


class BuyBundleRequest(BaseModel):
    bundle_id: int = Field(..., gt=0)


class DistributeBundleRequest(BaseModel):
    bundle_id: int = Field(..., gt=0)
    distribution_round: int = Field(1, ge=1)


# ─────────────────────────────────────────────
# 出金・課金関連
# ─────────────────────────────────────────────
class CreateCheckoutSessionRequest(BaseModel):
    product_code: str = Field(..., min_length=1, max_length=50)


class CreateWithdrawalRequest(BaseModel):
    amount_yen: int = Field(..., ge=1000, le=1000000)   # 最低1000円〜最大100万円程度
    method: str = Field(..., pattern=r"^(paypay|amazon_gift)$")
    destination: str = Field(..., min_length=1, max_length=200)
    # withdraw_code はサーバー側で生成するので不要かも


class ProcessWithdrawRequest(BaseModel):
    status: str = Field(..., pattern=r"^(approved|paid|rejected)$")
    admin_note: Optional[str] = Field(None, max_length=500)


# ─────────────────────────────────────────────
# プロンプト停止申請関連
# ─────────────────────────────────────────────
class PromptStopRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)


class ProcessPromptStopRequest(BaseModel):
    status: str = Field(..., pattern=r"^(approved|rejected)$")
    admin_note: Optional[str] = Field(None, max_length=500)


# ─────────────────────────────────────────────
# その他（将来的に使用）
# ─────────────────────────────────────────────
class TogglePromptFlagRequest(BaseModel):
    """プロンプトの各種フラグ切り替え（将来的に使用）"""
    enabled: bool


# レスポンス用モデル例（任意で追加可能）
class PromptResponse(BaseModel):
    id: int
    title: str
    category: str
    created_at: str

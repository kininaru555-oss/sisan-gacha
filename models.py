from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class RegisterRequest(BaseModel):
    user_id: str
    password: str


class LoginRequest(BaseModel):
    user_id: str
    password: str


class CreatePromptRequest(BaseModel):
    title: str
    content: str
    category: str
    url: Optional[str] = None
    bundle_consent: bool


class GachaRequest(BaseModel):
    category: Optional[str] = None


class CreateCheckoutSessionRequest(BaseModel):
    product_code: str


class CreateWithdrawalRequest(BaseModel):
    amount_yen: int
    method: str
    destination: str
    withdraw_code: str


class CreateBundleRequest(BaseModel):
    title: str
    description: Optional[str] = None
    target_article_count: int
    genre: str
    price_points: int


class AddBundleItemRequest(BaseModel):
    bundle_id: int
    prompt_id: int


class BundleEntryRequest(BaseModel):
    bundle_id: int
    prompt_id: int


class PublishBundleRequest(BaseModel):
    bundle_id: int


class BuyBundleRequest(BaseModel):
    bundle_id: int


class DistributeBundleRequest(BaseModel):
    bundle_id: int
    distribution_round: int = 1


class TogglePromptFlagRequest(BaseModel):
    enabled: bool


class PromptStopRequest(BaseModel):
    reason: Optional[str] = None


class UpdatePromptRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    url: Optional[str] = None


class ProcessPromptStopRequest(BaseModel):
    status: str
    admin_note: Optional[str] = None


class ProcessWithdrawRequest(BaseModel):
    status: str
    admin_note: Optional[str] = None

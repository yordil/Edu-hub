from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class SubscriptionPublic(BaseModel):
    id: str
    plan: str
    status: str
    current_period_start: datetime
    current_period_end: datetime
    created_at: datetime


class PaymentInitRequest(BaseModel):
    plan: str = "pro"


class PaymentInitResponse(BaseModel):
    checkout_url: str
    tx_ref: str


class TransactionPublic(BaseModel):
    id: str
    tx_ref: str
    amount: Decimal
    currency: str
    status: str
    payment_method: str | None
    created_at: datetime


class BillingOverview(BaseModel):
    subscription: SubscriptionPublic | None
    transactions: list[TransactionPublic]

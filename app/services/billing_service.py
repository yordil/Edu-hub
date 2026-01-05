import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.billing import (
    PaymentStatus,
    PaymentTransaction,
    PlanTier,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import User

CHAPA_BASE = "https://api.chapa.co/v1"

PLAN_PRICES: dict[str, Decimal] = {
    "pro": Decimal("999.00"),
}


def _chapa_headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.chapa_secret_key}",
        "Content-Type": "application/json",
    }


async def get_active_subscription(
    db: AsyncSession, org_id: uuid.UUID
) -> Subscription | None:
    return await db.scalar(
        select(Subscription)
        .where(
            Subscription.org_id == org_id,
            Subscription.status == SubscriptionStatus.active,
            Subscription.current_period_end > datetime.now(timezone.utc),
        )
        .order_by(Subscription.current_period_end.desc())
    )


async def get_subscription_for_org(
    db: AsyncSession, org_id: uuid.UUID
) -> Subscription | None:
    return await db.scalar(
        select(Subscription)
        .where(Subscription.org_id == org_id)
        .order_by(Subscription.created_at.desc())
    )


async def initialize_payment(
    db: AsyncSession, user: User, plan: str = "pro"
) -> dict:
    settings = get_settings()
    if not settings.chapa_secret_key:
        raise ValueError("Chapa is not configured")

    amount = PLAN_PRICES.get(plan)
    if amount is None:
        raise ValueError(f"Unknown plan: {plan}")

    tx_ref = f"eduhub-{user.org_id}-{uuid.uuid4().hex[:12]}"

    tx = PaymentTransaction(
        org_id=user.org_id,
        initiated_by_id=user.id,
        tx_ref=tx_ref,
        amount=amount,
        currency="ETB",
        status=PaymentStatus.pending,
    )
    db.add(tx)
    await db.flush()

    callback_url = f"{settings.api_public_url}/api/v1/billing/webhook"
    return_url = f"{settings.frontend_url}/billing/callback?tx_ref={tx_ref}"

    name_parts = user.full_name.split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    payload = {
        "amount": str(amount),
        "currency": "ETB",
        "email": user.email,
        "first_name": first_name,
        "last_name": last_name,
        "tx_ref": tx_ref,
        "callback_url": callback_url,
        "return_url": return_url,
        "customization": {
            "title": "EduHub Pro",
            "description": f"Pro plan for {user.organization.name}",
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{CHAPA_BASE}/transaction/initialize",
            json=payload,
            headers=_chapa_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("status") != "success":
        tx.status = PaymentStatus.failed
        tx.failure_reason = data.get("message", "Chapa initialization failed")
        await db.commit()
        raise ValueError(tx.failure_reason)

    await db.commit()
    return {
        "checkout_url": data["data"]["checkout_url"],
        "tx_ref": tx_ref,
    }


async def verify_transaction(db: AsyncSession, tx_ref: str) -> PaymentTransaction:
    # The browser redirect and the webhook can arrive together. Locking the row makes the
    # loser of that race wait, so it observes `success` below instead of settling again.
    tx = await db.scalar(
        select(PaymentTransaction)
        .where(PaymentTransaction.tx_ref == tx_ref)
        .with_for_update()
    )
    if tx is None:
        raise ValueError("Transaction not found")

    # Settling twice would extend the period twice for one payment.
    if tx.status == PaymentStatus.success:
        return tx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{CHAPA_BASE}/transaction/verify/{tx_ref}",
            headers=_chapa_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("status") != "success":
        tx.status = PaymentStatus.failed
        tx.failure_reason = data.get("message", "Verification failed")
        await db.commit()
        return tx

    body = data.get("data") or {}
    # The checkout page takes the amount from the caller, so confirm what actually
    # settled covers what this transaction was opened for.
    try:
        paid = Decimal(str(body.get("amount")))
    except (TypeError, ArithmeticError):
        paid = Decimal(0)
    if paid < tx.amount or (body.get("currency") or tx.currency) != tx.currency:
        tx.status = PaymentStatus.failed
        tx.failure_reason = f"Amount mismatch: settled {paid} {body.get('currency')}, expected {tx.amount} {tx.currency}"
        await db.commit()
        return tx

    tx.status = PaymentStatus.success
    tx.chapa_tx_id = body.get("reference")
    tx.payment_method = body.get("payment_type")
    await _activate_subscription(db, tx)
    await db.commit()
    return tx


def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """Reject anything we cannot positively authenticate: a missing signature, or a
    missing secret, is a failure rather than a bypass."""
    settings = get_settings()
    if not settings.chapa_webhook_secret or not signature:
        return False
    expected = hmac.new(
        settings.chapa_webhook_secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def handle_webhook(db: AsyncSession, tx_ref: str) -> None:
    """A webhook only tells us *which* transaction moved. What it moved to is read back
    from Chapa, so a replayed or forged body can never settle a payment by itself."""
    try:
        await verify_transaction(db, tx_ref)
    except ValueError:
        # Unknown tx_ref: not ours, or already pruned. Nothing to settle.
        return


async def _activate_subscription(
    db: AsyncSession, tx: PaymentTransaction
) -> Subscription:
    now = datetime.now(timezone.utc)
    existing = await get_active_subscription(db, tx.org_id)

    if existing:
        existing.current_period_end = max(
            existing.current_period_end, now
        ) + timedelta(days=30)
        existing.updated_at = now
        tx.subscription_id = existing.id
        return existing

    sub = Subscription(
        org_id=tx.org_id,
        plan=PlanTier.pro,
        status=SubscriptionStatus.active,
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
    )
    db.add(sub)
    await db.flush()
    tx.subscription_id = sub.id
    return sub


async def get_transactions(
    db: AsyncSession, org_id: uuid.UUID, limit: int = 20
) -> list[PaymentTransaction]:
    result = await db.scalars(
        select(PaymentTransaction)
        .where(PaymentTransaction.org_id == org_id)
        .order_by(PaymentTransaction.created_at.desc())
        .limit(limit)
    )
    return list(result)

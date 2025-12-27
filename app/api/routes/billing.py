import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import AdminUser, CurrentUser, DbSession, StaffUser
from app.schemas.billing import (
    BillingOverview,
    PaymentInitRequest,
    PaymentInitResponse,
    SubscriptionPublic,
    TransactionPublic,
)
from app.services.billing_service import (
    get_subscription_for_org,
    get_transactions,
    handle_webhook,
    initialize_payment,
    verify_transaction,
    verify_webhook_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


def _sub_to_public(sub) -> SubscriptionPublic:
    return SubscriptionPublic(
        id=str(sub.id),
        plan=sub.plan.value,
        status=sub.status.value,
        current_period_start=sub.current_period_start,
        current_period_end=sub.current_period_end,
        created_at=sub.created_at,
    )


def _tx_to_public(tx) -> TransactionPublic:
    return TransactionPublic(
        id=str(tx.id),
        tx_ref=tx.tx_ref,
        amount=tx.amount,
        currency=tx.currency,
        status=tx.status.value,
        payment_method=tx.payment_method,
        created_at=tx.created_at,
    )


@router.get("/subscription")
async def get_subscription(user: StaffUser, db: DbSession) -> BillingOverview:
    sub = await get_subscription_for_org(db, user.org_id)
    txns = await get_transactions(db, user.org_id, limit=10)
    return BillingOverview(
        subscription=_sub_to_public(sub) if sub else None,
        transactions=[_tx_to_public(t) for t in txns],
    )


@router.post("/initialize")
async def init_payment(
    body: PaymentInitRequest, user: AdminUser, db: DbSession
) -> PaymentInitResponse:
    try:
        result = await initialize_payment(db, user, body.plan)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))
    return PaymentInitResponse(**result)


@router.get("/verify/{tx_ref}")
async def verify(tx_ref: str, user: CurrentUser, db: DbSession) -> TransactionPublic:
    try:
        tx = await verify_transaction(db, tx_ref)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e))
    if tx.org_id != user.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Transaction not found")
    return _tx_to_public(tx)


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def webhook(request: Request, db: DbSession):
    """Chapa calls this when a payment settles. The body is treated as an untrusted
    notification: it names a tx_ref, and the amount owed is confirmed with Chapa directly."""
    body = await request.body()
    # Chapa sends "Chapa-Signature"; "x-chapa-signature" appears on some account types.
    signature = request.headers.get("Chapa-Signature") or request.headers.get("x-chapa-signature") or ""

    if not verify_webhook_signature(body, signature):
        logger.warning("Rejected webhook with missing or invalid signature")
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from None

    tx_ref = data.get("tx_ref") or data.get("trx_ref")
    if not tx_ref:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Missing tx_ref")

    await handle_webhook(db, tx_ref)
    return {"status": "ok"}

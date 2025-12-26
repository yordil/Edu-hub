"""Billing end to end: plan gating, Chapa checkout, settlement by redirect and webhook,
and the forgery/replay cases a payment path has to refuse."""

import hashlib
import hmac
import json

import httpx
import pytest

from app.ai.llm import text
from app.core.config import get_settings
from app.services import billing_service
from tests.conftest import org_id_of, subscribe
from tests.test_papers import published_paper  # noqa: F401
from tests.test_tutor import fake_llm  # noqa: F401

BILLING = "/api/v1/billing"
SECRET = "test-webhook-secret"
CHECKOUT = "https://checkout.chapa.co/checkout/payment/abc123"


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class Chapa:
    """Stands in for Chapa. `settle` is what /transaction/verify will report next."""

    def __init__(self):
        self.settle: dict = {"status": "success", "data": {}}
        self.init_status = "success"
        self.initialized: list[dict] = []
        self.verified: list[str] = []

    def client(self, service):
        chapa = self

        class FakeClient:
            def __init__(self, *_, **__):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def post(self, url, json=None, headers=None):
                chapa.initialized.append(json or {})
                if chapa.init_status != "success":
                    return FakeResponse({"status": "failed", "message": "Chapa said no"})
                return FakeResponse({"status": "success", "data": {"checkout_url": CHECKOUT}})

            async def get(self, url, headers=None):
                chapa.verified.append(url.rsplit("/", 1)[-1])
                return FakeResponse(chapa.settle)

        return FakeClient


@pytest.fixture(autouse=True)
def no_trial(app, monkeypatch):
    """These tests are about the paid plan, so the free tutor trial is switched off.
    The trial has its own tests in test_tutor_trial.py."""
    monkeypatch.setattr(get_settings(), "ai_free_trial_tokens", 0)


@pytest.fixture
def chapa(monkeypatch):
    c = Chapa()
    monkeypatch.setattr(get_settings(), "chapa_secret_key", "CHASECK_TEST-fake")
    monkeypatch.setattr(get_settings(), "chapa_webhook_secret", SECRET)
    monkeypatch.setattr(httpx, "AsyncClient", c.client(billing_service))
    return c


def settled(amount="999.00", currency="ETB", **extra):
    data = {"amount": amount, "currency": currency, "reference": "chapa-ref-1", "payment_type": "telebirr"}
    data.update(extra)
    return {"status": "success", "data": data}


def post_webhook(client, body: dict, secret: str | None = SECRET, header="Chapa-Signature"):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers[header] = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(f"{BILLING}/webhook", content=raw, headers=headers)


def start_checkout(client, school):
    r = client.post(f"{BILLING}/initialize", json={"plan": "pro"}, headers=school["admin"])
    assert r.status_code == 200, r.text
    return r.json()["tx_ref"]


def chat(client, headers, fake=None):
    """Ask the tutor one question. `fake` scripts a reply for the calls expected to get through."""
    if fake is not None:
        fake.script = [text("Start by factorising both sides.")]
    return client.post("/api/v1/tutor/chat", json={"conversation_id": None, "message": "Help me with algebra"}, headers=headers)


# ---------- gating ----------


def test_unpaid_school_cannot_use_the_tutor(client, unpaid_school, fake_llm):  # noqa: F811
    r = chat(client, unpaid_school["student"])
    assert r.status_code == 403
    assert "subscription" in r.json()["detail"].lower()


def test_paid_school_can_use_the_tutor(client, school, fake_llm):  # noqa: F811
    assert chat(client, school["student"], fake_llm).status_code == 200


def test_unpaid_school_cannot_sit_a_past_paper(client, unpaid_school, database_url):
    subscribe(database_url, org_id_of(client, unpaid_school["admin"]))
    paper = published_paper(client, unpaid_school)
    _expire(client, unpaid_school, database_url)
    r = client.post(f"/api/v1/papers/{paper['id']}/start", headers=unpaid_school["student"])
    assert r.status_code == 403


def _expire(client, school, database_url):
    """Wind the org's plan into the past without touching its content."""
    import asyncio

    from sqlalchemy import pool, text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run():
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE subscriptions SET current_period_end = now() - interval '1 day' WHERE org_id = :org"),
                {"org": org_id_of(client, school["admin"])},
            )
        await engine.dispose()

    asyncio.run(run())


def test_expired_plan_is_refused(client, school, database_url, fake_llm):  # noqa: F811
    assert chat(client, school["student"], fake_llm).status_code == 200
    _expire(client, school, database_url)
    assert chat(client, school["student"], fake_llm).status_code == 403


def test_free_features_stay_open_without_a_plan(client, unpaid_school):
    for path in ("/api/v1/learning/mastery", "/api/v1/attempts?limit=5", "/api/v1/announcements?limit=3"):
        assert client.get(path, headers=unpaid_school["student"]).status_code == 200, path


def test_tutor_history_is_readable_without_a_plan(client, unpaid_school):
    """Losing the plan must not hide work a school already paid for."""
    assert client.get("/api/v1/tutor/conversations", headers=unpaid_school["student"]).status_code == 200
    assert client.get("/api/v1/tutor/memories", headers=unpaid_school["student"]).status_code == 200


# ---------- reading the subscription ----------


def test_overview_is_empty_before_any_payment(client, unpaid_school):
    r = client.get(f"{BILLING}/subscription", headers=unpaid_school["admin"])
    assert r.status_code == 200, r.text
    assert r.json() == {"subscription": None, "transactions": []}


def test_overview_reports_the_active_plan(client, school):
    r = client.get(f"{BILLING}/subscription", headers=school["admin"])
    body = r.json()["subscription"]
    assert (body["plan"], body["status"]) == ("pro", "active")
    assert body["current_period_end"] > body["current_period_start"]


def test_students_cannot_read_school_billing(client, school):
    assert client.get(f"{BILLING}/subscription", headers=school["student"]).status_code == 403


# ---------- opening a checkout ----------


def test_admin_gets_a_checkout_url(client, unpaid_school, chapa):
    r = client.post(f"{BILLING}/initialize", json={"plan": "pro"}, headers=unpaid_school["admin"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkout_url"] == CHECKOUT
    assert body["tx_ref"].startswith("eduhub-")

    sent = chapa.initialized[0]
    assert (sent["amount"], sent["currency"]) == ("999.00", "ETB")
    assert sent["tx_ref"] == body["tx_ref"]
    assert sent["return_url"].endswith(f"tx_ref={body['tx_ref']}")


def test_checkout_records_a_pending_transaction(client, unpaid_school, chapa):
    tx_ref = start_checkout(client, unpaid_school)
    txns = client.get(f"{BILLING}/subscription", headers=unpaid_school["admin"]).json()["transactions"]
    assert len(txns) == 1
    assert (txns[0]["tx_ref"], txns[0]["status"], txns[0]["currency"]) == (tx_ref, "pending", "ETB")
    assert txns[0]["amount"] == "999.00"


def test_teachers_cannot_open_a_checkout(client, unpaid_school, chapa):
    assert client.post(f"{BILLING}/initialize", json={"plan": "pro"}, headers=unpaid_school["teacher"]).status_code == 403


def test_students_cannot_open_a_checkout(client, unpaid_school, chapa):
    assert client.post(f"{BILLING}/initialize", json={"plan": "pro"}, headers=unpaid_school["student"]).status_code == 403


def test_unknown_plan_is_rejected(client, unpaid_school, chapa):
    r = client.post(f"{BILLING}/initialize", json={"plan": "platinum"}, headers=unpaid_school["admin"])
    assert r.status_code == 400
    assert "platinum" in r.json()["detail"]


def test_checkout_fails_cleanly_when_chapa_is_unconfigured(client, unpaid_school, monkeypatch):
    monkeypatch.setattr(get_settings(), "chapa_secret_key", None)
    r = client.post(f"{BILLING}/initialize", json={"plan": "pro"}, headers=unpaid_school["admin"])
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


def test_a_rejected_checkout_leaves_a_failed_transaction(client, unpaid_school, chapa):
    chapa.init_status = "failed"
    assert client.post(f"{BILLING}/initialize", json={"plan": "pro"}, headers=unpaid_school["admin"]).status_code == 400
    txns = client.get(f"{BILLING}/subscription", headers=unpaid_school["admin"]).json()["transactions"]
    assert txns[0]["status"] == "failed"


# ---------- settling by redirect ----------


def test_verify_settles_the_payment_and_unlocks_the_tutor(client, unpaid_school, chapa, fake_llm):  # noqa: F811
    tx_ref = start_checkout(client, unpaid_school)
    assert chat(client, unpaid_school["student"]).status_code == 403

    chapa.settle = settled()
    r = client.get(f"{BILLING}/verify/{tx_ref}", headers=unpaid_school["admin"])
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["payment_method"]) == ("success", "telebirr")

    assert chat(client, unpaid_school["student"], fake_llm).status_code == 200
    sub = client.get(f"{BILLING}/subscription", headers=unpaid_school["admin"]).json()["subscription"]
    assert (sub["plan"], sub["status"]) == ("pro", "active")


def test_verify_is_idempotent(client, unpaid_school, chapa):
    tx_ref = start_checkout(client, unpaid_school)
    chapa.settle = settled()
    first = client.get(f"{BILLING}/verify/{tx_ref}", headers=unpaid_school["admin"]).json()
    end_after_first = client.get(f"{BILLING}/subscription", headers=unpaid_school["admin"]).json()["subscription"]["current_period_end"]

    second = client.get(f"{BILLING}/verify/{tx_ref}", headers=unpaid_school["admin"]).json()
    end_after_second = client.get(f"{BILLING}/subscription", headers=unpaid_school["admin"]).json()["subscription"]["current_period_end"]

    assert first == second
    assert end_after_first == end_after_second, "one payment must not extend the plan twice"


def test_underpayment_does_not_unlock_the_plan(client, unpaid_school, chapa, fake_llm):  # noqa: F811
    tx_ref = start_checkout(client, unpaid_school)
    chapa.settle = settled(amount="1.00")

    r = client.get(f"{BILLING}/verify/{tx_ref}", headers=unpaid_school["admin"])
    assert r.json()["status"] == "failed"
    assert chat(client, unpaid_school["student"]).status_code == 403
    assert client.get(f"{BILLING}/subscription", headers=unpaid_school["admin"]).json()["subscription"] is None


def test_a_wrong_currency_does_not_unlock_the_plan(client, unpaid_school, chapa):
    tx_ref = start_checkout(client, unpaid_school)
    chapa.settle = settled(amount="999.00", currency="USD")
    assert client.get(f"{BILLING}/verify/{tx_ref}", headers=unpaid_school["admin"]).json()["status"] == "failed"


def test_a_failed_payment_stays_failed(client, unpaid_school, chapa, fake_llm):  # noqa: F811
    tx_ref = start_checkout(client, unpaid_school)
    chapa.settle = {"status": "failed", "message": "Insufficient funds"}
    assert client.get(f"{BILLING}/verify/{tx_ref}", headers=unpaid_school["admin"]).json()["status"] == "failed"
    assert chat(client, unpaid_school["student"]).status_code == 403


def test_another_schools_transaction_is_not_visible(client, school, unpaid_school, chapa):
    tx_ref = start_checkout(client, unpaid_school)
    chapa.settle = settled()
    assert client.get(f"{BILLING}/verify/{tx_ref}", headers=school["admin"]).status_code == 404


def test_an_unknown_reference_is_not_found(client, school, chapa):
    assert client.get(f"{BILLING}/verify/eduhub-nope-000", headers=school["admin"]).status_code == 404


# ---------- settling by webhook ----------


def test_signed_webhook_settles_the_payment(client, unpaid_school, chapa, fake_llm):  # noqa: F811
    tx_ref = start_checkout(client, unpaid_school)
    chapa.settle = settled()

    assert post_webhook(client, {"tx_ref": tx_ref, "status": "success"}).status_code == 200
    assert chat(client, unpaid_school["student"], fake_llm).status_code == 200


def test_webhook_without_a_signature_is_refused(client, unpaid_school, chapa, fake_llm):  # noqa: F811
    tx_ref = start_checkout(client, unpaid_school)
    chapa.settle = settled()

    r = post_webhook(client, {"tx_ref": tx_ref, "status": "success"}, secret=None)
    assert r.status_code == 403
    assert chat(client, unpaid_school["student"]).status_code == 403


def test_webhook_with_a_forged_signature_is_refused(client, unpaid_school, chapa, fake_llm):  # noqa: F811
    tx_ref = start_checkout(client, unpaid_school)
    chapa.settle = settled()

    r = post_webhook(client, {"tx_ref": tx_ref, "status": "success"}, secret="not-the-secret")
    assert r.status_code == 403
    assert chat(client, unpaid_school["student"]).status_code == 403


def test_the_lowercase_signature_header_is_accepted(client, unpaid_school, chapa):
    tx_ref = start_checkout(client, unpaid_school)
    chapa.settle = settled()
    assert post_webhook(client, {"tx_ref": tx_ref}, header="x-chapa-signature").status_code == 200


def test_a_signed_webhook_claiming_success_cannot_override_chapa(client, unpaid_school, chapa, fake_llm):  # noqa: F811
    """The body is only a pointer. If Chapa says the money never arrived, nothing unlocks —
    even for a caller who holds the webhook secret."""
    tx_ref = start_checkout(client, unpaid_school)
    chapa.settle = {"status": "failed", "message": "never paid"}

    assert post_webhook(client, {"tx_ref": tx_ref, "status": "success", "amount": "999.00"}).status_code == 200
    assert chat(client, unpaid_school["student"]).status_code == 403


def test_webhook_for_an_unknown_reference_is_accepted_quietly(client, school, chapa):
    """Chapa retries anything non-2xx, so an unknown reference must not look like an outage."""
    assert post_webhook(client, {"tx_ref": "eduhub-unknown-123", "status": "success"}).status_code == 200


def test_webhook_needs_a_reference(client, school, chapa):
    assert post_webhook(client, {"status": "success"}).status_code == 400


def test_webhook_rejects_a_non_json_body(client, school, chapa):
    raw = b"not json at all"
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    r = client.post(f"{BILLING}/webhook", content=raw, headers={"Chapa-Signature": sig})
    assert r.status_code == 400


def test_webhook_after_a_redirect_does_not_double_extend(client, unpaid_school, chapa):
    tx_ref = start_checkout(client, unpaid_school)
    chapa.settle = settled()

    client.get(f"{BILLING}/verify/{tx_ref}", headers=unpaid_school["admin"])
    end_after_redirect = client.get(f"{BILLING}/subscription", headers=unpaid_school["admin"]).json()["subscription"]["current_period_end"]

    post_webhook(client, {"tx_ref": tx_ref, "status": "success"})
    end_after_webhook = client.get(f"{BILLING}/subscription", headers=unpaid_school["admin"]).json()["subscription"]["current_period_end"]

    assert end_after_redirect == end_after_webhook


# ---------- renewal ----------


def test_renewing_stacks_another_period(client, unpaid_school, chapa):
    chapa.settle = settled()
    first = start_checkout(client, unpaid_school)
    client.get(f"{BILLING}/verify/{first}", headers=unpaid_school["admin"])
    end_after_first = client.get(f"{BILLING}/subscription", headers=unpaid_school["admin"]).json()["subscription"]["current_period_end"]

    second = start_checkout(client, unpaid_school)
    client.get(f"{BILLING}/verify/{second}", headers=unpaid_school["admin"])
    overview = client.get(f"{BILLING}/subscription", headers=unpaid_school["admin"]).json()

    assert overview["subscription"]["current_period_end"] > end_after_first
    assert len(overview["transactions"]) == 2
    assert {t["status"] for t in overview["transactions"]} == {"success"}

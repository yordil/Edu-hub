"""The free AI tutor trial: a one-time token allowance per school, shared by all its students,
for schools without a paid plan. Only tutor chat counts against it."""

import asyncio

import pytest
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.ai.llm import text as reply
from app.core.config import get_settings
from tests.conftest import org_id_of, register_student, subscribe
from tests.test_tutor import fake_llm  # noqa: F401

CHAT = "/api/v1/tutor/chat"
ACCESS = "/api/v1/tutor/access"


@pytest.fixture(autouse=True)
def default_trial(app, monkeypatch):
    """Pin the allowance these tests assert against. Settings also read a developer's `.env`,
    where a larger local trial is a reasonable thing to have."""
    monkeypatch.setattr(get_settings(), "ai_free_trial_tokens", 10_000)


def chat(client, headers, fake, tokens=(100, 20)):
    fake.script = [reply("Start by factorising both sides.", tokens=tokens)]
    return client.post(CHAT, json={"message": "Help me with algebra"}, headers=headers)


def access(client, headers):
    return client.get(ACCESS, headers=headers).json()


def record_usage(database_url, org_id, tokens, feature="tutor_chat"):
    """AI usage the school already has on record, as the tutor would have logged it."""

    async def run():
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ai_usage (id, org_id, feature, model, input_tokens, output_tokens, outcome, created_at)"
                    " VALUES (gen_random_uuid(), :org, :feature, 'fake-model', :tokens, 0, 'ok', now())"
                ),
                {"org": org_id, "feature": feature, "tokens": tokens},
            )
        await engine.dispose()

    asyncio.run(run())


def test_a_school_without_a_plan_can_try_the_tutor(client, unpaid_school, fake_llm):  # noqa: F811
    r = chat(client, unpaid_school["student"], fake_llm)
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "Start by factorising both sides."


def test_access_reports_the_untouched_trial(client, unpaid_school):
    assert access(client, unpaid_school["student"]) == {
        "plan": "trial", "allowed": True, "trial_allowance": 10_000, "trial_used": 0, "trial_remaining": 10_000,
    }


def test_each_message_is_deducted_from_the_trial(client, unpaid_school, fake_llm):  # noqa: F811
    chat(client, unpaid_school["student"], fake_llm, tokens=(900, 100))
    a = access(client, unpaid_school["student"])
    assert (a["trial_used"], a["trial_remaining"], a["allowed"]) == (1_000, 9_000, True)


def test_the_trial_is_shared_by_the_whole_school(client, unpaid_school, fake_llm):  # noqa: F811
    chat(client, unpaid_school["student"], fake_llm, tokens=(900, 100))
    classmate = register_student(client, unpaid_school["code"], "classmate@free.edu")
    assert access(client, classmate)["trial_used"] == 1_000
    assert access(client, unpaid_school["teacher"])["trial_used"] == 1_000


def test_a_used_up_trial_turns_the_tutor_away(client, unpaid_school, database_url, fake_llm):  # noqa: F811
    record_usage(database_url, org_id_of(client, unpaid_school["admin"]), 10_000)

    r = chat(client, unpaid_school["student"], fake_llm)
    assert r.status_code == 403
    assert "used its free AI tutor trial (10,000 tokens)" in r.json()["detail"]
    assert "Upgrade to Pro" in r.json()["detail"]
    a = access(client, unpaid_school["student"])
    assert (a["allowed"], a["trial_remaining"]) == (False, 0)


def test_the_message_that_crosses_the_limit_still_completes(client, unpaid_school, fake_llm, monkeypatch):  # noqa: F811
    """The allowance is checked before each message, not part-way through a reply."""
    monkeypatch.setattr(get_settings(), "ai_free_trial_tokens", 500)
    assert chat(client, unpaid_school["student"], fake_llm, tokens=(900, 100)).status_code == 200
    assert chat(client, unpaid_school["student"], fake_llm).status_code == 403


def test_question_explanations_do_not_use_up_the_trial(client, unpaid_school, database_url):
    record_usage(database_url, org_id_of(client, unpaid_school["admin"]), 50_000, feature="explanation")
    a = access(client, unpaid_school["student"])
    assert (a["trial_used"], a["allowed"]) == (0, True)


def test_one_schools_usage_does_not_touch_another_schools_trial(client, school, unpaid_school, database_url):
    record_usage(database_url, org_id_of(client, school["admin"]), 10_000)
    assert access(client, unpaid_school["student"])["trial_used"] == 0


def test_a_paid_school_is_not_limited_by_the_trial(client, school, database_url, fake_llm):  # noqa: F811
    record_usage(database_url, org_id_of(client, school["admin"]), 50_000)
    a = access(client, school["student"])
    assert (a["plan"], a["allowed"]) == ("pro", True)
    assert chat(client, school["student"], fake_llm).status_code == 200


def test_the_trial_is_once_per_school_not_per_plan(client, unpaid_school, database_url, fake_llm):  # noqa: F811
    """Tokens used while subscribed still count, so a lapsed plan doesn't come with a fresh trial."""
    org = org_id_of(client, unpaid_school["admin"])
    subscribe(database_url, org, days=-1)  # a plan that has already expired
    record_usage(database_url, org, 20_000)
    assert chat(client, unpaid_school["student"], fake_llm).status_code == 403


def test_setting_the_trial_to_zero_disables_it(client, unpaid_school, fake_llm, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "ai_free_trial_tokens", 0)
    r = chat(client, unpaid_school["student"], fake_llm)
    assert r.status_code == 403
    # No trial exists, so there is nothing to have "used up".
    assert r.json()["detail"] == "Active subscription required. Upgrade your plan to access this feature."
    assert access(client, unpaid_school["student"])["trial_allowance"] == 0


def test_past_paper_exams_stay_pro_only(client, unpaid_school):
    """The trial covers the tutor. Paper exams use no AI tokens and remain part of Pro."""
    r = client.post("/api/v1/papers/00000000-0000-0000-0000-000000000000/start", headers=unpaid_school["student"])
    assert r.status_code == 403
    assert "subscription" in r.json()["detail"].lower()

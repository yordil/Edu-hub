"""Tutor agent logic, driven by a scripted FakeLLM so results are deterministic and free.
The same flow against real Bedrock lives in test_tutor_live.py."""

import json

import pytest

from app.ai.llm import FakeLLM, get_llm, text, tool_call
from app.core.config import get_settings
from tests.conftest import create_org, create_question, org_id_of, register_student, subscribe

CHAT = "/api/v1/tutor/chat"
USAGE = "/api/v1/tutor/usage"


@pytest.fixture
def fake_llm(app):
    fake = FakeLLM()
    app.dependency_overrides[get_llm] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_llm, None)


def practice_with_mistake(client, school):
    """Student answers one algebra question right and one genetics question wrong."""
    alg = create_question(client, school["teacher"], subject="math", topic="algebra", n=1)
    gen = create_question(client, school["teacher"], subject="biology", topic="genetics", n=2)
    bank = {alg["id"]: alg, gen["id"]: gen}
    a = client.post("/api/v1/attempts", json={"size": 2}, headers=school["student"]).json()
    answers = []
    for item in a["items"]:
        q = bank[item["question"]["id"]]
        want_correct = q["topic"] == "algebra"
        choice = next(c for c in q["choices"] if c["is_correct"] == want_correct)
        answers.append({"question_id": q["id"], "choice_id": choice["id"]})
    assert client.post(f"/api/v1/attempts/{a['id']}/submit", json={"answers": answers}, headers=school["student"]).status_code == 200
    return alg, gen


def tool_outputs(request: list[dict]) -> list[dict]:
    return [json.loads(m["content"]) for m in request if m["role"] == "tool"]


def test_agent_calls_tools_with_real_student_data(client, school, fake_llm):
    _, gen = practice_with_mistake(client, school)
    fake_llm.script = [
        tool_call("get_mastery"),
        tool_call("get_recent_mistakes", {"limit": 3}),
        text("Focus on genetics: review Punnett squares."),
    ]

    r = client.post(CHAT, json={"message": "What should I focus on?"}, headers=school["student"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "Focus on genetics: review Punnett squares."
    assert body["tools_used"] == ["get_mastery", "get_recent_mistakes"]
    assert body["usage"] == {"input_tokens": 300, "output_tokens": 40}

    first = fake_llm.requests[0]
    assert first[0]["role"] == "system" and "Sam" in first[0]["content"]
    assert first[-1] == {"role": "user", "content": "What should I focus on?"}

    mastery = tool_outputs(fake_llm.requests[1])[0]
    assert [t["topic"] for t in mastery["topics"]] == ["genetics", "algebra"]  # weakest first

    mistakes = tool_outputs(fake_llm.requests[2])[1]["mistakes"]
    assert len(mistakes) == 1
    assert mistakes[0]["question"] == gen["prompt"]
    assert mistakes[0]["correct_answer"] == next(c["text"] for c in gen["choices"] if c["is_correct"])
    assert mistakes[0]["student_answer"] != mistakes[0]["correct_answer"]


def test_conversation_history_and_listing(client, school, fake_llm):
    fake_llm.script = [text("Hi Sam! What would you like to study?"), text("Sure, let's do algebra.")]
    first = client.post(CHAT, json={"message": "  Hello tutor  "}, headers=school["student"]).json()
    cid = first["conversation_id"]

    second = client.post(CHAT, json={"conversation_id": cid, "message": "Algebra please"}, headers=school["student"])
    assert second.status_code == 200 and second.json()["conversation_id"] == cid
    history = [(m["role"], m["content"]) for m in fake_llm.requests[1][1:]]
    assert history == [
        ("user", "Hello tutor"),
        ("assistant", "Hi Sam! What would you like to study?"),
        ("user", "Algebra please"),
    ]

    convs = client.get("/api/v1/tutor/conversations", headers=school["student"]).json()
    assert [(c["id"], c["title"]) for c in convs] == [(cid, "Hello tutor")]
    detail = client.get(f"/api/v1/tutor/conversations/{cid}", headers=school["student"]).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]


def test_conversations_are_private(client, school, fake_llm):
    fake_llm.script = [text("hi")]
    cid = client.post(CHAT, json={"message": "hi"}, headers=school["student"]).json()["conversation_id"]
    classmate = register_student(client, school["code"], "classmate@school.edu")
    assert client.get(f"/api/v1/tutor/conversations/{cid}", headers=classmate).status_code == 404
    assert client.post(CHAT, json={"conversation_id": cid, "message": "x"}, headers=classmate).status_code == 404
    assert client.get("/api/v1/tutor/conversations", headers=classmate).json() == []


def test_tools_only_see_own_and_submitted_data(client, school, fake_llm):
    practice_with_mistake(client, school)
    classmate = register_student(client, school["code"], "classmate@school.edu")
    # Classmate has an attempt in progress: its answers must never reach the tutor.
    client.post("/api/v1/attempts", json={}, headers=classmate)

    fake_llm.script = [tool_call("get_recent_mistakes"), tool_call("get_mastery"), text("ok")]
    client.post(CHAT, json={"message": "my mistakes?"}, headers=classmate)
    outputs = tool_outputs(fake_llm.requests[-1])
    assert outputs[0] == {"mistakes": []}
    assert outputs[1]["topics"] == []


def test_recommendations_never_include_answers(client, school, fake_llm):
    create_question(client, school["teacher"], n=1)
    fake_llm.script = [tool_call("recommend_questions", {"limit": 99}), text("Try these.")]
    client.post(CHAT, json={"message": "give me practice"}, headers=school["student"])
    rec = tool_outputs(fake_llm.requests[-1])[0]["questions"]
    assert len(rec) == 1
    assert set(rec[0]) == {"subject", "topic", "difficulty", "question", "predicted_success_percent"}


def test_unknown_tool_and_bad_arguments_are_contained(client, school, fake_llm):
    fake_llm.script = [tool_call("drop_tables"), tool_call("get_mastery", {"limit": "lots"}), text("Done.")]
    r = client.post(CHAT, json={"message": "hi"}, headers=school["student"])
    assert r.status_code == 200 and r.json()["reply"] == "Done."
    outputs = tool_outputs(fake_llm.requests[-1])
    assert outputs[0] == {"error": "Unknown tool drop_tables"}
    assert outputs[1]["topics"] == []


def test_agent_loop_is_bounded(client, school, fake_llm):
    fake_llm.script = [tool_call("get_mastery") for _ in range(10)]
    r = client.post(CHAT, json={"message": "loop forever"}, headers=school["student"])
    assert r.status_code == 200
    assert r.json()["reply"].startswith("Sorry, I couldn't put together an answer")
    assert len(fake_llm.requests) == 5


def test_provider_failure_returns_503_but_bills_completed_calls(client, school, fake_llm):
    fake_llm.script = [tool_call("get_mastery")]  # second call fails: script exhausted
    r = client.post(CHAT, json={"message": "hi"}, headers=school["student"])
    assert r.status_code == 503
    usage = client.get(USAGE, headers=school["admin"]).json()
    # The failed call is logged too (0 tokens) so error rates are measurable.
    assert (usage["calls"], usage["tokens_used"]) == (2, 110)
    assert client.get("/api/v1/tutor/conversations", headers=school["student"]).json() == []


def test_usage_ledger_per_school(client, school, fake_llm):
    fake_llm.script = [tool_call("get_mastery"), text("a")]
    client.post(CHAT, json={"message": "hi"}, headers=school["student"])

    usage = client.get(USAGE, headers=school["teacher"]).json()
    assert usage["tokens_used"] == 230 and usage["calls"] == 2
    assert usage["budget"] == get_settings().ai_school_monthly_token_budget
    assert usage["remaining"] == usage["budget"] - 230
    assert usage["by_feature"] == [{"feature": "tutor_chat", "calls": 2, "tokens": 230}]
    assert client.get(USAGE, headers=school["student"]).status_code == 403

    other = create_org(client, email="boss@other.edu", name="Other")
    assert client.get(USAGE, headers=other).json()["tokens_used"] == 0


def test_school_budget_blocks_only_that_school(client, school, fake_llm, monkeypatch, database_url):
    monkeypatch.setattr(get_settings(), "ai_school_monthly_token_budget", 200)
    fake_llm.script = [tool_call("get_mastery"), text("a"), text("b")]
    assert client.post(CHAT, json={"message": "one"}, headers=school["student"]).status_code == 200

    blocked = client.post(CHAT, json={"message": "two"}, headers=school["student"])
    assert blocked.status_code == 429
    assert "allowance" in blocked.json()["detail"]

    other = create_org(client, email="boss@other.edu", name="Other")
    subscribe(database_url, org_id_of(client, other))
    assert client.post(CHAT, json={"message": "hi"}, headers=other).status_code == 200


def test_hourly_message_limit_per_student(client, school, fake_llm, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_user_hourly_message_limit", 1)
    fake_llm.script = [text("a"), text("b")]
    assert client.post(CHAT, json={"message": "one"}, headers=school["student"]).status_code == 200
    assert client.post(CHAT, json={"message": "two"}, headers=school["student"]).status_code == 429
    classmate = register_student(client, school["code"], "classmate@school.edu")
    assert client.post(CHAT, json={"message": "hi"}, headers=classmate).status_code == 200


def test_message_validation(client, school, fake_llm):
    assert client.post(CHAT, json={"message": "   "}, headers=school["student"]).status_code == 422
    assert client.post(CHAT, json={"message": "x" * 2001}, headers=school["student"]).status_code == 422
    assert client.post(CHAT, json={"message": "hi"}).status_code == 401


def test_platform_metrics_include_ai_tokens(client, school, fake_llm, platform_admin):
    fake_llm.script = [text("a", tokens=(70, 30))]
    client.post(CHAT, json={"message": "hi"}, headers=school["student"])
    m = client.get("/api/v1/platform/metrics", headers=platform_admin).json()
    assert m["totals"]["ai_tokens_30d"] == 100
    assert {s["name"]: s["ai_tokens_30d"] for s in m["schools"]} == {"Springfield High": 100}

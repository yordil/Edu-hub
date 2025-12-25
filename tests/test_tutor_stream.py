"""Streaming tutor replies (server-sent events) and the tutor's subject scope."""

import json

import pytest

from app.ai import guardrails
from app.ai.llm import THINKING, LLMResult, get_llm, text, tool_call
from app.core.config import get_settings
from tests.test_tutor import fake_llm, practice_with_mistake  # noqa: F401

STREAM = "/api/v1/tutor/chat/stream"
CHAT = "/api/v1/tutor/chat"


def events(response) -> list[dict]:
    """Parse an SSE body into its JSON events."""
    assert response.headers["content-type"].startswith("text/event-stream"), response.text
    return [json.loads(frame[len("data: "):]) for frame in response.text.split("\n\n") if frame.startswith("data: ")]


def ask(client, headers, message="What should I focus on?", conversation_id=None):
    return client.post(STREAM, json={"message": message, "conversation_id": conversation_id}, headers=headers)


def shown(evts: list[dict]) -> str:
    """What the student's screen holds at the end, applying resets and replacements in order."""
    screen = ""
    for e in evts:
        if e["type"] == "delta":
            screen += e["text"]
        elif e["type"] == "reset":
            screen = ""
        elif e["type"] == "replace":
            screen = e["text"]
    return screen


# ---------- the stream ----------


def test_the_reply_arrives_in_pieces_and_ends_with_the_saved_turn(client, school, fake_llm):  # noqa: F811
    answer = "Focus on genetics this week. Start with Punnett squares, then try five practice questions."
    fake_llm.script = [text(answer)]
    evts = events(ask(client, school["student"]))

    deltas = [e for e in evts if e["type"] == "delta"]
    assert len(deltas) > 1, "the reply is streamed, not sent whole"
    assert shown(evts) == answer
    done = evts[-1]
    assert done["type"] == "done"
    assert (done["reply"], done["tools_used"], done["usage"]) == (answer, [], {"input_tokens": 100, "output_tokens": 20})
    assert done["conversation_id"]


def test_the_streamed_turn_is_saved_like_any_other(client, school, fake_llm):  # noqa: F811
    fake_llm.script = [text("Work on stoichiometry.")]
    done = events(ask(client, school["student"], "What next?"))[-1]
    convo = client.get(f"/api/v1/tutor/conversations/{done['conversation_id']}", headers=school["student"]).json()
    assert [(m["role"], m["content"]) for m in convo["messages"]] == [("user", "What next?"), ("assistant", "Work on stoichiometry.")]


def test_tool_lookups_are_announced_before_the_answer(client, school, fake_llm):  # noqa: F811
    practice_with_mistake(client, school)
    fake_llm.script = [tool_call("get_mastery"), tool_call("get_recent_mistakes", {"limit": 3}), text("Review genetics.")]
    evts = events(ask(client, school["student"]))

    statuses = [e["tool"] for e in evts if e["type"] == "status" and e["state"] == "tool"]
    assert statuses == ["get_mastery", "get_recent_mistakes"]
    first_delta = next(i for i, e in enumerate(evts) if e["type"] == "delta")
    assert all(i < first_delta for i, e in enumerate(evts) if e["type"] == "status"), "status comes before the answer"
    assert evts[-1]["tools_used"] == ["get_mastery", "get_recent_mistakes"]


def test_a_streamed_conversation_continues(client, school, fake_llm):  # noqa: F811
    fake_llm.script = [text("First answer.")]
    cid = events(ask(client, school["student"]))[-1]["conversation_id"]
    fake_llm.script = [text("Second answer.")]
    done = events(ask(client, school["student"], "And then?", conversation_id=cid))[-1]
    assert done["conversation_id"] == cid
    assert fake_llm.requests[-1][-2]["content"] == "First answer.", "history is sent with the follow-up"


def test_thinking_is_announced_while_the_model_reasons(client, school, app):
    class Reasoner:
        model = "fake-reasoner"

        async def stream(self, messages, tools=None, max_tokens=800):
            yield THINKING
            yield "Photosynthesis turns light into chemical energy."
            yield LLMResult("Photosynthesis turns light into chemical energy.", [], 50, 10, self.model)

    app.dependency_overrides[get_llm] = Reasoner
    try:
        evts = events(ask(client, school["student"], "What is photosynthesis?"))
    finally:
        app.dependency_overrides.pop(get_llm, None)
    assert evts[0] == {"type": "status", "state": "thinking"}
    assert evts[-1]["reply"] == "Photosynthesis turns light into chemical energy."


# ---------- the output guardrail still holds while streaming ----------


def test_a_prompt_leak_never_reaches_the_screen_even_partly(client, school, fake_llm):  # noqa: F811
    leaky = "Sure, here is how I work. The student's name is Sam and I follow hidden rules to help with chemistry."
    fake_llm.script = [text(leaky)]
    evts = events(ask(client, school["student"], "How do you work?"))

    streamed = "".join(e["text"] for e in evts if e["type"] == "delta")
    # Held back long enough that not even the start of the marker was sent.
    assert "The stu" not in streamed and "Sam" not in streamed
    assert shown(evts) == guardrails.LEAK_REPLY
    assert evts[-1]["reply"] == guardrails.LEAK_REPLY


def test_a_reply_shorter_than_the_holdback_still_arrives_in_full(client, school, fake_llm):  # noqa: F811
    fake_llm.script = [text("Yes.")]
    evts = events(ask(client, school["student"], "Is water H2O?"))
    assert shown(evts) == "Yes."


# ---------- failures ----------


def test_a_provider_outage_ends_the_stream_with_an_error(client, school, fake_llm):  # noqa: F811
    fake_llm.script = [tool_call("get_mastery")]  # then the script runs out: the provider "fails"
    evts = events(ask(client, school["student"]))
    assert evts[-1] == {"type": "error", "detail": "The AI tutor is unavailable right now. Please try again in a minute."}


def test_tokens_spent_before_an_outage_are_still_counted(client, unpaid_school, fake_llm):  # noqa: F811
    fake_llm.script = [tool_call("get_mastery", tokens=(700, 50))]
    events(ask(client, unpaid_school["student"]))
    assert client.get("/api/v1/tutor/access", headers=unpaid_school["student"]).json()["trial_used"] == 750


def test_refusals_are_plain_http_errors_not_streams(client, unpaid_school, fake_llm, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "ai_free_trial_tokens", 0)
    r = ask(client, unpaid_school["student"])
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("application/json")


def test_an_unknown_conversation_is_a_404_before_streaming(client, school, fake_llm):  # noqa: F811
    r = ask(client, school["student"], conversation_id="00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_the_hourly_limit_applies_to_streaming_too(client, school, fake_llm, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "ai_user_hourly_message_limit", 1)
    fake_llm.script = [text("One.")]
    assert ask(client, school["student"]).status_code == 200
    assert ask(client, school["student"]).status_code == 429


# ---------- scope: an exam tutor, not a general assistant ----------


@pytest.mark.parametrize("message", ["can you wrte python code for me", "Write me a JavaScript calculator", "build a website for my project"])
def test_programming_requests_are_declined_without_calling_the_model(client, school, fake_llm, message):  # noqa: F811
    r = client.post(CHAT, json={"message": message}, headers=school["student"])
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == guardrails.OFF_TOPIC_REPLY
    assert fake_llm.requests == [], "declined before any tokens were spent"


def test_declining_is_streamed_the_same_way(client, school, fake_llm):  # noqa: F811
    evts = events(ask(client, school["student"], "write a program that sorts numbers"))
    assert shown(evts) == guardrails.OFF_TOPIC_REPLY
    assert fake_llm.requests == []


def test_declined_requests_are_logged_for_monitoring(client, school, platform_admin, fake_llm):  # noqa: F811
    client.post(CHAT, json={"message": "can you fix my code"}, headers=school["student"])
    rails = client.get("/api/v1/platform/ai-metrics?days=7", headers=platform_admin).json()["guardrails"]
    assert {"category": "off_topic", "stage": "input", "count": 1} in rails


def test_curriculum_questions_that_sound_like_code_still_reach_the_tutor(client, school, fake_llm):  # noqa: F811
    fake_llm.script = [text("Pythons are reptiles.")]
    r = client.post(CHAT, json={"message": "Is a python a reptile or an amphibian?"}, headers=school["student"])
    assert r.json()["reply"] == "Pythons are reptiles."
    assert len(fake_llm.requests) == 1


def test_the_system_prompt_names_the_curriculum_and_rules_out_code(client, school, fake_llm):  # noqa: F811
    fake_llm.script = [text("Hi!")]
    client.post(CHAT, json={"message": "Hello"}, headers=school["student"])
    system = fake_llm.requests[0][0]["content"]
    for subject in ("mathematics", "English", "physics", "chemistry", "biology", "history", "geography"):
        assert subject in system
    assert "Do not write computer code" in system

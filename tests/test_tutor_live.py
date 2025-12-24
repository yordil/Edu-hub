"""End-to-end against real Bedrock. Opt-in because it costs tokens and needs a key:

    $env:RUN_LIVE_LLM="1"; .venv/Scripts/python -m pytest tests/test_tutor_live.py -s
"""

import os

import pytest

from app.ai.llm import OpenAICompatibleLLM, get_llm
from app.core.config import get_settings
from tests.test_tutor import practice_with_mistake

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE_LLM") != "1", reason="set RUN_LIVE_LLM=1 to call real Bedrock")

KNOWN_TOOLS = {"get_mastery", "get_recent_mistakes", "recommend_questions"}


@pytest.fixture
def real_llm(app):
    s = get_settings()
    if not s.llm_api_key:
        pytest.skip("No Bedrock key in .env")
    llm = OpenAICompatibleLLM(s.openai_base_url, s.llm_api_key, s.llm_chat_model, s.llm_timeout_seconds)
    app.dependency_overrides[get_llm] = lambda: llm
    yield llm
    app.dependency_overrides.pop(get_llm, None)


def test_live_tutor_reads_student_data_and_meters_tokens(client, school, real_llm):
    practice_with_mistake(client, school)

    r = client.post(
        "/api/v1/tutor/chat",
        json={"message": "Look at my results. What is my weakest topic, and what did I get wrong?"},
        headers=school["student"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    print(f"\n--- model: {real_llm.model}\n--- tools: {body['tools_used']}\n--- usage: {body['usage']}\n--- reply:\n{body['reply']}\n")

    assert body["reply"].strip()
    assert set(body["tools_used"]) & KNOWN_TOOLS, "the model should have looked up the student's data"
    assert body["usage"]["input_tokens"] > 0 and body["usage"]["output_tokens"] > 0
    assert "genetics" in body["reply"].lower()

    usage = client.get("/api/v1/tutor/usage", headers=school["admin"]).json()
    assert usage["tokens_used"] == body["usage"]["input_tokens"] + body["usage"]["output_tokens"]

    follow_up = client.post(
        "/api/v1/tutor/chat",
        json={"conversation_id": body["conversation_id"], "message": "Explain that genetics question simply."},
        headers=school["student"],
    )
    assert follow_up.status_code == 200, follow_up.text
    print(f"--- follow-up reply:\n{follow_up.json()['reply']}\n")
    assert follow_up.json()["reply"].strip()
